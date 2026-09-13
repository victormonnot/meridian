"""Inspect logged IMU snapshots without resampling or running an estimator."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import platform

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian.dataflash import ImuRecord, read_dataflash


def _statistics(values: np.ndarray) -> dict:
    """Descriptive statistics of finite values; not a noise identification."""
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0, "min": None, "max": None, "mean": None, "std": None}
    return {"count": len(values), "min": float(np.min(values)),
            "max": float(np.max(values)), "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=0))}


def analyze_records(records: list[ImuRecord], *, gap_threshold_s: float = 0.2) -> tuple[dict, list]:
    """Retain file order; mark finite runs separated by gaps or clock discontinuity.

    Segment IDs are local to each instance. A usable pair requires finite sensor
    components at both ends and 0 < delta TimeUS <= gap_threshold_s. This is a
    timing/finite-data condition only, not an arming or physical-model check.
    """
    if not math.isfinite(gap_threshold_s) or gap_threshold_s <= 0:
        raise ValueError("gap_threshold_s must be finite and positive")
    if not records:
        raise ValueError("no IMU records to analyze")
    labels = [None] * len(records)
    result = {}
    for instance in sorted({r.instance for r in records}):
        indices = [i for i, r in enumerate(records) if r.instance == instance]
        times = [records[i].time_us for i in indices]
        # Subtract integer microseconds before conversion to preserve clock differences.
        dt = np.array([b - a for a, b in zip(times, times[1:])], dtype=np.float64) / 1e6
        positive = dt[dt > 0]
        sensors = np.array([(*records[i].gyro_rad_s, *records[i].accel_m_s2) for i in indices])
        finite = np.all(np.isfinite(sensors), axis=1)
        segments, gaps = [], []
        previous = None
        for j, i in enumerate(indices):
            if j and dt[j - 1] > gap_threshold_s:
                gaps.append({"previous_record_index": indices[j - 1], "next_record_index": i,
                             "previous_time_us": times[j - 1], "next_time_us": times[j],
                             "duration_s": float(dt[j - 1])})
            if not finite[j]:
                previous = None
                continue
            if previous is None or not (0 < dt[j - 1] <= gap_threshold_s):
                segments.append({"id": len(segments), "first_record_index": i,
                                 "last_record_index": i, "start_time_us": times[j],
                                 "end_time_us": times[j], "samples": 0, "duration_s": 0.0})
            segment = segments[-1]
            segment.update(last_record_index=i, end_time_us=times[j], samples=segment["samples"] + 1,
                           duration_s=(times[j] - segment["start_time_us"]) / 1e6)
            labels[i] = segment["id"]
            previous = i
        force = sensors[:, 3:]
        yz = np.hypot(force[:, 1], force[:, 2])
        tilt_ok = np.all(np.isfinite(force), axis=1) & (yz > 1e-9)
        tilt = np.rad2deg(np.arctan2(-force[tilt_ok, 1], -force[tilt_ok, 2]))
        tilt_bounds = _statistics(tilt)
        # Arithmetic angle mean/std would misrepresent samples spanning the branch cut.
        del tilt_bounds["mean"], tilt_bounds["std"]
        result[str(instance)] = {
            "samples": len(indices), "first_time_us": times[0], "last_time_us": times[-1],
            "time_extent_s": (max(times) - min(times)) / 1e6,
            "median_positive_dt_s": float(np.median(positive)) if len(positive) else None,
            "max_positive_dt_s": float(np.max(positive)) if len(positive) else None,
            "duplicate_intervals": int(np.sum(dt == 0)),
            "backward_intervals": int(np.sum(dt < 0)),
            "nonfinite_sensor_records": int(np.sum(~finite)),
            "gaps": gaps, "segments": segments,
            "within_segment_duration_s": sum(s["duration_s"] for s in segments),
            "gyro_rad_s": {axis: _statistics(sensors[:, k]) for k, axis in enumerate("xyz")},
            "accel_m_s2": {axis: _statistics(force[:, k]) for k, axis in enumerate("xyz")},
            "accel_norm_m_s2": _statistics(np.linalg.norm(force, axis=1)),
            "apparent_roll_principal_deg": tilt_bounds,
        }
    return result, labels


def _plot(records: list[ImuRecord], labels: list, output: Path) -> None:
    figure = Figure(figsize=(11, 9), constrained_layout=True)
    FigureCanvasAgg(figure)
    axes = figure.subplots(4, 1, sharex=True)
    for instance in sorted({r.instance for r in records}):
        seen = False
        style = ("-", "--", ":", "-.")[instance % 4]
        for segment in sorted({labels[i] for i, r in enumerate(records)
                               if r.instance == instance and labels[i] is not None}):
            selected = [r for i, r in enumerate(records) if r.instance == instance and labels[i] == segment]
            time = np.array([r.time_us for r in selected]) / 1e6
            gyro = np.array([r.gyro_rad_s for r in selected])
            force = np.array([r.accel_m_s2 for r in selected])
            for k, axis in enumerate("xyz"):
                label = f"IMU {instance}, {axis}" if not seen else None
                color = f"C{(3 * instance + k) % 10}"
                axes[0].plot(time, np.rad2deg(gyro[:, k]), color=color, linestyle=style, label=label, linewidth=0.7)
                axes[1].plot(time, force[:, k], color=color, linestyle=style, linewidth=0.7)
            axes[2].plot(time, np.linalg.norm(force, axis=1), color=f"C{instance % 10}", linestyle=style, linewidth=0.7)
            tilt = np.rad2deg(np.arctan2(-force[:, 1], -force[:, 2]))
            tilt[np.hypot(force[:, 1], force[:, 2]) <= 1e-9] = np.nan
            tilt[1:][np.abs(np.diff(tilt)) > 180] = np.nan
            axes[3].plot(time, tilt, color=f"C{instance % 10}", linestyle=style, linewidth=0.7)
            seen = True
    for axis, label in zip(axes, ("Gyro (deg/s)", "Specific force (m/s²)", "3D force norm (m/s²)", "Apparent roll (deg)")):
        axis.set_ylabel(label)
        axis.grid(alpha=0.25)
    if axes[0].lines:
        axes[0].legend(loc="upper right", fontsize="small")
    axes[-1].set_xlabel("Logged TimeUS / 1e6 (s since startup)")
    figure.suptitle("DataFlash IMU audit — line breaks at gaps and clock discontinuities\n"
                   "Principal tilt breaks at ±180°; gravity assumption, no angle ground truth")
    figure.savefig(output, dpi=140, metadata={"Software": "Meridian"})


def sha256_file(path: Path) -> str:
    """Fingerprint source bytes for offline audit and replay provenance."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_file(source: Path, output: Path, *, gap_threshold_s: float = 0.2) -> dict:
    """Audit one DataFlash binary into a new directory; preserve the source."""
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    if not math.isfinite(gap_threshold_s) or gap_threshold_s <= 0:
        raise ValueError("gap_threshold_s must be finite and positive")
    fingerprint = sha256_file(source)
    data = read_dataflash(source)
    streams, labels = analyze_records(data.records, gap_threshold_s=gap_threshold_s)
    # Detect ordinary source changes during decoding; this does not authenticate provenance.
    if sha256_file(source) != fingerprint:
        raise ValueError("source file changed during audit")
    summary = {
        "schema_version": 1,
        "source": {"sha256": fingerprint, "size_bytes": source.stat().st_size},
        "environment": {"python": platform.python_version(), "numpy": np.__version__,
                        "matplotlib": matplotlib.__version__},
        "gap_threshold_s": gap_threshold_s,
        "metadata": data.metadata,
        "imu": streams,
        "limitations": [
            "Historical log metadata does not identify the current flight-controller configuration.",
            "IMU logger timestamps do not establish simultaneous sensor sampling or filter delays.",
            "Segment IDs certify only finite components and bounded positive logged intervals.",
            "No arming event is not evidence that the complete recording was disarmed.",
            "No resampling, estimator replay, bias/noise identification, or accuracy evaluation is performed.",
            "Accelerometer tilt and descriptive statistics have no independent ground truth.",
            "Successful decoding and source hashing do not prove a complete, authentic recording.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    with (output / "imu.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["record_index", "instance", "time_us", "segment_id", "gyro_x_rad_s", "gyro_y_rad_s",
                         "gyro_z_rad_s", "accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2"])
        for i, record in enumerate(data.records):
            writer.writerow([i, record.instance, record.time_us, labels[i], *record.gyro_rad_s, *record.accel_m_s2])
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _plot(data.records, labels, output / "overview.png")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="DataFlash .bin file")
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--gap-threshold-s", type=float, default=0.2,
                        help="split runs above this logged interval (default: 0.2 s)")
    args = parser.parse_args(argv)
    try:
        summary = audit_file(args.source, args.output, gap_threshold_s=args.gap_threshold_s)
    except (OSError, ValueError, ImportError) as error:
        parser.exit(2, f"error: {error}\n")
    for instance, stream in summary["imu"].items():
        print(f"IMU {instance}: {stream['samples']} records, {len(stream['gaps'])} gaps, "
              f"{len(stream['segments'])} finite timing segments")
    metadata = summary["metadata"]
    diagnostics, tail = metadata.get("parser_diagnostic_count", 0), metadata.get("unparsed_tail_bytes", 0)
    if diagnostics or tail:
        print(f"Decoder: {diagnostics} diagnostic lines, {tail} unparsed trailing bytes; inspect summary.json.")
    print(f"Audit written to {args.output}; no estimator accuracy claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
