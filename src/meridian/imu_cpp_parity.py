"""Compare Python and C++ EKF operations on an explicitly selected DataFlash segment."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import subprocess

import numpy as np

from meridian import imu_replay
from meridian.cpp_parity import (
    QUANTITY_UNITS, TOLERANCES, VALUE_NAMES, _write_trace, compare_traces, python_trace,
    read_cpp_trace, replay_command, serialize_events,
)
from meridian.dataflash_audit import sha256_file
from meridian.replay import REPLAY_GRAVITY_M_S2, ReplayConfig


MEASUREMENT_HEADER = (
    "record_index", "instance", "segment_id", "time_us", "gyro_x_rad_s", "gyro_y_rad_s",
    "gyro_z_rad_s", "accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2",
)
ESTIMATE_HEADER = (
    "time_us", "elapsed_s", "ekf_roll_rad", "ekf_bias_rad_s", "p_angle_rad2",
    "p_angle_bias_rad2_s", "p_bias_rad2_s2",
)
INNOVATION_HEADER = (
    "time_us", "innovation_y_m_s2", "innovation_z_m_s2", "s_yy_m2_s4",
    "s_yz_m2_s4", "s_zz_m2_s4", "normalized_innovation_squared",
)
TIMELINE_HEADER = (
    "step", "operation", "endpoint_record_index", "endpoint_time_us",
    "input_record_index", "input_time_us",
)


def _read_rows(path: Path, header: tuple[str, ...]) -> list[dict[str, str]]:
    """Read a fresh replay export, refusing a changed layout or incomplete row."""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(header):
            raise ValueError(f"unexpected replay header: {path.name}")
        rows = list(reader)
    if any(set(row) != set(header) or any(value is None or value == "" for value in row.values())
           for row in rows):
        raise ValueError(f"invalid replay row: {path.name}")
    return rows


def _measurements(directory: Path) -> list[dict]:
    rows = _read_rows(directory / "measurements.csv", MEASUREMENT_HEADER)
    result = [{key: int(value) if key in MEASUREMENT_HEADER[:4] else float(value)
               for key, value in row.items()} for row in rows]
    if len(result) < 2:
        raise ValueError("snapshot comparison needs at least two measurements")
    if any(row["time_us"] < 0 or row["time_us"] > np.iinfo(np.int64).max
           or not all(math.isfinite(row[key]) for key in MEASUREMENT_HEADER[4:]) for row in result):
        raise ValueError("invalid measurement time or sensor value")
    if any(b["time_us"] <= a["time_us"] or b["record_index"] <= a["record_index"]
           for a, b in zip(result, result[1:])):
        raise ValueError("measurement times and record indices must increase")
    return result


def replay_events(directory: Path, summary: dict) -> tuple[dict, list[tuple[str, float, float]], list[dict]]:
    """Map freshly exported snapshots to the unchanged native event protocol."""
    rows = _measurements(directory)
    selected = summary["selection"]
    if len(rows) != selected["samples"] or any(
        row["instance"] != selected["instance"] or row["segment_id"] != selected["id"]
        for row in rows
    ):
        raise ValueError("measurements do not match the selected segment")
    config = ReplayConfig(**summary["config"])
    settings = {
        "initial_angle_rad": summary["initialization"]["angle_rad"],
        "initial_bias_rad_s": summary["initialization"]["bias_rad_s"],
        "initial_angle_std_rad": math.radians(config.angle_noise_std_deg),
        "initial_bias_std_rad_s": math.radians(config.initial_bias_std_deg_s),
        "gyro_noise_std_rad_s": math.radians(config.gyro_noise_std_deg_s),
        "accel_noise_std_m_s2": config.accel_noise_std_m_s2,
        "gravity_m_s2": REPLAY_GRAVITY_M_S2,
    }
    events = []
    timeline = []

    def record(operation: str, endpoint: dict, source: dict) -> None:
        timeline.append(dict(zip(TIMELINE_HEADER, (
            len(timeline), operation, endpoint["record_index"], endpoint["time_us"],
            source["record_index"], source["time_us"],
        ))))

    record("init", rows[0], rows[0])
    for previous, current in zip(rows, rows[1:]):
        # Subtract integer logger timestamps before conversion, including near int64 limits.
        dt = (current["time_us"] - previous["time_us"]) / 1_000_000
        if dt > config.max_gap_s:
            raise ValueError("measurement gap exceeds the replay threshold")
        events.append(("predict", previous["gyro_x_rad_s"], dt))
        record("predict", current, previous)
        events.append(("update", current["accel_y_m_s2"], current["accel_z_m_s2"]))
        record("update", current, current)
    return settings, events, timeline


def check_snapshot_link(directory: Path, operations: list[str], values: np.ndarray) -> dict:
    """Check event endpoints and prior innovations against the original snapshot replay."""
    measurements = _measurements(directory)
    estimates = _read_rows(directory / "ekf_estimates.csv", ESTIMATE_HEADER)
    innovations = _read_rows(directory / "ekf_innovations.csv", INNOVATION_HEADER)
    count = len(measurements)
    if (len(estimates) != count or len(innovations) != count - 1
            or operations != ["init"] + ["predict", "update"] * (count - 1)
            or values.shape != (2 * count - 1, len(VALUE_NAMES))):
        raise ValueError("snapshot/operation record count or order mismatch")
    times = [row["time_us"] for row in measurements]
    if ([int(row["time_us"]) for row in estimates] != times
            or [int(row["time_us"]) for row in innovations] != times[1:]):
        raise ValueError("snapshot export timestamp mismatch")
    reference = np.full((count, len(VALUE_NAMES)), np.nan)
    for k, row in enumerate(estimates):
        angle, bias, p00, p01, p11 = (float(row[key]) for key in ESTIMATE_HEADER[2:])
        # Snapshot CSVs store symmetric upper triangles; operation traces store all entries.
        reference[k, :6] = [angle, bias, p00, p01, p01, p11]
    for k, row in enumerate(innovations, start=1):
        v0, v1, s00, s01, s11 = (float(row[key]) for key in INNOVATION_HEADER[1:6])
        reference[k, 6:] = [v0, v1, s00, s01, s01, s11]
    return compare_traces(reference, values[::2])


def run_comparison(source: Path, binary: Path, output: Path, *, instance: int, segment: int,
                   config: ReplayConfig = ReplayConfig()) -> dict:
    """Retain the snapshot replay, shared operations, both traces, and agreement evidence."""
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    binary = Path(binary).resolve(strict=True)
    source_digest, binary_digest = sha256_file(source), sha256_file(binary)
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                             timeout=30, check=True)
    metadata = json.loads(version.stdout)
    if (not isinstance(metadata, dict) or type(metadata.get("protocol_version")) is not int
            or metadata["protocol_version"] != 1):
        raise ValueError("C++ binary must support event protocol version 1")
    directory = output / "python_replay"
    replay = imu_replay.run_replay(source, directory, instance=instance, segment=segment, config=config)
    settings, events, timeline = replay_events(directory, replay)
    serialized = serialize_events(events)
    operations, expected = python_trace(settings, serialized)
    linkage = check_snapshot_link(directory, operations, expected)
    event_path = output / "events.csv"
    event_path.write_text(serialized, encoding="utf-8")
    with (output / "timeline.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TIMELINE_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(timeline)
    _write_trace(output / "python_trace.csv", operations, expected)
    retained_digests = {name: sha256_file(output / name) for name in
                        ("timeline.csv", "python_trace.csv", "python_replay/summary.json")}
    cpp_path = output / "cpp_trace.csv"
    subprocess.run(replay_command(binary, settings, event_path, cpp_path), capture_output=True,
                   text=True, timeout=30, check=True)
    retained_digests["cpp_trace.csv"] = sha256_file(cpp_path)
    actual = read_cpp_trace(cpp_path, operations)
    comparison = compare_traces(expected, actual)
    if source_digest != replay["source"]["sha256"] or sha256_file(source) != source_digest:
        raise ValueError("source file changed during comparison")
    if sha256_file(binary) != binary_digest:
        raise ValueError("C++ binary changed during comparison")
    if event_path.read_bytes() != serialized.encode("utf-8"):
        raise ValueError("serialized events changed during comparison")
    for name, digest in retained_digests.items():
        if sha256_file(output / name) != digest:
            raise ValueError(f"comparison artifact changed during comparison: {name}")
    for name, digest in replay["artifact_sha256"].items():
        if sha256_file(directory / name) != digest:
            raise ValueError(f"snapshot artifact changed during comparison: {name}")
    summary = {
        "schema_version": 1, "experiment": "recorded_imu_cpp_python_ekf_parity",
        "data_source": "dataflash_imu_snapshots", "source": replay["source"],
        "binary": {**metadata, "sha256": binary_digest},
        "environment": replay["environment"], "metadata": replay["metadata"],
        "selection": replay["selection"], "configuration": settings,
        "sampling": replay["sampling"], "tolerances": TOLERANCES, "quantity_units": QUANTITY_UNITS,
        "comparison_rule": "abs(candidate-reference) <= atol + rtol * abs(reference)",
        "prediction_count": operations.count("predict"), "correction_count": operations.count("update"),
        "snapshot_link": linkage, "comparison": comparison,
        "passed": linkage["passed"] and comparison["passed"],
        "limitations": [
            "Numerical agreement does not establish physical accuracy or identify sensor noise.",
            "Both implementations decode the same serialized inputs; snapshot linkage checks init/update endpoints and prior innovations separately.",
            "Snapshot linkage reconstructs symmetric P/S entries; the full event comparison checks all four entries independently.",
            "TimeUS is logged time, not verified sensor acquisition time; prediction uses the previous snapshot hold.",
            "NIS is not a native protocol field; the comparison checks innovation and full S, not a native NIS output.",
            "Source conditions and integrity limits are retained in metadata and python_replay/summary.json.",
            "Offline desktop execution is not embedded, real-time, or flight validation.",
        ],
    }
    summary["artifact_sha256"] = {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*")) if path.is_file()
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="audited DataFlash .bin source")
    parser.add_argument("--binary", type=Path, required=True, help="native event replay executable")
    parser.add_argument("--instance", type=int, required=True)
    parser.add_argument("--segment", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new local output directory")
    parser.add_argument("--gyro-noise-std-deg-s", type=float, default=1.0)
    parser.add_argument("--angle-noise-std-deg", type=float, default=2.0)
    parser.add_argument("--initial-bias-std-deg-s", type=float, default=1.0)
    parser.add_argument("--complementary-tau-s", type=float, default=1.0)
    parser.add_argument("--max-gap-s", type=float, default=0.2)
    args = vars(parser.parse_args(argv))
    source, binary, output, instance, segment = (
        args.pop(key) for key in ("source", "binary", "output", "instance", "segment")
    )
    try:
        summary = run_comparison(source, binary, output, instance=instance, segment=segment,
                                 config=ReplayConfig(**args))
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        parser.exit(2, f"error: {error}\n")
    status = "PASS" if summary["passed"] else "FAIL"
    print(f"{status}: {summary['comparison']['record_count']} Python/C++ records; "
          f"snapshot linkage {'PASS' if summary['snapshot_link']['passed'] else 'FAIL'}.")
    print(f"Results written to {output}; numerical agreement is not physical accuracy.")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
