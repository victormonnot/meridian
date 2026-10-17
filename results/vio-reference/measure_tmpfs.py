"""Sample temporary filesystem occupancy in a separate, bounded resource probe.

Reproduce from the repository root with the project's Python environment:

    python results/vio-reference/measure_tmpfs.py \
        --prepared outputs/vio-reference-data/prepared \
        --baseline outputs/vio-reference/room1-run-1 \
        --output outputs/vio-reference/tmpfs-probe \
        --measurement results/vio-reference/tmpfs-measurement.json

Existing outputs are refused. This probe adds monitoring processes and is not
one of the three baseline timing/repetition runs. No reference poses are mounted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import uuid

from meridian.vio_dataset import verify_prepared
from meridian.vio_evaluation import read_trajectory
from meridian.vio_reference import (
    _digest, _host, _image_identity, _json, _limits, _parse_json,
    _runtime_version, _sha, _validate_native_status, container_command,
)


EXPECTED_TRAJECTORY_SHA256 = "d2a59ed328807d54af0a930006b7c2e850998c2dc7d6f89debd1c195ca4ed764"
SAMPLE_PERIOD_SECONDS = 0.2
# Fixed trusted script, passed as one subprocess argument without interpolation.
# Samples and logs go to /output, so the probe itself does not write to /tmp.
SHELL_SCRIPT = r"""set -u
sample() {
    timestamp=$(date +%s%N) || return 1
    usage=$(df --block-size=1 --output=size,used /tmp) || return 1
    printf '%s ' "$timestamp"
    printf '%s\n' "$usage" | tail -n 1
}
printf '#timestamp_ns size_bytes used_bytes\n' > /output/tmpfs-samples.txt
sample >> /output/tmpfs-samples.txt || exit 90
/usr/local/bin/meridian-openvins --dataset /sensors --config /config/estimator_config.yaml --output /output > /output/runner.log 2>&1 &
native_pid=$!
while kill -0 "$native_pid" 2>/dev/null; do
    sample >> /output/tmpfs-samples.txt || exit 90
    sleep 0.2
