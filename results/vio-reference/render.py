"""Render recorded room1 qualification evidence without rerunning evaluation.

Example, from the repository root after acquiring and running the reference::

    python results/vio-reference/render.py \
        --run outputs/vio-reference/room1-run-1 \
        --prepared outputs/vio-reference-data/prepared \
        --output results/vio-reference/room1.png

The stored alignment and per-sample errors are authoritative. This script only
transforms coordinates for plotting and verifies the identities of files it reads.
It does not fit alignment, interpolate reference poses, or recompute any metric.
"""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from meridian.vio_dataset import camera_timestamps_ns
from meridian.vio_evaluation import read_trajectory


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify_file(path, identity):
    if (path.is_symlink() or not path.is_file()
            or path.stat().st_size != identity["size_bytes"]
            or sha256(path) != identity["sha256"]):
        raise ValueError(f"plot input differs from its recorded identity: {path.name}")


def segments(times, maximum_gap_ns):
    """Index groups whose connecting lines never cross a rejected reference gap."""
    if not len(times):
        return []
    cuts = [i for i in range(1, len(times))
            if int(times[i]) - int(times[i - 1]) > maximum_gap_ns]
    return np.split(np.arange(len(times)), cuts)


def shade_missing(axis, intervals, timestamps, seconds, *, color, label):
    """Shade missing sample support using the midpoints to adjacent camera ticks."""
    for number, interval in enumerate(intervals):
        first = int(np.searchsorted(timestamps, interval["first_timestamp_ns"]))
        last = int(np.searchsorted(timestamps, interval["last_timestamp_ns"]))
        if (first >= len(timestamps) or last >= len(timestamps)
                or timestamps[first] != interval["first_timestamp_ns"]
                or timestamps[last] != interval["last_timestamp_ns"]):
            raise ValueError("stored missing interval does not match the camera schedule")
        left = seconds[0] if first == 0 else (seconds[first - 1] + seconds[first]) / 2
        right = seconds[-1] if last == len(timestamps) - 1 else (seconds[last] + seconds[last + 1]) / 2
        axis.axvspan(left, right, color=color, alpha=0.8, linewidth=0,
                     label=label if number == 0 else None, zorder=0)


