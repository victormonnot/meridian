"""Fenced private stereo execution using the maintained native image and supervisor."""
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import subprocess
import time

from meridian import vio_reference as reference
from meridian.vio_evaluation import read_trajectory
from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from meridian.vio_private_inputs import prepare_private_inputs, strict_json
from meridian.vio_private_specification import (
    canonical, specification_sha256, validate_private_specification, validate_private_investigation,
)
from meridian.vio_runtime_identity import resolve_runtime_identity
from .job_execution import (
    _Controller, _check_directory, _identity, cleanup_attempt, container_names, verify_scratch,
    ExecutionFailure, HOSTING_POLICY, MAX_ARTIFACT_BYTES, _MESSAGES,
)


def artifact_names(spec):
    return {"trajectory.csv", "status.json", "evaluation.json", "investigation.json", "image-index.json",
        "experiment-specification.json", "input-manifest.json", "run-spec.json", "runner.log", "evaluation.log",
        "result.json", "hosting-policy.json", "binary-sha256.txt", "dependency-versions.txt"} | {
            "config-" + name for name in spec["configuration"]["files"]}


def verify_fixed_metadata(directory, expected):
    """The native output mount cannot redefine host-generated input provenance."""
    for name, identity in expected.items():
        path = directory / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != identity["size_bytes"]
                or reference._sha(path) != identity["sha256"]):
            raise ExecutionFailure("invalid_input")


def manifest(directory, spec, *, complete, investigation_validated=False, fixed_metadata=None):
    allowed = artifact_names(spec)
    if not directory.exists():
        return {"schema_version": 1, "files": {}}
    _check_directory(directory, allowed)
    verify_fixed_metadata(directory, fixed_metadata or {})
    names = {p.name for p in directory.iterdir()}
    if complete and names != allowed:
        raise ExecutionFailure("numerical_failure")
    if not complete:
        names -= {"trajectory.csv", "evaluation.json"}
        if not investigation_validated:
            names -= {"investigation.json", "image-index.json"}
    return {"schema_version": 1, "files": {name: {
        "size_bytes": (directory / name).stat().st_size, "sha256": reference._sha(directory / name),
    } for name in sorted(names)}}


def native_command(spec, prepared, output, image_id, camera_mounts, name):
    command = reference.container_command(prepared, output, image_id, name, spec["resources"],
                                          configuration=prepared / "config")
    index = command.index(image_id)
    additions = []
    for camera in camera_mounts:
        if camera["slot"] not in {"cam0", "cam1"} or camera["target"] != f'/sensors/mav0/{camera["slot"]}/data':
            raise ExecutionFailure("invalid_input")
        path = Path(camera["source_path"])
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
            raise ExecutionFailure("invalid_input")
        additions += reference._mount(path, camera["target"], readonly=True)
    return command[:index] + additions + command[index:]


