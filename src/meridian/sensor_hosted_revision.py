"""Prepare a selected calibration/time revision in the isolated import sandbox.

The sole mounted recording is read-only. Artifacts are strict contract JSON,
never executable configuration, and preparation never invokes an estimator.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import stat

from .sensor_contract import contract_sha256, loads_contract
from .sensor_hosted_import import HOSTED_LIMITS, canonical, write_json
from .sensor_import import prepare_package
from .sensor_package import file_identity


MAX_REQUEST_BYTES = 2 * 1024**2
MAX_RESULT_BYTES = 2 * 1024**3
MAX_RESULT_FILES = 29_999  # The parent also reserves the publication manifest.
MAX_DESCRIPTOR_BYTES = 64 * 1024
LIMITS = replace(HOSTED_LIMITS, max_total_bytes=MAX_RESULT_BYTES,
                 max_file_bytes=MAX_RESULT_BYTES, max_members=MAX_RESULT_FILES)
REQUEST_KEYS = {"prepared_id", "source_identity", "calibration", "clock_mappings",
                "dependency_acquisitions", "apply_imu_intrinsics", "imu_target_frame",
                "imu_stream_id", "include_reference"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}")


def _contract(text, kind):
    maximum = MAX_REQUEST_BYTES if kind == "acquisition" else MAX_DESCRIPTOR_BYTES
    if type(text) is not str or len(text.encode("utf-8")) > maximum:
        raise ValueError("Revision artifact exceeds its descriptor byte limit.")
    document = loads_contract(text)
    if document["kind"] != kind:
        raise ValueError("Unexpected revision artifact kind.")
    return document


def validate_request(request):
    if (type(request) is not dict or set(request) != REQUEST_KEYS
            or type(request["prepared_id"]) is not str or not ID.fullmatch(request["prepared_id"])
            or any(type(request[key]) is not bool for key in ("apply_imu_intrinsics", "include_reference"))
            or any(request[key] is not None and (type(request[key]) is not str or not ID.fullmatch(request[key]))
                   for key in ("imu_stream_id", "imu_target_frame"))
            or type(request["source_identity"]) is not dict
            or set(request["source_identity"]) != {"id", "sha256"}
            or type(request["source_identity"]["id"]) is not str
            or not ID.fullmatch(request["source_identity"]["id"])
            or type(request["source_identity"]["sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", request["source_identity"]["sha256"])):
        raise ValueError("Unsupported revision request.")
    if len(canonical(request)) > MAX_REQUEST_BYTES:
        raise ValueError("Revision request exceeds its byte limit.")
    for key, maximum in (("clock_mappings", 16), ("dependency_acquisitions", 16)):
        if type(request[key]) is not list or len(request[key]) > maximum:
            raise ValueError("Revision artifact count exceeds its limit.")
    return {
        "calibration": None if request["calibration"] is None else _contract(request["calibration"], "calibration"),
        "clock_mappings": [_contract(value, "clock_mapping") for value in request["clock_mappings"]],
        "dependency_acquisitions": [_contract(value, "acquisition") for value in request["dependency_acquisitions"]],
    }


def result_inventory(root):
    files, total = {}, 0
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Revision output contains a link or special file.")
        name = path.relative_to(root).as_posix()
        if (len(name.encode("utf-8")) > 256
                or len(json.dumps(name, ensure_ascii=False).encode("utf-8")) - 2 > 256
                or name == "publication-manifest.json" or len(files) >= MAX_RESULT_FILES):
            raise ValueError("Revision output has an unsupported member.")
        identity = file_identity(path, limits=LIMITS)
        files[name] = identity
        total += identity["size_bytes"]
        if total > MAX_RESULT_BYTES:
            raise ValueError("Revision output exceeds its storage budget.")
    manifest = {"schema_version": 1, "files": files}
    # Reserve metadata for the parent publication marker as well.
    if total + len(canonical(manifest)) + 1 > MAX_RESULT_BYTES:
        raise ValueError("Revision output exceeds its storage budget.")
    return manifest


def run_revision(source, output, request):
    artifacts = validate_request(request)
    source, output = Path(source), Path(output)
    descriptor = os.open(source / "acquisition.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > HOSTED_LIMITS.max_descriptor_bytes):
            raise ValueError("Invalid source descriptor.")
        original = handle.read(HOSTED_LIMITS.max_descriptor_bytes + 1)
    acquisition = loads_contract(original)
    if {"id": acquisition["id"], "sha256": contract_sha256(acquisition)} != request["source_identity"]:
        raise ValueError("Source acquisition identity changed.")
    output.mkdir(mode=0o700)
    prepared = prepare_package(source, output / "result", prepared_id=request["prepared_id"],
        imu_stream=request["imu_stream_id"], include_reference=request["include_reference"],
        apply_imu_intrinsics=request["apply_imu_intrinsics"], imu_target_frame=request["imu_target_frame"],
        limits=HOSTED_LIMITS, output_limits=LIMITS, **artifacts)
    result = prepared["report"]
    outcome = {"state": "ready", "prepared_input": prepared["prepared_input"],
               "manifest": result_inventory(output / "result"), "result": result}
    write_json(output / "completion.json", outcome)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.request.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("Revision request exceeds its byte limit.")
        run_revision(args.source, args.output, json.loads(args.request.read_bytes()))
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "rejected", "code": "invalid_package",
                          "exception": type(error).__name__}), flush=True)
        return 2
    print(json.dumps({"status": "completed"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