done
wait "$native_pid"
native_status=$?
sample >> /output/tmpfs-samples.txt || exit 90
exit "$native_status"
"""


def summarize_samples(path: Path, cap_bytes: int) -> dict:
    samples = []
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3 or any(not value.isdecimal() for value in fields):
            raise ValueError("malformed tmpfs observation")
        timestamp, size, used = map(int, fields)
        if size != cap_bytes or not 0 <= used <= size:
            raise ValueError("unexpected tmpfs allocation or usage")
        if samples and timestamp <= samples[-1][0]:
            raise ValueError("non-increasing probe timestamps")
        samples.append((timestamp, size, used))
    if len(samples) < 2:
        raise ValueError("insufficient tmpfs observations")
    intervals = [(b[0] - a[0]) * 1e-9 for a, b in zip(samples, samples[1:])]
    return {
        "sample_count": len(samples),
        "requested_sample_period_seconds": SAMPLE_PERIOD_SECONDS,
        "observed_interval_seconds": {
            "minimum": min(intervals), "median": statistics.median(intervals), "maximum": max(intervals),
        },
        "observed_duration_seconds": (samples[-1][0] - samples[0][0]) * 1e-9,
        "allocated_cap_bytes": cap_bytes,
        "observed_initial_used_bytes": samples[0][2],
        "observed_maximum_used_bytes": max(row[2] for row in samples),
        "observed_final_used_bytes": samples[-1][2],
    }


def probe(prepared: Path, baseline: Path, output: Path, measurement: Path) -> dict:
    prepared, baseline = prepared.resolve(strict=True), baseline.resolve(strict=True)
    output, measurement = output.resolve(), measurement.resolve()
    if output.exists() or measurement.exists():
        raise FileExistsError("probe output and published measurement must not already exist")
    if output.is_relative_to(prepared) or output.is_relative_to(baseline):
        raise ValueError("probe output must be separate from prepared inputs and baseline")
    manifest = verify_prepared(prepared)
    spec = _parse_json((baseline / "run-spec.json").read_text())
    result = _parse_json((baseline / "result.json").read_text())
    if result.get("state") != "complete" or result.get("run_spec_sha256") != _digest(spec):
        raise ValueError("baseline is not a completed, intact specification")
    if _sha(baseline / "trajectory.csv") != EXPECTED_TRAJECTORY_SHA256:
        raise ValueError("baseline trajectory differs from the qualified room1 result")
    manifest_hash = _sha(prepared / "manifest.json")
    if manifest_hash != spec["dataset_manifest_sha256"]:
        raise ValueError("prepared inputs differ from the baseline")
    limits = spec["limits"]
    if limits != _limits(2.0, 4096, 900.0):
        raise ValueError("baseline resource limits differ from the qualified protocol")
    image = _image_identity(spec["runtime_image_id"])
    version = _runtime_version(image, limits)
    if image != spec["runtime_image_id"] or version != spec["runtime_version"]:
        raise ValueError("runtime differs from the qualified baseline")
    output.mkdir(parents=True)
    name = f"meridian-tmpfs-probe-{uuid.uuid4().hex}"
    command = container_command(prepared, output, image, name, limits)
    boundary = command.index(image)
    command = command[:boundary] + ["--entrypoint", "/bin/sh", image, "-c", SHELL_SCRIPT]
    (output / "monitor.sh").write_text(SHELL_SCRIPT)
    report = {
        "schema_version": 1,
        "kind": "supplemental_tmpfs_resource_probe",
        "state": "failed",
        "method": "df --block-size=1 --output=size,used /tmp, before/during/after native execution",
        "limitations": [
            "Sampled maximum occupancy can miss transients shorter than the observation interval.",
            "Monitoring adds shell, date, df, tail and sleep overhead; timings are not baseline performance measurements.",
            "Only /tmp occupancy is measured; the /output bind mount and filesystem cache are excluded.",
        ],
        "provenance": {
            "runtime_image_id": image, "runtime_version": version,
            "baseline_run_spec_sha256": _digest(spec),
            "prepared_manifest_sha256": manifest_hash,
            "configuration_files": {key: value for key, value in manifest["files"].items()
                                    if key.startswith("config/")},
            "probe_source_sha256": _sha(Path(__file__)),
            "shell_script_sha256": hashlib.sha256(SHELL_SCRIPT.encode()).hexdigest(),
            "runtime_limits": limits, "host": _host(),
        },
    }
    started = time.monotonic()
    try:
        with (output / "probe.log").open("wb") as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=limits["timeout_s"])
        report["container_wall_seconds_including_probe"] = time.monotonic() - started
        report["native_exit_code"] = process.returncode
        if process.returncode != 0:
            raise ValueError("native/probe execution failed; inspect probe.log and runner.log")
        status = _parse_json((output / "status.json").read_text())
        _validate_native_status(status, read_trajectory(output / "trajectory.csv"))
        trajectory_hash = _sha(output / "trajectory.csv")
        if trajectory_hash != EXPECTED_TRAJECTORY_SHA256:
            raise ValueError("probe trajectory differs from the three baseline runs")
        verify_prepared(prepared)
        if _sha(prepared / "manifest.json") != manifest_hash:
            raise ValueError("prepared inputs changed during the probe")
        report["measurement"] = summarize_samples(output / "tmpfs-samples.txt", limits["tmp_mb"] * 1024**2)
        report["native_status"] = status
        report["trajectory_sha256"] = trajectory_hash
        report["baseline_trajectory_byte_identical"] = True
        report["artifacts"] = {path.name: {"sha256": _sha(path), "size_bytes": path.stat().st_size}
                               for path in sorted(output.iterdir()) if path.is_file()}
        report["state"] = "complete"
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
        _json(output / "probe-result.json", report)
    publication = {**report, "probe_result_sha256": _sha(output / "probe-result.json")}
    measurement.parent.mkdir(parents=True, exist_ok=True)
    _json(measurement, publication)
    return publication


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.prepared, args.baseline, args.output, args.measurement)
    print(json.dumps(result["measurement"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