def render(run, prepared, output):
    result = read_json(run / "result.json")
    if result.get("schema_version") != 1 or result.get("state") != "complete":
        raise ValueError("the figure requires a completed schema-1 reference run")
    for name in ("run-spec.json", "evaluation.json", "trajectory.csv"):
        verify_file(run / name, result["artifacts"][name])
    spec = read_json(run / "run-spec.json")
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(canonical.encode()).hexdigest() != result["run_spec_sha256"]:
        raise ValueError("run specification identity differs from the completed result")
    if sha256(prepared / "manifest.json") != spec["dataset_manifest_sha256"]:
        raise ValueError("prepared dataset identity differs from the executed input")
    manifest = read_json(prepared / "manifest.json")
    if manifest != spec["input_manifest"] or manifest["dataset_id"] != "tum-vi-room1-512-16":
        raise ValueError("the figure requires the recorded room1 prepared dataset")
    for name in ("reference/trajectory.csv", "sensors/mav0/cam0/data.csv"):
        verify_file(prepared / name, manifest["files"][name])

    evaluation = read_json(run / "evaluation.json")
    if (evaluation.get("schema_version") != 1 or evaluation.get("status") != "ok"
            or evaluation["protocol"]["version"] != 1
            or evaluation["alignment"]["scale"] != 1.0):
        raise ValueError("the figure requires the recorded fixed-scale version-1 evaluation")
    estimate = read_trajectory(run / "trajectory.csv")
    reference = read_trajectory(prepared / "reference/trajectory.csv")
    expected = np.asarray(camera_timestamps_ns(prepared), dtype=np.int64)
    if (len(expected) != evaluation["counts"]["expected_camera_samples"]
            or int(expected[0]) != evaluation["timing"]["expected_first_timestamp_ns"]
            or int(expected[-1]) != evaluation["timing"]["expected_last_timestamp_ns"]):
        raise ValueError("camera schedule disagrees with the stored evaluation")
    origin = int(expected[0])
    seconds = np.array([(int(stamp) - origin) * 1e-9 for stamp in expected])
    rotation = np.asarray(evaluation["alignment"]["rotation_estimate_world_to_reference_world"])
    translation = np.asarray(evaluation["alignment"]["translation_m"])
    aligned = estimate.position_m @ rotation.T + translation

    # Populate the complete camera schedule with stored errors. NaN entries break
    # the line at startup, absent estimates, and unsupported reference samples.
    stored = evaluation["per_sample"]
    error_cm = np.full(len(expected), np.nan)
    indices = np.searchsorted(expected, np.asarray(stored["timestamp_ns"], dtype=np.int64))
    if (np.any(indices >= len(expected))
            or expected[indices].tolist() != stored["timestamp_ns"]):
        raise ValueError("stored errors do not match exact expected camera timestamps")
    error_cm[indices] = 100 * np.asarray(stored["position_error_m"])

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 12,
        "axes.labelsize": 10, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#a5adb8", "axes.linewidth": 0.7,
        "grid.color": "#e2e6eb", "grid.linewidth": 0.6,
        "text.color": "#172b3a", "axes.labelcolor": "#263b4b",
        "xtick.color": "#435463", "ytick.color": "#435463",
    })
    figure, (xy, error) = plt.subplots(1, 2, figsize=(13, 6),
                                     gridspec_kw={"width_ratios": [1, 1.35]})
    figure.subplots_adjust(left=0.07, right=0.98, bottom=0.26, top=0.77, wspace=0.25)
    figure.suptitle("Room1 | OpenVINS reference replay", x=0.07, y=0.965,
                   ha="left", fontsize=18, fontweight="bold")
    metrics = evaluation["metrics"]
    figure.text(0.07, 0.891,
                f"ATE RMSE {100 * metrics['ate_rmse_m']:.2f} cm"
                f"   |   Orientation RMSE {metrics['orientation_rmse_deg']:.2f}°"
                f"   |   Estimate coverage {100 * evaluation['coverage_fraction']:.2f}%"
                f"   |   Evaluated coverage {100 * evaluation['evaluated_fraction']:.2f}%",
                fontsize=10.5)

    in_window = (reference.timestamp_ns >= expected[0]) & (reference.timestamp_ns <= expected[-1])
    reference_times = reference.timestamp_ns[in_window]
    reference_positions = reference.position_m[in_window]
    for index, group in enumerate(segments(reference_times, evaluation["protocol"]["max_reference_gap_ns"])):
        xy.plot(reference_positions[group, 0], reference_positions[group, 1],
                color="#62727d", linewidth=1.45, alpha=0.85,
                label="Motion-capture reference" if index == 0 else None)
    # Break estimated segments at any missing expected camera sample as well.
    estimate_camera_indices = np.searchsorted(expected, estimate.timestamp_ns)
    if (np.any(estimate_camera_indices >= len(expected))
            or not np.array_equal(expected[estimate_camera_indices], estimate.timestamp_ns)):
        raise ValueError("this figure requires estimates at exact camera timestamps")
    breaks = np.flatnonzero(np.diff(estimate_camera_indices) > 1) + 1
    for index, group in enumerate(np.split(np.arange(len(aligned)), breaks)):
        xy.plot(aligned[group, 0], aligned[group, 1], color="#1769a6", linewidth=1,
                label="Aligned VIO estimate" if index == 0 else None)
    xy.scatter(aligned[0, 0], aligned[0, 1], marker="o", s=26, color="#1769a6",
               edgecolor="white", linewidth=.6, zorder=3)
    xy.set(title="Trajectory: reference-world XY projection", xlabel="Reference-world X [m]",
           ylabel="Reference-world Y [m]")
    xy.set_aspect("equal", adjustable="box")
    xy.grid(True)
    xy.legend(loc="upper left", bbox_to_anchor=(0, -0.18), frameon=False, fontsize=9)

    shade_missing(error, evaluation["missing_estimate_intervals"], expected, seconds,
                  color="#e1e5ea", label="No estimate")
    shade_missing(error, evaluation["unsupported_reference_intervals"], expected, seconds,
                  color="#f1d4ad", label="Reference unavailable")
    error.plot(seconds, error_cm, color="#1769a6", linewidth=1.2, label="Position error", zorder=2)
    error.axhline(100 * metrics["ate_rmse_m"], color="#497961", linestyle="--",
                  linewidth=1, label="ATE RMSE", zorder=1)
    error.set(title="Position error over the full camera timeline", xlabel="Time from first camera sample [s]",
              ylabel="Aligned position error [cm]", xlim=(0, seconds[-1]),
              ylim=(0, float(np.nanmax(error_cm)) * 1.12))
    error.grid(True)
    error.legend(loc="upper left", bbox_to_anchor=(0, -0.18), frameon=False, ncol=2, fontsize=9)

    figure.text(0.07, 0.075,
                "One recorded run; global SE(3) alignment, scale fixed to 1. "
                "Reference and error lines retain gaps.\n"
                "Data: TUM VI Benchmark, Schubert et al. (2018), CC BY 4.0. "
                "Handheld recording; these results do not establish drone-flight performance.",
                fontsize=8.5, color="#52616d", va="top", linespacing=1.65)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, facecolor="white", metadata={
        "Title": "Meridian room1 VIO reference qualification",
        "Description": "Recorded evaluation and aligned XY trajectory. TUM VI Benchmark, Schubert et al. (2018), CC BY 4.0.",
        "Run specification SHA256": result["run_spec_sha256"],
    })
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.run, args.prepared, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