class PrivateController(_Controller):
    def __init__(self, specification, staging, attempt_id, checkpoint, evaluation_image, configuration_parent):
        self.specification, self.staging, self.attempt_id = specification, staging, attempt_id
        self.names = container_names(attempt_id)
        self.container_name, self.callback = self.names["native"], checkpoint
        self.evaluation_image, self.configuration_parent = evaluation_image, configuration_parent
        self.phase, self.allowed = "preparing", artifact_names(specification)
        self.investigation_validated, self.investigation_run_state = False, None
        self.fixed_metadata = {}

    def evaluate_private(self, handoff, *, run_state):
        verify_fixed_metadata(self.staging, self.fixed_metadata)
        self.checkpoint("evaluating", {})
        request = {"schema_version": 2, "specification": self.specification,
                   "input_manifest": handoff["input_manifest"], "run_state": run_state}
        request_path = self.configuration_parent / "private-evaluation-request.json"
        reference._json(request_path, request)
        if request_path.stat().st_size > 2 * 1024**2:
            raise ExecutionFailure("resource_exhaustion")
        destination = self.configuration_parent / "private-evaluation-output"
        destination.mkdir()
        limits = {"cpus": 0.5, "memory_mb": 512, "pids": 64, "tmp_mb": 32}
        command = ["docker", "run", *reference._container_options(limits), "--entrypoint", "python"]
        mounts = ((self.staging / "trajectory.csv", "/inputs/estimate.csv"),
                  (Path(handoff["evaluation"]["reference_path"]), "/inputs/reference.csv"),
                  (Path(handoff["evaluation"]["imu_path"]), "/inputs/imu.csv"),
                  (self.staging / "status.json", "/inputs/native-status.json"),
                  (request_path, "/inputs/request.json"))
        for source, target in mounts:
            command += reference._mount(source, target, readonly=True)
        command += reference._mount(destination, "/evaluation", readonly=False)
        command += [self.evaluation_image, "-m", "meridian.vio_private_evaluation",
            "--request", "/inputs/request.json", "--estimate", "/inputs/estimate.csv",
            "--reference", "/inputs/reference.csv", "--imu", "/inputs/imu.csv",
            "--native-status", "/inputs/native-status.json", "--evaluation-output", "/evaluation/evaluation.json",
            "--investigation-output", "/evaluation/investigation.json"]
        code, _ = self._supervise(command, "evaluation", 120, self.staging / "evaluation.log")
        if code:
            raise ExecutionFailure("numerical_failure")
        _check_directory(destination, {"evaluation.json", "investigation.json"})
        values = {}
        for name in ("evaluation.json", "investigation.json"):
            path = destination / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ExecutionFailure("numerical_failure")
            values[name] = strict_json(path.read_bytes())
        validate_evaluation(values["evaluation.json"], self.specification, handoff["input_manifest"],
                            len(read_trajectory(self.staging / "trajectory.csv").timestamp_ns))
        validate_private_investigation(values["investigation.json"], specification=self.specification,
            input_manifest=handoff["input_manifest"], evaluation=values["evaluation.json"],
            trajectory_sha256=reference._sha(self.staging / "trajectory.csv"))
        if values["investigation.json"]["run_state"] != run_state:
            raise ExecutionFailure("numerical_failure")
        for name in values:
            os.replace(destination / name, self.staging / name)
        reference._json(self.staging / "image-index.json", handoff["image_index"])
        _check_directory(self.staging, self.allowed)
        self.investigation_validated, self.investigation_run_state = True, run_state
        return values["evaluation.json"]


def validate_evaluation(value, spec, inputs, estimate_count):
    protocol = value.get("protocol", {})
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or type(protocol.get("version")) is not int or protocol["version"] != 1
            or protocol.get("name") != "meridian-vio-se3"
            or protocol.get("max_reference_gap_ns") != spec["evaluation"]["maximum_reference_bracket_ns"]
            or protocol.get("max_estimate_timestamp_error_ns") != spec["evaluation"]["maximum_estimate_timestamp_error_ns"]
            or protocol.get("scale_fitted") is not False or protocol.get("time_offset_fitted") is not False
            or protocol.get("estimate_interpolation") is not False):
        raise ExecutionFailure("numerical_failure")
    counts = value.get("counts", {})
    expected = inputs["camera_pairs"]
    keys = {"expected_camera_samples", "estimate_samples", "reference_samples", "reference_supported_camera_samples",
            "evaluated_camera_samples", "missing_initial_camera_samples", "missing_interior_camera_samples", "missing_trailing_camera_samples"}
    if (set(counts) != keys or any(type(v) is not int or v < 0 for v in counts.values())
            or counts["expected_camera_samples"] != expected or counts["estimate_samples"] != estimate_count
            or estimate_count > expected or counts["reference_supported_camera_samples"] > expected
            or counts["evaluated_camera_samples"] > min(estimate_count, counts["reference_supported_camera_samples"])
            or sum(counts[k] for k in ("missing_initial_camera_samples", "missing_interior_camera_samples", "missing_trailing_camera_samples")) != expected-estimate_count):
        raise ExecutionFailure("numerical_failure")
    for key, count in (("coverage_fraction", "estimate_samples"), ("evaluated_fraction", "evaluated_camera_samples"),
                       ("reference_coverage_fraction", "reference_supported_camera_samples")):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or not math.isclose(number, counts[count]/expected, abs_tol=1e-12):
            raise ExecutionFailure("numerical_failure")
    status, metrics = value.get("status"), value.get("metrics")
    if status not in {"ok", "insufficient_associations", "degenerate_alignment"}:
        raise ExecutionFailure("numerical_failure")
    if status == "ok":
        keys = {"ate_rmse_m", "position_error_mean_m", "position_error_max_m", "orientation_rmse_rad",
                "orientation_rmse_deg", "orientation_error_mean_deg", "orientation_error_max_deg"}
        if type(metrics) is not dict or set(metrics) != keys or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in metrics.values()):
            raise ExecutionFailure("numerical_failure")
    elif metrics is not None:
        raise ExecutionFailure("numerical_failure")
    availability = ({"status": "unavailable", "reason": inputs["reference_unavailable_reason"]}
                    if not inputs["reference_available"] else
                    {"status": "unavailable", "reason": status} if metrics is None else
                    {"status": "partial", "reason": "partial_reference_coverage"}
                    if value["reference_coverage_fraction"] < 1 else {"status": "available", "reason": None})
    if value.get("availability") != availability or (not inputs["reference_available"] and
            (counts["reference_samples"] or counts["evaluated_camera_samples"] or metrics is not None)):
        raise ExecutionFailure("numerical_failure")
    canonical(value)


