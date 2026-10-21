"""Bounded import/preparation entry point for a network-isolated child container.

This process receives one immutable Meridian transfer archive and no database,
Docker socket, account credentials or estimator. The orchestrator owns lifecycle
and publication; completion.json is a private handoff, never a ready-state flag.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import shutil
import stat

from .sensor_contract import contract_sha256
from .sensor_import import prepare_package
from .sensor_inspection import InspectionLimitError, write_inspection
from .sensor_package import PackageLimits, file_identity, materialize_package


GIB = 1024**3
MAX_UPLOAD_BYTES = 4 * GIB
MAX_ACQUISITION_BYTES = 4 * GIB
MAX_PREPARED_BYTES = 2 * GIB
MAX_RESULT_BYTES = 6 * GIB
MAX_INSPECTION_BYTES = 512 * 1024**2
MAX_RESULT_FILES = 59_998  # Retention also reserves original + publication manifest.
MAX_HANDOFF_BYTES = 32 * 1024**2
HOSTED_LIMITS = PackageLimits(max_total_bytes=MAX_ACQUISITION_BYTES,
                             max_file_bytes=MAX_UPLOAD_BYTES,
                             max_members=30_000, max_csv_rows=100_000)
PREPARED_LIMITS = replace(HOSTED_LIMITS, max_total_bytes=MAX_PREPARED_BYTES,
                        max_file_bytes=MAX_PREPARED_BYTES)
HOSTING_POLICY = {
    "schema_version": 1, "upload_bytes": MAX_UPLOAD_BYTES,
    "acquisition_bytes": MAX_ACQUISITION_BYTES, "prepared_bytes": MAX_PREPARED_BYTES,
    "result_bytes": MAX_RESULT_BYTES, "archive_members": 30_000, "rows": 100_000,
    "scratch_bytes": 8 * GIB, "cpus": 1, "memory_mb": 1024, "pids": 64,
    "timeout_seconds": 900, "network": "none", "image_encoding": "preserve",
    "inspection_bytes": MAX_INSPECTION_BYTES,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def write_json(path, value):
    encoded = canonical(value) + b"\n"
    if len(encoded) > MAX_HANDOFF_BYTES:
        raise ValueError("Import metadata exceeds the handoff byte limit.")
    with path.open("xb") as handle:
        handle.write(encoded)


def result_inventory(root):
    """Hash all regular output files with branch and aggregate storage limits."""
    files, totals = {}, {"acquisition": 0, "prepared": 0, "inspection": 0, "report.json": 0}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Import output contains a link or special file.")
        name = path.relative_to(root).as_posix()
        if (len(name.encode("utf-8")) > 256
                or len(json.dumps(name, ensure_ascii=False).encode("utf-8")) - 2 > 256):
            raise ValueError("Hosted output paths exceed 256 UTF-8 or JSON-serialized bytes.")
        branch = name.split("/", 1)[0]
        if branch not in totals or len(files) >= MAX_RESULT_FILES:
            raise ValueError("Import output has an unsupported member.")
        identity = file_identity(path, limits=HOSTED_LIMITS)
        files[name] = identity
        totals[branch] += identity["size_bytes"]
        if (totals["acquisition"] > MAX_ACQUISITION_BYTES
                or totals["prepared"] > MAX_PREPARED_BYTES
                or totals["inspection"] > MAX_INSPECTION_BYTES
                or totals["report.json"] > MAX_HANDOFF_BYTES
                or sum(totals.values()) > MAX_RESULT_BYTES):
            raise ValueError("Import output exceeds its storage budget.")
    return {"schema_version": 1, "files": files}


def run_import(source, output, request):
    """Validate and prepare one transfer archive, retaining it only at its source."""
    if (type(request) is not dict or set(request) != {"prepared_id", "imu_stream_id", "include_reference"}
            or type(request["include_reference"]) is not bool
            or type(request["prepared_id"]) is not str
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,79}", request["prepared_id"]) is None
            or (request["imu_stream_id"] is not None and (
                type(request["imu_stream_id"]) is not str
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,79}", request["imu_stream_id"]) is None))):
        raise ValueError("Unsupported hosted preparation request.")
    source, output = Path(source), Path(output)
    if source.is_dir():
        raise ValueError("Hosted imports require a transfer archive.")
    original = file_identity(source, limits=HOSTED_LIMITS)
    output.mkdir(mode=0o700)
    result_root = output / "result"
    result_root.mkdir(mode=0o700)
    imported = materialize_package(source, result_root / "acquisition", limits=HOSTED_LIMITS)
    prepared = prepare_package(
        result_root / "acquisition", result_root / "prepared",
        prepared_id=request["prepared_id"], imu_stream=request["imu_stream_id"],
        include_reference=request["include_reference"], limits=HOSTED_LIMITS,
        output_limits=PREPARED_LIMITS,
    )
    # Inspection is derived evidence, independently bounded within the existing
    # reservation. Exhaustion does not turn a valid sensor package into bad data.
    existing = [path for path in result_root.rglob("*") if path.is_file()]
    inspection_budget = min(MAX_INSPECTION_BYTES,
                            MAX_RESULT_BYTES - sum(path.stat().st_size for path in existing) - MAX_HANDOFF_BYTES)
    inspection_root = result_root / "inspection"
    try:
        if inspection_budget <= 0:
            raise InspectionLimitError("No remaining inspection storage budget.")
        summary = write_inspection(result_root / "acquisition", inspection_root,
                                   validated=imported, limits=HOSTED_LIMITS,
                                   max_bytes=inspection_budget)
        if len(existing) + sum(path.is_file() for path in inspection_root.rglob("*")) + 1 > MAX_RESULT_FILES:
            raise InspectionLimitError("Inspection exceeds the reserved file count.")
        inspection = {"status": "available", "method_version": summary["method_version"],
                      "summary_path": "inspection/summary.json"}
    except InspectionLimitError:
        if inspection_root.exists():
            shutil.rmtree(inspection_root)
        inspection = {"status": "unavailable", "reason": "inspection_resource_limit"}
    if file_identity(source, limits=HOSTED_LIMITS) != original:
        raise ValueError("Uploaded archive changed during import.")
    descriptor = imported["acquisition"]
    report = {
        "schema_version": 1, "status": "ready", "source": original,
        "acquisition": {"id": descriptor["id"], "sha256": contract_sha256(descriptor)},
        "prepared_input": {"id": prepared["prepared_input"]["id"],
                           "sha256": contract_sha256(prepared["prepared_input"])},
        "streams": [{"id": name, "kind": value["kind"], "samples": value["samples"],
                     "segments": len(value["segments"])}
                    for name, value in imported["report"]["streams"].items()],
        "reference_availability": imported["report"]["reference_availability"],
        "execution_readiness": "not_assessed", "calibration_validity": "not_assessed",
        "hosting_policy": HOSTING_POLICY,
        "inspection": inspection,
    }
    write_json(result_root / "report.json", report)
    manifest = result_inventory(result_root)
    outcome = {"state": "ready", "descriptor": descriptor, "result": report,
               "manifest": manifest, "sha256": original["sha256"]}
    write_json(output / "completion.json", outcome)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.request.stat().st_size > 8192:
            raise ValueError("Import request exceeds its byte limit.")
        request = json.loads(args.request.read_bytes())
        run_import(args.source, args.output, request)
    except (ValueError, OSError) as error:
        # Raw decoder errors, source paths and uploaded metadata stay private.
        print(json.dumps({"status": "rejected", "code": "invalid_package",
                          "exception": type(error).__name__}), flush=True)
        return 2
    print(json.dumps({"status": "completed"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
