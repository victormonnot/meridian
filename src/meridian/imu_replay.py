"""Replay one selected DataFlash timing segment with shared roll estimators."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path
import platform

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian.dataflash import ImuRecord, read_dataflash
from meridian.dataflash_audit import analyze_records, sha256_file
from meridian.replay import REPLAY_GRAVITY_M_S2, ReplayConfig, ReplayResult, replay_snapshots
from meridian.tilt import wrap_angle


def select_segment(records: list[ImuRecord], *, instance: int, segment: int,
                   max_gap_s: float) -> tuple[list[int], dict]:
    """Select one audited segment explicitly, without choosing by filter outcome."""
    for name, value in (("instance", instance), ("segment", segment)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    streams, labels = analyze_records(records, gap_threshold_s=max_gap_s)
    stream = streams.get(str(instance))
    if stream is None or segment >= len(stream["segments"]):
        raise ValueError("requested instance/segment does not exist; inspect the audit first")
    indices = [i for i, record in enumerate(records)
               if record.instance == instance and labels[i] == segment]
    if len(indices) < 2:
        raise ValueError("selected segment needs at least two IMU observations")
    return indices, stream["segments"][segment]


def _angles(result: ReplayResult) -> dict[str, np.ndarray]:
    return {"gyro": result.gyro_roll_rad, "complementary": result.complementary_roll_rad,
            "kalman": result.kalman_state[:, 0], "ekf": result.ekf_state[:, 0]}


def _metrics(result: ReplayResult) -> dict:
    agreement = {}
    for name, values in _angles(result).items():
        difference = np.rad2deg(wrap_angle(values - result.accel_roll_rad))
        agreement[name] = {
            "rms_difference_deg": float(np.sqrt(np.mean(difference**2))),
            "max_abs_difference_deg": float(np.max(np.abs(difference))),
            "final_difference_deg": float(difference[-1]),
        }
    return {
        "agreement_with_apparent_accelerometer_tilt": agreement,
        "gyro_angle_change_deg": float(np.rad2deg(result.gyro_roll_rad[-1] - result.gyro_roll_rad[0])),
        "kalman_final_bias_deg_s": float(np.rad2deg(result.kalman_state[-1, 1])),
        "kalman_model_final_angle_std_deg": float(np.rad2deg(np.sqrt(result.kalman_covariance[-1, 0, 0]))),
        "kalman_model_final_bias_std_deg_s": float(np.rad2deg(np.sqrt(result.kalman_covariance[-1, 1, 1]))),
        "mean_squared_normalized_innovation": float(np.mean(result.innovation_rad**2 / result.innovation_variance_rad2)),
        "ekf_final_bias_deg_s": float(np.rad2deg(result.ekf_state[-1, 1])),
        "ekf_model_final_angle_std_deg": float(np.rad2deg(np.sqrt(result.ekf_covariance[-1, 0, 0]))),
        "ekf_model_final_bias_std_deg_s": float(np.rad2deg(np.sqrt(result.ekf_covariance[-1, 1, 1]))),
        "ekf_mean_normalized_innovation_squared": float(np.mean(result.ekf_nis)),
        "ekf_innovation_component_rms_m_s2": np.sqrt(np.mean(result.ekf_innovation_m_s2**2, axis=0)).tolist(),
    }


def _broken_principal(values: np.ndarray) -> np.ndarray:
    values = values.copy()
    values[1:][np.abs(np.diff(values)) > 180] = np.nan
    return values


def _plot(result: ReplayResult, output: Path) -> None:
    figure = Figure(figsize=(11, 12), constrained_layout=True)
    FigureCanvasAgg(figure)
    axes = figure.subplots(5, 1, sharex=True)
    elapsed = result.elapsed_s
    axes[0].plot(elapsed, _broken_principal(np.rad2deg(result.accel_roll_rad)),
                 color="0.6", linewidth=0.7, label="Apparent accelerometer tilt")
    for k, (name, values) in enumerate(_angles(result).items()):
        axes[0].plot(elapsed, _broken_principal(np.rad2deg(wrap_angle(values))),
                     color=f"C{k}", linewidth=1, label={"kalman": "Angle KF", "ekf": "Vector EKF"}.get(name, name.capitalize()))
        difference = np.rad2deg(wrap_angle(values - result.accel_roll_rad))
        axes[1].plot(elapsed, _broken_principal(difference), color=f"C{k}", linewidth=0.8)
    for label, state, covariance, color in (
        ("Angle KF", result.kalman_state, result.kalman_covariance, "C2"),
        ("Vector EKF", result.ekf_state, result.ekf_covariance, "C3"),
    ):
        bias = np.rad2deg(state[:, 1])
        std = np.rad2deg(np.sqrt(covariance[:, 1, 1]))
        axes[2].plot(elapsed, bias, color=color, label=label)
        axes[2].fill_between(elapsed, bias - 2 * std, bias + 2 * std, color=color, alpha=0.12)
    axes[2].set_title("Estimated bias with ±2 model standard deviations", fontsize="small")
    axes[3].plot(elapsed[1:], result.innovation_rad**2 / result.innovation_variance_rad2,
                 color="C2", marker=".", markersize=1, linestyle="none")
    axes[4].plot(elapsed[1:], result.ekf_nis, color="C3", marker=".", markersize=1, linestyle="none")
    labels = ("Principal roll (deg)", "Difference to tilt (deg)", "Bias (deg/s)", "Angle KF NIS (1D)", "Vector EKF NIS (2D)")
    for axis, label in zip(axes, labels):
        axis.set_ylabel(label)
        axis.grid(alpha=0.25)
    axes[0].legend(loc="best", fontsize="small")
    axes[2].legend(loc="best", fontsize="small")
    axes[-1].set_xlabel("Time since selected segment start (s)")
    figure.suptitle("Offline IMU replay — shared measurements, no independent angle reference\n"
                   "Disagreement and covariance are model diagnostics, not accuracy or calibrated confidence")
    figure.savefig(output, dpi=140, metadata={"Software": "Meridian"})


def run_replay(source: Path, output: Path, *, instance: int, segment: int,
               config: ReplayConfig = ReplayConfig()) -> dict:
    """Decode a source, replay a selected finite run, and export to a new directory."""
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    fingerprint = sha256_file(source)
    data = read_dataflash(source)
    indices, selection = select_segment(data.records, instance=instance, segment=segment,
                                        max_gap_s=config.max_gap_s)
    records = [data.records[i] for i in indices]
    gyro = np.array([r.gyro_rad_s for r in records])
    force = np.array([r.accel_m_s2 for r in records])
    result = replay_snapshots([r.time_us for r in records], gyro, force, config=config)
    if sha256_file(source) != fingerprint:
        raise ValueError("source file changed during replay")
    summary = {
        "schema_version": 2,
        "source": {"sha256": fingerprint, "size_bytes": source.stat().st_size},
        "environment": {"python": platform.python_version(), "numpy": np.__version__,
                        "matplotlib": matplotlib.__version__},
        "metadata": data.metadata,
        "selection": {"instance": instance, "gap_threshold_s": config.max_gap_s, **selection},
        "config": asdict(config),
        "observation_models": {
            "kalman": {"dimension": 1, "variance_rad2": float(np.deg2rad(config.angle_noise_std_deg)**2)},
            "ekf": {"dimension": 2, "components": ["body_y", "body_z"],
                    "gravity_m_s2": REPLAY_GRAVITY_M_S2,
                    "component_std_m_s2": config.accel_noise_std_m_s2,
                    "covariance_m2_s4": (np.eye(2) * config.accel_noise_std_m_s2**2).tolist(),
                    "noise_mapping": "sigma_accel = g * radians(angle_noise_std_deg); local equivalence at nominal gravity, not identified noise",
                    "measurement_normalized": False},
        },
        "initialization": {"angle_rad": float(result.accel_roll_rad[0]), "bias_rad_s": 0.0,
                           "state_order": ["roll_rad", "bias_rad_s"],
                           "angle_source": "first selected accelerometer tilt; used once",
                           "covariance": result.kalman_covariance[0].tolist(),
                           "covariance_applies_to": ["kalman", "ekf"]},
        "sampling": {"gyro_rule": "previous snapshot held over the next logged interval",
                     "correction_rule": "current accelerometer tilt for scalar filters, raw body y/z force for EKF after prediction; none at initialization",
                     "state_phase": "after prediction and correction; initial state at row 0",
                     "innovation_phase": "prior before correction, rows 1 through N-1",
                     "propagations_and_corrections": len(records) - 1,
                     "final_gyro_snapshot_used_for_propagation": False},
        "selected_force_norm_m_s2": {"min": float(np.min(np.linalg.norm(force, axis=1))),
                                     "max": float(np.max(np.linalg.norm(force, axis=1)))},
        "diagnostics": _metrics(result),
        "limitations": [
            "No independent angle or gyro-bias truth; agreement with a fused input is not accuracy.",
            "Tuning is illustrative, not identified noise, calibration, or an acceptance specification.",
            "Filtered logger snapshots may be correlated and delayed; a left hold does not restore independence or synchronization.",
            "The one-axis gravity model does not cover general 3D motion or translational acceleration.",
            "Covariance and normalized innovations describe the assumed model, not calibrated physical uncertainty.",
            "Scalar and vector NIS have different dimensions (1 and 2); vector NIS includes force-magnitude mismatch invisible to tilt.",
            "Replay rejects undefined accelerometer tilt for the shared comparison, even though the standalone EKF accepts finite zero force.",
            "No sorting, interpolation across gaps, rejection of physical disturbances, or bias random walk is applied.",
            "Health and arming metadata cover the source; a finite segment does not certify bench conditions.",
            "Historical source metadata does not identify current firmware, complete recording integrity, or new acquisition conditions.",
            "Offline replay does not establish embedded, real-time, or flight validation.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["record_index", "instance", "segment_id", "time_us", "gyro_x_rad_s", "gyro_y_rad_s",
                         "gyro_z_rad_s", "accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2"])
        for i, record in zip(indices, records):
            writer.writerow([i, instance, segment, record.time_us, *record.gyro_rad_s, *record.accel_m_s2])
    with (output / "estimates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_us", "elapsed_s", "accel_roll_rad", "gyro_roll_rad", "complementary_roll_rad",
                         "kalman_roll_rad", "kalman_bias_rad_s", "p_angle_rad2", "p_angle_bias_rad2_s", "p_bias_rad2_s2"])
        for k in range(len(records)):
            covariance = result.kalman_covariance[k]
            writer.writerow([int(result.time_us[k]), result.elapsed_s[k], result.accel_roll_rad[k],
                             result.gyro_roll_rad[k], result.complementary_roll_rad[k], *result.kalman_state[k],
                             covariance[0, 0], covariance[0, 1], covariance[1, 1]])
    with (output / "innovations.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_us", "innovation_rad", "innovation_variance_rad2", "squared_normalized_innovation"])
        for time, innovation, variance in zip(result.time_us[1:], result.innovation_rad, result.innovation_variance_rad2):
            writer.writerow([int(time), innovation, variance, innovation * innovation / variance])
    with (output / "ekf_estimates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_us", "elapsed_s", "ekf_roll_rad", "ekf_bias_rad_s",
                         "p_angle_rad2", "p_angle_bias_rad2_s", "p_bias_rad2_s2"])
        for k, (state, covariance) in enumerate(zip(result.ekf_state, result.ekf_covariance)):
            writer.writerow([int(result.time_us[k]), result.elapsed_s[k], *state,
                             covariance[0, 0], covariance[0, 1], covariance[1, 1]])
    with (output / "ekf_innovations.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_us", "innovation_y_m_s2", "innovation_z_m_s2",
                         "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4", "normalized_innovation_squared"])
        for time, innovation, covariance, nis in zip(result.time_us[1:], result.ekf_innovation_m_s2,
                                                     result.ekf_innovation_covariance_m2_s4, result.ekf_nis):
            writer.writerow([int(time), *innovation, covariance[0, 0], covariance[0, 1], covariance[1, 1], nis])
    _plot(result, output / "overview.png")
    summary["artifact_sha256"] = {name: sha256_file(output / name)
                                  for name in ("measurements.csv", "estimates.csv", "innovations.csv",
                                               "ekf_estimates.csv", "ekf_innovations.csv", "overview.png")}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="DataFlash .bin source inspected with the audit tool")
    parser.add_argument("--instance", type=int, required=True)
    parser.add_argument("--segment", type=int, required=True, help="per-instance segment ID from the audit")
    parser.add_argument("--output", type=Path, required=True, help="new local output directory")
    parser.add_argument("--gyro-noise-std-deg-s", type=float, default=1.0, help="illustrative per-held-sample tuning")
    parser.add_argument("--angle-noise-std-deg", type=float, default=2.0,
                        help="tilt noise and initial angle uncertainty; EKF component noise = g * radians(this value)")
    parser.add_argument("--initial-bias-std-deg-s", type=float, default=1.0)
    parser.add_argument("--complementary-tau-s", type=float, default=1.0)
    parser.add_argument("--max-gap-s", type=float, default=0.2, help="must match the audited segmentation threshold")
    args = vars(parser.parse_args(argv))
    source, output, instance, segment = (args.pop(key) for key in ("source", "output", "instance", "segment"))
    try:
        summary = run_replay(source, output, instance=instance, segment=segment, config=ReplayConfig(**args))
    except (OSError, ValueError, ImportError) as error:
        parser.exit(2, f"error: {error}\n")
    selected = summary["selection"]
    print(f"Replayed IMU {instance}, segment {segment}: {selected['samples']} observations, {selected['duration_s']:.6f} s.")
    metadata = summary["metadata"]
    if metadata.get("parser_diagnostic_count", 0) or metadata.get("unparsed_tail_bytes", 0):
        print("Source decoder diagnostics or trailing bytes remain; inspect summary.json.")
    print(f"Results written to {output}; comparison has no independent ground truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
