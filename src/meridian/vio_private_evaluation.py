"""Optional-reference evaluation using the unchanged qualified numerical policy.

This child runs separately from the estimator. An empty reference transport CSV
represents absence explicitly; it is never presented as measured truth. A run can
have valid local-world estimates with unavailable aligned accuracy metrics.
"""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path

from . import vio_evaluation as evaluator
from .vio_investigation import build_investigation, _json_safe_times
from .vio_private_inputs import strict_json
from .vio_private_specification import (
    canonical, specification_sha256, summarize_private_interval,
    validate_private_investigation, PROFILE_ID,
)

MAX_OUTPUT_BYTES = 30 * 1024**2


def evaluate_private_request(request, estimate, reference, imu, native_status):
    """Evaluate full resolution and build an exact-frame investigation artifact."""
    if (type(request) is not dict or set(request) != {"schema_version", "specification", "input_manifest", "run_state"}
            or type(request["schema_version"]) is not int or request["schema_version"] != 2
            or request["run_state"] not in {"complete", "partial"}):
        raise ValueError("Unsupported private evaluation request.")
    spec, inputs = request["specification"], request["input_manifest"]
    digest = specification_sha256(spec)
    if inputs["specification_sha256"] != digest or inputs["input"] != spec["input"] or inputs["interval"] != spec["interval"]:
        raise ValueError("Private input manifest differs from the execution request.")
    if hashlib.sha256(Path(evaluator.__file__).read_bytes()).hexdigest() != spec["evaluation"]["evaluator_sha256"]:
        raise ValueError("Numerical evaluator source identity differs.")
    expected = [int(value) for value in inputs["camera_timestamps_ns"]]
    values = evaluator.evaluate_trajectory(estimate, reference, expected_timestamps_ns=expected,
        max_reference_gap_ns=spec["evaluation"]["maximum_reference_bracket_ns"],
        max_estimate_timestamp_error_ns=spec["evaluation"]["maximum_estimate_timestamp_error_ns"])
    legacy_request = {"schema_version": 1, "specification_sha256": digest,
        "dataset_manifest_sha256": spec["input"]["publication_manifest_sha256"],
        "dataset_id": spec["input"]["acquisition_contract_id"], "expected_timestamps_ns": expected,
        "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
        "reference_sha256": inputs["reference_sha256"], "imu_sha256": inputs["imu_sha256"],
        "license": spec["license"], "run_state": request["run_state"]}
    artifact = build_investigation(Path(estimate), Path(reference), Path(imu), request=legacy_request, evaluation=values, native_status=native_status)
    if not inputs["reference_available"]:
        if values["counts"]["reference_samples"] != 0:
            raise ValueError("Unavailable reference transport must not contain poses.")
        availability = {"status": "unavailable", "reason": inputs["reference_unavailable_reason"]}
    elif values["metrics"] is None:
        availability = {"status": "unavailable", "reason": values["status"]}
    elif values["reference_coverage_fraction"] < 1:
        availability = {"status": "partial", "reason": "partial_reference_coverage"}
    else:
        availability = {"status": "available", "reason": None}
    values["availability"] = availability
    artifact["schema_version"] = 2
    artifact["evaluation"] = deepcopy(availability)
    artifact["input"] = deepcopy(spec["input"]) | {"profile_id": PROFILE_ID, "clock_id": spec["clock_id"],
        "imu_frame_id": spec["imu"]["frame_id"], "processing": deepcopy(spec["processing"])}
    artifact["cameras"] = deepcopy(spec["cameras"])
    artifact["imu"]["source"] = "prepared_input"
    artifact["imu"]["bias_corrected"] = any(op["kind"] == "imu_calibration" and op["stream_id"] == spec["imu"]["stream_id"] for op in spec["processing"])
    artifact["summary"]["availability"] = deepcopy(availability)
    frame = spec["imu"]["frame_id"]
    artifact["coordinate_frames"].update({"estimate_raw": f"Prepared {frame} body to estimator local world",
        "estimate": f"Prepared {frame} body to reference world after full-selected-interval global SE3 alignment",
        "reference": f"Prepared {frame} body to declared reference world", "imu": f"Prepared {frame} measurement axes"})
    provenance = artifact["provenance"]
    provenance["private_builder_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    provenance["evaluation_sha256"] = hashlib.sha256(canonical(values).encode()).hexdigest()
    if not inputs["reference_available"]:
        provenance["reference_sha256"] = None
    artifact["full_interval"] = summarize_private_interval(artifact, str(expected[0]), str(expected[-1]))
    validate_private_investigation(artifact, specification=spec, input_manifest=inputs, evaluation=values,
        trajectory_sha256=hashlib.sha256(Path(estimate).read_bytes()).hexdigest())
    if len(canonical(values).encode()) + len(canonical(artifact).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("Private evaluation artifacts exceed the bounded publication allocation.")
    return {"evaluation": values, "investigation": artifact}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("request", "estimate", "reference", "imu", "native-status", "evaluation-output", "investigation-output"):
        parser.add_argument("--"+flag, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.request.stat().st_size > 2*1024**2: raise ValueError("Evaluation request is too large.")
        result = evaluate_private_request(strict_json(args.request.read_bytes()), args.estimate, args.reference, args.imu,
            strict_json(args.native_status.read_bytes()))
        for path, key in ((args.evaluation_output, "evaluation"), (args.investigation_output, "investigation")):
            with path.open("x", encoding="utf-8") as handle: handle.write(canonical(result[key])+"\n")
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"status": "failed", "reason": "Private evaluation input or output validation failed."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