def validate_native(status, trajectory, inputs):
    reference._validate_native_status(status, trajectory)
    counts = status.get("counts", {})
    keys = {"imu_input", "cam0_input", "cam1_input", "imu_processed", "camera_pairs_processed",
            "camera_pairs_without_imu_bracket", "trajectory_states"}
    if (status.get("upstream_revision") != reference.UPSTREAM_REVISION
            or status.get("orientation") != "Hamilton xyzw, IMU-body to local-world"
            or status.get("ground_truth_access") != "no reference input or initialization"
            or set(counts) != keys or any(type(v) is not int or v < 0 for v in counts.values())
            or counts["cam0_input"] != inputs["camera_pairs"] or counts["cam1_input"] != inputs["camera_pairs"]
            or counts["imu_input"] != inputs["imu_samples"] or counts["imu_processed"] > counts["imu_input"]
            or counts["camera_pairs_without_imu_bracket"] != 0 or counts["camera_pairs_processed"] != inputs["camera_pairs"]):
        raise ExecutionFailure("numerical_failure")
    for key, expected in (("first_camera_timestamp_ns", inputs["camera_timestamps_ns"][0]),
                           ("last_camera_timestamp_ns", inputs["camera_timestamps_ns"][-1])):
        if type(status.get(key)) is not int or str(status[key]) != expected:
            raise ExecutionFailure("numerical_failure")


