"""Isolated hosted evaluation entry point; numerical protocol remains unchanged."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from meridian.vio_evaluation import evaluate_trajectory


def evaluate_request(request: dict, estimate: Path, reference: Path) -> dict:
    """Validate the worker's explicit protocol before reading independent inputs."""
    required = {"schema_version", "expected_timestamps_ns", "max_reference_gap_ns",
                "max_estimate_timestamp_error_ns", "evaluator_sha256"}
    if type(request) is not dict or set(request) != required:
        raise ValueError("Unsupported evaluation request fields.")
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ValueError("Unsupported evaluation request schema.")
    implementation = Path(__file__).with_name("vio_evaluation.py")
    if hashlib.sha256(implementation.read_bytes()).hexdigest() != request["evaluator_sha256"]:
        raise ValueError("The evaluation image does not contain the pinned evaluator.")
    # The evaluator enforces integer nanoseconds, ordering, bounds and finite poses.
    return evaluate_trajectory(
        estimate, reference, expected_timestamps_ns=request["expected_timestamps_ns"],
        max_reference_gap_ns=request["max_reference_gap_ns"],
        max_estimate_timestamp_error_ns=request["max_estimate_timestamp_error_ns"],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estimate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--imu", type=Path)
    parser.add_argument("--native-status", type=Path)
    parser.add_argument("--investigation-request", type=Path)
    parser.add_argument("--investigation-output", type=Path)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        evaluation = evaluate_request(request, args.estimate, args.reference)
        investigation_arguments = (args.imu, args.native_status, args.investigation_request, args.investigation_output)
        if any(investigation_arguments):
            if not all(investigation_arguments):
                raise ValueError("Investigation requires all explicit input and output paths.")
            from meridian.vio_investigation import build_investigation
            investigation = build_investigation(
                args.estimate, args.reference, args.imu,
                request=json.loads(args.investigation_request.read_text(encoding="utf-8")),
                evaluation=evaluation, native_status=json.loads(args.native_status.read_text(encoding="utf-8")))
            temporary = args.investigation_output.with_name(".investigation.json.tmp")
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(investigation, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.write("\n")
            os.replace(temporary, args.investigation_output)
        temporary = args.output.with_name(".evaluation.json.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(evaluation, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, args.output)
    except (ValueError, OSError, TypeError, KeyError) as error:
        # This child has no database credentials; keep diagnostics independent
        # of the worker's local absolute paths and environment.
        print(json.dumps({"schema_version": 1, "error_type": type(error).__name__,
                          "message": "Independent trajectory evaluation failed."}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