def execute_private_attempt(specification, prepared, staging, *, attempt_id, checkpoint,
                            evaluation_image, configuration_parent):
    """Read only the bound publication; never publish or mutate durable job state."""
    controller, handoff, failure, result = None, None, None, None
    state, valid = "failed", False
    staging, parent = Path(staging), Path(configuration_parent)
    _identity(attempt_id)
    if staging.exists() or staging.is_symlink():
        raise ValueError("Attempt staging already exists.")
    try:
        validate_private_specification(specification)
        valid = True
        if parent.resolve(strict=True) != parent or staging.parent != parent or staging.name != "run":
            raise ExecutionFailure("infrastructure_failure")
        verify_scratch(parent)
        if re.fullmatch(r"sha256:[a-f0-9]{64}", evaluation_image) is None:
            raise ExecutionFailure("infrastructure_failure")
        controller = PrivateController(specification, staging, attempt_id, checkpoint, evaluation_image, parent)
        controller.checkpoint("preparing", {})
        stage_inputs = parent / "private-inputs"
        handoff = prepare_private_inputs(specification, prepared, stage_inputs,
                                         checkpoint=lambda: controller.checkpoint("preparing", {}))
        runtime = resolve_runtime_identity(specification["runtime"]["image_id"], specification["runtime"]["image_id"], reference._image_identity)
        image_id = runtime["execution_image_id"]
        version = reference._runtime_version(image_id, specification["resources"], controller=controller)
        provenance = reference._runtime_provenance(image_id, specification["resources"], controller=controller)
        if (canonical(version) != canonical(specification["runtime"]["version"])
                or {name: sha256(text.encode()).hexdigest() for name, text in provenance.items()}
                != specification["runtime"]["provenance_sha256"]):
            raise ExecutionFailure("invalid_input")
        staging.mkdir()
        reference._json(staging / "experiment-specification.json", specification)
        reference._json(staging / "input-manifest.json", handoff["input_manifest"])
        reference._json(staging / "run-spec.json", {"schema_version": 2,
            "experiment_specification_sha256": specification_sha256(specification), "runtime_identity": runtime,
            "runtime_version": version, "input": specification["input"], "limits": specification["resources"]})
        for name, identity in specification["configuration"]["files"].items():
            content = (stage_inputs / "config" / name).read_bytes()
            if len(content) != identity["size_bytes"] or sha256(content).hexdigest() != identity["sha256"]:
                raise ExecutionFailure("invalid_input")
            (staging / ("config-" + name)).write_bytes(content)
        for name, text in provenance.items():
            (staging / name).write_text(text)
        reference._json(staging / "hosting-policy.json", HOSTING_POLICY | {"schema_version": 2,
            "profile_id": specification["profile_id"], "evaluation_image_id": evaluation_image,
            "compute_adapter_sha256": reference._sha(Path(__file__))})
        controller.fixed_metadata = {path.name: {"size_bytes": path.stat().st_size,
            "sha256": reference._sha(path)} for path in staging.iterdir()}
        controller.checkpoint("running", {})
        started = time.monotonic()
        controller.run_native(native_command(specification, stage_inputs, staging, image_id,
            handoff["camera_mounts"], controller.container_name), staging, specification["resources"]["timeout_s"])
        elapsed = time.monotonic() - started
        status = strict_json((staging / "status.json").read_bytes())
        validate_native(status, read_trajectory(staging / "trajectory.csv"), handoff["input_manifest"])
        evaluation = controller.evaluate_private(handoff, run_state="complete")
        fields = ("status", "availability", "metrics", "counts", "coverage_fraction", "evaluated_fraction",
                  "reference_coverage_fraction", "initialization_delay_s", "protocol")
        timing = status["timing"]
        result = {"schema_version": 2, "input": specification["input"], "evaluation": {k: evaluation[k] for k in fields},
            "qualification": {"status": "not_assessed", "applicable": False, "passed": None, "checks": {},
                              "reason": "No physical-accuracy qualification gates are defined for private inputs."},
            "calibration_policy": specification["policy"]["calibration"],
            "execution": {"container_wall_seconds": elapsed, "native_wall_seconds": timing["wall_seconds"],
                          "native_cpu_seconds": timing["cpu_seconds"], "native_peak_rss_kib": timing["peak_rss_kib"]}}
        reference._json(staging / "result.json", result)
        complete_manifest = manifest(staging, specification, complete=True, fixed_metadata=controller.fixed_metadata)
        result["artifacts"] = {"file_count": len(complete_manifest["files"]),
            "total_bytes": sum(v["size_bytes"] for v in complete_manifest["files"].values()),
            "manifest_sha256": sha256(canonical(complete_manifest).encode()).hexdigest()}
        controller.checkpoint("validating", {})
        state = "succeeded"
    except AttemptLeaseLost:
        raise
    except AttemptCancelled:
        state, failure = "cancelled", "cancelled"
    except ExecutionFailure as exc:
        failure = exc.code
        if (controller and handoff and controller.phase == "running"
                and failure in {"initialization_failure", "numerical_failure"}):
            try:
                controller.evaluate_private(handoff, run_state="partial")
            except AttemptLeaseLost:
                raise
            except AttemptCancelled:
                state, failure = "cancelled", "cancelled"
            except (ExecutionFailure, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                controller.investigation_validated = False
    except (ValueError, TypeError, KeyError, UnicodeError):
        failure = "invalid_input" if controller is None or controller.phase == "preparing" else "numerical_failure"
    except (OSError, subprocess.SubprocessError):
        failure = "infrastructure_failure"
    finally:
        cleaned = cleanup_attempt(attempt_id)
    if not cleaned:
        state, failure = "failed", "infrastructure_failure"
    if state == "succeeded":
        return {"state": state, "failure": None, "result": result, "manifest": complete_manifest}
    try:
        retained = manifest(staging, specification, complete=False,
            investigation_validated=bool(controller and controller.investigation_validated and state == "failed" and cleaned),
            fixed_metadata=controller.fixed_metadata if controller else None) if valid else {"schema_version": 1, "files": {}}
    except (ExecutionFailure, OSError, ValueError):
        retained = {"schema_version": 1, "files": {}}
    return {"state": state, "failure": {"code": failure, "message": _MESSAGES[failure]}, "result": None, "manifest": retained}
