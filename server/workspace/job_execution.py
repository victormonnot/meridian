"""Trusted compute adapter, separate from HTTP and durable queue ownership.

This module never publishes a job or renews a database lease itself. The caller's
checkpoint raises on cancellation or lease loss, and the caller fences durable
publication after this adapter has stopped its children and validated artifacts.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import time
from uuid import UUID

from meridian import vio_reference as reference
from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from meridian.vio_evaluation import read_trajectory
from meridian.vio_investigation import build_image_index, validate_investigation
from meridian.vio_specification import specification_sha256, validate_specification


MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_SCRATCH_BYTES = 128 * 1024 * 1024
CONTAINER_PHASES = ("native", "evaluation", "version", "dependencies", "binary")
HOSTING_POLICY = {
    "schema_version": 1, "log_limit_bytes_per_process": MAX_LOG_BYTES,
    "artifact_limit_bytes": MAX_ARTIFACT_BYTES, "scratch_limit_bytes": MAX_SCRATCH_BYTES,
    "evaluation": {"cpus": 0.5, "memory_mb": 512, "timeout_s": 120,
                   "network": "none", "rootfs": "read-only", "pids": 64},
    "progress": "execution phases and observed elapsed wall time; no inferred completion percentage",
}
_MESSAGES = {
    "invalid_input": "The saved specification or prepared input identities are invalid.",
    "initialization_failure": "The estimator did not initialize on the recording.",
    "numerical_failure": "The estimator or independent evaluation produced invalid output.",
    "resource_exhaustion": "The attempt exceeded its memory, log, or artifact budget.",
    "timeout": "The attempt exceeded its execution time budget.",
    "infrastructure_failure": "The compute environment could not complete the attempt.",
    "cancelled": "The attempt was cancelled.",
}


class ExecutionFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


def _identity(attempt_id: str) -> str:
    value = UUID(str(attempt_id))
    if str(value) != str(attempt_id):
        raise ValueError("Attempt identity must be a canonical UUID.")
    return value.hex


def container_names(attempt_id: str) -> dict[str, str]:
    identity = _identity(attempt_id)
    return {phase: f"meridian-vio-job-{identity}-{phase}" for phase in CONTAINER_PHASES}


def cleanup_attempt(attempt_id: str) -> bool:
    """Remove only this attempt's labelled containers, then verify their absence.

    Daemon errors return False, never a false claim that cleanup succeeded.
    The queue must not retry or release an execution slot after failed cleanup.
    """
    names = set(container_names(attempt_id).values())
    query = ["docker", "container", "ls", "--all", "--filter",
             "label=meridian.component=vio-job", "--filter",
             f"label=meridian.attempt={attempt_id}", "--format", "{{.Names}}"]
    try:
        for _ in range(2):
            found = subprocess.run(query, check=True, capture_output=True, text=True, timeout=15)
            active = set(found.stdout.splitlines())
            if not active:
                return True
            if not active <= names:
                return False
            subprocess.run(["docker", "rm", "--force", *sorted(active)], check=True,
                           capture_output=True, timeout=30)
        found = subprocess.run(query, check=True, capture_output=True, text=True, timeout=15)
        return not found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def verify_scratch(path: Path) -> None:
    """Require the host-mounted bounded tmpfs, including after a host restart."""
    path = Path(path).resolve(strict=True)
    mounted = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), before.split()[4])
        parent = Path(mountpoint)
        if path == parent or path.is_relative_to(parent):
            mounted.append((len(parent.parts), after.split()[0]))
    capacity = os.statvfs(path)
    if (not mounted or max(mounted)[1] != "tmpfs"
            or not 0 < capacity.f_blocks * capacity.f_frsize <= MAX_SCRATCH_BYTES):
        raise ExecutionFailure("infrastructure_failure")


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _allowed_files(specification: dict) -> set[str]:
    return {"trajectory.csv", "status.json", "evaluation.json", "run-spec.json",
            "experiment-specification.json", "runner.log", "evaluation.log", "result.json",
            "dependency-versions.txt", "binary-sha256.txt", "hosting-policy.json",
            "investigation.json", "image-index.json"} | {
                "config-" + name for name in specification["configuration"]["rendered_files"]}


def _check_directory(directory: Path, allowed: set[str]) -> None:
    total = 0
    for path in directory.iterdir():
        metadata = path.lstat()
        if path.name not in allowed or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExecutionFailure("numerical_failure")
        total += metadata.st_size
        if metadata.st_size > MAX_ARTIFACT_BYTES or total > MAX_ARTIFACT_BYTES:
            raise ExecutionFailure("resource_exhaustion")


def _manifest(directory: Path, specification: dict, *, complete: bool, investigation_validated=False) -> dict:
    files = {}
    allowed = _allowed_files(specification)
    if complete:
        _check_directory(directory, allowed)
        if {path.name for path in directory.iterdir()} != allowed:
            raise ExecutionFailure("numerical_failure")
    if not directory.exists():
        return {"schema_version": 1, "files": {}}
    total = 0
    for path in sorted(directory.iterdir()):
        metadata = path.lstat()
        if path.name not in allowed or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            continue
        if not complete and path.name in {"trajectory.csv", "evaluation.json"}:
            continue
        if not complete and path.name in {"investigation.json", "image-index.json"} and not investigation_validated:
            continue
        if metadata.st_size > MAX_ARTIFACT_BYTES or total + metadata.st_size > MAX_ARTIFACT_BYTES:
            continue
        files[path.name] = {"sha256": reference._sha(path), "size_bytes": metadata.st_size}
        total += metadata.st_size
    return {"schema_version": 1, "files": files}


class _Controller:
    def __init__(self, specification, staging, attempt_id, checkpoint, evaluation_image,
                 configuration_parent):
        self.specification = specification
        self.staging = staging
        self.attempt_id = attempt_id
        self.names = container_names(attempt_id)
        self.container_name = self.names["native"]
        self.callback = checkpoint
        self.evaluation_image = evaluation_image
        self.configuration_parent = configuration_parent
        self.phase = "preparing"
        self.allowed = _allowed_files(specification)
        self.investigation_validated = False
        self.investigation_run_state = None

    def checkpoint(self, phase, progress):
        self.phase = phase
        self.callback(phase, progress)

    def _command(self, command, kind):
        command = list(command)
        if command[:2] != ["docker", "run"]:
            raise ExecutionFailure("infrastructure_failure")
        if "--rm" in command:
            command.remove("--rm")
        if "--name" in command:
            index = command.index("--name")
            del command[index:index + 2]
        return command[:2] + [
            "--name", self.names[kind], "--log-driver", "none",
            "--label", "meridian.component=vio-job", "--label", f"meridian.attempt={self.attempt_id}",
            "--label", f"meridian.phase={kind}",
            "--ulimit", f"fsize={MAX_ARTIFACT_BYTES}:{MAX_ARTIFACT_BYTES}",
        ] + command[2:]

    def _supervise(self, command, kind, timeout_s, log_path=None):
        """Drain a bounded pipe while checking cancellation, leases and artifacts."""
        command = self._command(command, kind)
        started, captured = time.monotonic(), 0
        stream = io.BytesIO() if log_path is None else log_path.open("xb")
        process = None
        try:
            with stream if log_path is not None else nullcontext(stream):
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    next_checkpoint = 0.0
                    while selector.get_map() or process.poll() is None:
                        elapsed = time.monotonic() - started
                        if elapsed > timeout_s:
                            raise ExecutionFailure("timeout")
                        if elapsed >= next_checkpoint:
                            self.checkpoint(self.phase, {"elapsed_seconds": round(elapsed, 3)})
                            next_checkpoint = elapsed + 1
                            if self.staging.exists():
                                _check_directory(self.staging, self.allowed)
                        for key, _ in selector.select(timeout=min(1.0, timeout_s - elapsed)):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            remaining = MAX_LOG_BYTES - captured
                            stream.write(chunk[:remaining])
                            captured += len(chunk)
                            if captured > MAX_LOG_BYTES:
                                raise ExecutionFailure("resource_exhaustion")
                    returncode = process.wait(timeout=max(0.01, timeout_s - (time.monotonic() - started)))
                    self.checkpoint(self.phase, {"elapsed_seconds": round(time.monotonic() - started, 3)})
                    inspection = subprocess.run(
                        ["docker", "inspect", "--format", "{{json .State}}", self.names[kind]],
                        capture_output=True, text=True, check=True, timeout=15)
                    state = json.loads(inspection.stdout)
                    if state.get("OOMKilled") is True or returncode == 153:
                        raise ExecutionFailure("resource_exhaustion")
                    return returncode, stream.getvalue().decode("utf-8") if log_path is None else None
        finally:
            try:
                try:
                    subprocess.run(["docker", "rm", "--force", self.names[kind]],
                                   capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    # Final cleanup_attempt verifies absence. Never mask a
                    # cancellation/lease-loss exception with a cleanup error.
                    pass
            finally:
                if process is not None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdout is not None:
                        process.stdout.close()

    def probe(self, command, kind):
        self.checkpoint("preparing", {})
        code, text = self._supervise(command, kind, 30)
        if code:
            raise ExecutionFailure("infrastructure_failure")
        return subprocess.CompletedProcess(command, code, stdout=text)

    def run_native(self, command, output, timeout_s):
        code, _ = self._supervise(command, "native", timeout_s, output / "runner.log")
        _check_directory(output, self.allowed)
        if code:
            status = reference._parse_json((output / "status.json").read_text()) if (output / "status.json").exists() else {}
            if status.get("state") == "uninitialized":
                raise ExecutionFailure("initialization_failure")
            raise ExecutionFailure("infrastructure_failure" if code in {125, 126, 127} else "numerical_failure")
        return code

    def evaluate(self, estimate, ground_truth, *, expected_timestamps_ns, run_state="complete", **options):
        self.checkpoint("evaluating", {})
        parent = self.configuration_parent
        request_path = parent / "evaluation-request.json"
        reference._json(request_path, {
            "schema_version": 1, "expected_timestamps_ns": expected_timestamps_ns,
            "max_reference_gap_ns": options.get("max_reference_gap_ns", 20_000_000),
            "max_estimate_timestamp_error_ns": options.get("max_estimate_timestamp_error_ns", 1000),
            "evaluator_sha256": self.specification["evaluation"]["evaluator_sha256"],
        })
        prepared = ground_truth.parent.parent
        manifest_path = prepared / "manifest.json"
        if reference._sha(manifest_path) != self.specification["dataset"]["prepared_manifest_sha256"]:
            raise ExecutionFailure("invalid_input")
        manifest = reference._parse_json(manifest_path.read_text())
        for filename in ("investigation.json", "image-index.json"):
            if (self.staging / filename).exists() or (self.staging / filename).is_symlink():
                raise ExecutionFailure("numerical_failure")
        investigation_request = {
            "schema_version": 1, "specification_sha256": specification_sha256(self.specification),
            "dataset_manifest_sha256": self.specification["dataset"]["prepared_manifest_sha256"],
            "dataset_id": self.specification["dataset"]["id"],
            "expected_timestamps_ns": expected_timestamps_ns,
            "evaluator_sha256": self.specification["evaluation"]["evaluator_sha256"],
            "reference_sha256": self.specification["dataset"]["reference_sha256"],
            "imu_sha256": manifest["files"]["sensors/mav0/imu0/data.csv"]["sha256"],
            "license": manifest["license"], "run_state": run_state,
        }
        investigation_request_path = parent / "investigation-request.json"
        reference._json(investigation_request_path, investigation_request)
        image_index = build_image_index(manifest, expected_timestamps_ns,
                                       specification_sha256=specification_sha256(self.specification),
                                       dataset_manifest_sha256=self.specification["dataset"]["prepared_manifest_sha256"])
        destination = parent / "evaluation-output"
        destination.mkdir()
        limits = {"cpus": 0.5, "memory_mb": 512, "pids": 64, "tmp_mb": 32}
        command = ["docker", "run", *reference._container_options(limits), "--entrypoint", "python"]
        for source, target in ((estimate, "/inputs/estimate.csv"),
                               (ground_truth, "/inputs/reference.csv"),
                               (request_path, "/inputs/request.json"),
                               (prepared / "sensors/mav0/imu0/data.csv", "/inputs/imu.csv"),
                               (self.staging / "status.json", "/inputs/native-status.json"),
                               (investigation_request_path, "/inputs/investigation-request.json")):
            command += reference._mount(source, target, readonly=True)
        command += reference._mount(destination, "/evaluation", readonly=False)
        command += [self.evaluation_image, "-m", "meridian.vio_job_evaluation", "--estimate",
                    "/inputs/estimate.csv", "--reference", "/inputs/reference.csv", "--request",
                    "/inputs/request.json", "--output", "/evaluation/evaluation.json",
                    "--imu", "/inputs/imu.csv", "--native-status", "/inputs/native-status.json",
                    "--investigation-request", "/inputs/investigation-request.json",
                    "--investigation-output", "/evaluation/investigation.json"]
        code, _ = self._supervise(command, "evaluation", 120, self.staging / "evaluation.log")
        if code:
            raise ExecutionFailure("infrastructure_failure" if code in {125, 126, 127} else "numerical_failure")
        _check_directory(destination, {"evaluation.json", "investigation.json"})
        evaluation = reference._parse_json((destination / "evaluation.json").read_text())
        investigation = reference._parse_json((destination / "investigation.json").read_text())
        validate_investigation(investigation, request=investigation_request, evaluation=evaluation,
                               trajectory_sha256=reference._sha(estimate))
        if len(investigation["imu"]["samples"]) != self.specification["interval"]["imu_samples"]:
            raise ExecutionFailure("numerical_failure")
        shutil.copyfile(destination / "investigation.json", self.staging / "investigation.json")
        reference._json(self.staging / "image-index.json", image_index)
        _check_directory(self.staging, self.allowed)
        self.investigation_validated = True
        self.investigation_run_state = run_state
        return evaluation

    def salvage_investigation(self, prepared):
        """Retain valid partial outputs after native failure, without a success result."""
        _check_directory(self.staging, self.allowed)
        trace = read_trajectory(self.staging / "trajectory.csv")
        status = reference._parse_json((self.staging / "status.json").read_text())
        if (type(status.get("schema_version")) is not int or status["schema_version"] != 1
                or status.get("state") not in {"failed", "uninitialized"}
                or status.get("orientation") != "Hamilton xyzw, IMU-body to local-world"
                or status.get("counts", {}).get("trajectory_states") != len(trace.timestamp_ns)):
            raise ExecutionFailure("numerical_failure")
        manifest = reference._parse_json((prepared / "manifest.json").read_text())
        camera_csv = prepared / "sensors/mav0/cam0/data.csv"
        if reference._sha(camera_csv) != manifest["files"]["sensors/mav0/cam0/data.csv"]["sha256"]:
            raise ExecutionFailure("invalid_input")
        from meridian.vio_dataset import camera_timestamps_ns
        expected = camera_timestamps_ns(prepared)
        interval = self.specification["interval"]
        if (len(expected) != interval["camera_pairs"] or str(expected[0]) != interval["first_camera_timestamp_ns"]
                or str(expected[-1]) != interval["last_camera_timestamp_ns"]):
            raise ExecutionFailure("invalid_input")
        self.evaluate(self.staging / "trajectory.csv", prepared / "reference/trajectory.csv",
                      expected_timestamps_ns=expected, run_state="partial",
                      max_reference_gap_ns=self.specification["evaluation"]["maximum_reference_bracket_ns"],
                      max_estimate_timestamp_error_ns=self.specification["evaluation"]["maximum_estimate_timestamp_error_ns"])


def _summary(directory, run, manifest):
    evaluation = reference._parse_json((directory / "evaluation.json").read_text())
    fields = ("status", "metrics", "counts", "coverage_fraction", "evaluated_fraction",
              "reference_coverage_fraction", "initialization_delay_s", "protocol")
    if any(name not in evaluation for name in fields):
        raise ExecutionFailure("numerical_failure")
    native = run["native_status"]["timing"]
    return {
        "schema_version": 1, "evaluation": {name: evaluation[name] for name in fields},
        "qualification": {name: run["qualification"][name] for name in ("passed", "checks")},
        "execution": {"container_wall_seconds": run["container_wall_seconds"],
                      "native_wall_seconds": native["wall_seconds"],
                      "native_cpu_seconds": native["cpu_seconds"],
                      "native_peak_rss_kib": native["peak_rss_kib"]},
        "artifacts": {"file_count": len(manifest["files"]),
                      "total_bytes": sum(item["size_bytes"] for item in manifest["files"].values()),
                      "manifest_sha256": hashlib.sha256(_canonical(manifest)).hexdigest()},
    }


def _validate_completed(directory, run, specification):
    status = run["native_status"]
    trace = read_trajectory(directory / "trajectory.csv")
    reference._validate_native_status(status, trace)
    if (status.get("upstream_revision") != reference.UPSTREAM_REVISION
            or status.get("orientation") != "Hamilton xyzw, IMU-body to local-world"
            or status.get("ground_truth_access") != "no reference input or initialization"):
        raise ExecutionFailure("numerical_failure")
    counts = status.get("counts", {})
    required = {"imu_input", "cam0_input", "cam1_input", "imu_processed",
                "camera_pairs_processed", "camera_pairs_without_imu_bracket", "trajectory_states"}
    if set(counts) != required or any(type(value) is not int or value < 0 for value in counts.values()):
        raise ExecutionFailure("numerical_failure")
    interval = specification["interval"]
    if (counts["cam0_input"] != interval["camera_pairs"]
            or counts["cam1_input"] != interval["camera_pairs"]
            or counts["imu_input"] != interval["imu_samples"]
            or counts["imu_processed"] > counts["imu_input"]
            or counts["camera_pairs_processed"] + counts["camera_pairs_without_imu_bracket"] != interval["camera_pairs"]):
        raise ExecutionFailure("numerical_failure")
    for field in ("first_camera_timestamp_ns", "last_camera_timestamp_ns"):
        if type(status.get(field)) is not int or str(status[field]) != interval[field]:
            raise ExecutionFailure("numerical_failure")
    for field in ("first_estimate_timestamp_ns", "last_estimate_timestamp_ns", "initialized_timestamp_ns"):
        if type(status.get(field)) is not int:
            raise ExecutionFailure("numerical_failure")
    if specification_sha256(reference._load_specification(directory / "experiment-specification.json")) != specification_sha256(specification):
        raise ExecutionFailure("invalid_input")
    saved = reference._parse_json((directory / "result.json").read_text())
    if saved != run or run.get("state") != "complete":
        raise ExecutionFailure("numerical_failure")
    for name, identity in run["artifacts"].items():
        path = directory / name
        if (path.name != name or path.is_symlink() or not path.is_file()
                or path.stat().st_size != identity["size_bytes"] or reference._sha(path) != identity["sha256"]):
            raise ExecutionFailure("numerical_failure")
    evaluation = reference._parse_json((directory / "evaluation.json").read_text())
    protocol = evaluation.get("protocol", {})
    if (type(evaluation.get("schema_version")) is not int or evaluation["schema_version"] != 1
            or type(protocol.get("version")) is not int or protocol["version"] != 1
            or protocol.get("name") != "meridian-vio-se3"
            or protocol.get("max_reference_gap_ns") != specification["evaluation"]["maximum_reference_bracket_ns"]
            or protocol.get("max_estimate_timestamp_error_ns") != specification["evaluation"]["maximum_estimate_timestamp_error_ns"]
            or protocol.get("scale_fitted") is not False or protocol.get("time_offset_fitted") is not False
            or protocol.get("estimate_interpolation") is not False
            or reference.qualification(evaluation) != run["qualification"]):
        raise ExecutionFailure("numerical_failure")
    counts = evaluation.get("counts", {})
    required_counts = {"expected_camera_samples", "estimate_samples", "reference_samples",
                       "reference_supported_camera_samples", "evaluated_camera_samples",
                       "missing_initial_camera_samples", "missing_interior_camera_samples",
                       "missing_trailing_camera_samples"}
    if set(counts) != required_counts or any(type(value) is not int or value < 0 for value in counts.values()):
        raise ExecutionFailure("numerical_failure")
    expected = interval["camera_pairs"]
    if (counts["expected_camera_samples"] != expected or counts["estimate_samples"] != len(trace.timestamp_ns)
            or counts["reference_supported_camera_samples"] > expected
            or counts["evaluated_camera_samples"] > min(counts["estimate_samples"], counts["reference_supported_camera_samples"])
            or sum(counts[key] for key in ("missing_initial_camera_samples", "missing_interior_camera_samples",
                                          "missing_trailing_camera_samples")) != expected - counts["estimate_samples"]):
        raise ExecutionFailure("numerical_failure")
    for field, count in (("coverage_fraction", "estimate_samples"),
                         ("evaluated_fraction", "evaluated_camera_samples"),
                         ("reference_coverage_fraction", "reference_supported_camera_samples")):
        value = evaluation.get(field)
        if type(value) not in (int, float) or not math.isclose(value, counts[count] / expected, abs_tol=1e-12):
            raise ExecutionFailure("numerical_failure")
    if evaluation.get("status") not in {"ok", "insufficient_associations", "degenerate_alignment"}:
        raise ExecutionFailure("numerical_failure")
    metrics = evaluation.get("metrics")
    if evaluation["status"] == "ok":
        required_metrics = {"ate_rmse_m", "position_error_mean_m", "position_error_max_m",
                            "orientation_rmse_rad", "orientation_rmse_deg", "orientation_error_mean_deg",
                            "orientation_error_max_deg"}
        if (type(metrics) is not dict or set(metrics) != required_metrics
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                       for value in metrics.values())):
            raise ExecutionFailure("numerical_failure")
    elif metrics is not None:
        raise ExecutionFailure("numerical_failure")


def execute_attempt(specification, prepared: Path, staging: Path, *, attempt_id: str,
                    checkpoint, evaluation_image: str, configuration_parent: Path) -> dict:
    """Execute one immutable specification, without publishing or changing its owner."""
    controller = None
    run = None
    failure = None
    valid_specification = False
    outcome = "failed"
    staging, parent = Path(staging), Path(configuration_parent)
    _identity(attempt_id)
    if staging.exists() or staging.is_symlink():
        raise ValueError("Attempt staging already exists.")
    try:
        validate_specification(specification)
        valid_specification = True
        checkpoint("preparing", {})
        if parent != parent.resolve(strict=True) or staging.parent != parent or staging.name != "run":
            raise ExecutionFailure("infrastructure_failure")
        verify_scratch(parent)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", evaluation_image):
            raise ExecutionFailure("infrastructure_failure")
        controller = _Controller(specification, staging, attempt_id, checkpoint,
                                 evaluation_image, parent)
        run = reference.run_reference(
            prepared, specification["runtime"]["image_id"], staging,
            specification=specification, controller=controller)
        reference._json(staging / "hosting-policy.json", {
            **HOSTING_POLICY, "evaluation_image_id": evaluation_image,
            "compute_adapter_sha256": reference._sha(Path(__file__)),
            "evaluation_entrypoint_sha256": reference._sha(Path(reference.__file__).with_name("vio_job_evaluation.py")),
        })
        # A successful process exit is insufficient: the independent reader,
        # manifest, saved specification and evaluator have all been validated.
        _validate_completed(staging, run, specification)
        if not controller.investigation_validated:
            raise ExecutionFailure("numerical_failure")
        manifest = _manifest(staging, specification, complete=True)
        result = _summary(staging, run, manifest)
        _canonical(result)
        checkpoint("validating", {})
        outcome = "succeeded"
    except AttemptLeaseLost:
        raise
    except AttemptCancelled:
        failure = "cancelled"
        outcome = "cancelled"
    except ExecutionFailure as error:
        failure = error.code
        if (controller is not None and controller.phase == "running"
                and failure in {"initialization_failure", "numerical_failure"}):
            try:
                controller.salvage_investigation(Path(prepared))
            except AttemptLeaseLost:
                raise
            except AttemptCancelled:
                failure, outcome = "cancelled", "cancelled"
            except (ExecutionFailure, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                # Keep the original native failure. Unvalidated diagnostics do
                # not enter the published failure manifest.
                controller.investigation_validated = False
    except (ValueError, TypeError, KeyError, UnicodeError):
        failure = "numerical_failure" if controller is not None and controller.phase != "preparing" else "invalid_input"
    except (OSError, subprocess.SubprocessError):
        failure = "infrastructure_failure"
    finally:
        cleaned = cleanup_attempt(attempt_id)
    if not cleaned:
        outcome, failure = "failed", "infrastructure_failure"
    if outcome != "succeeded":
        return {"state": outcome, "failure": {"code": failure, "message": _MESSAGES[failure]},
                "result": None, "manifest": (_manifest(staging, specification, complete=False,
                    investigation_validated=(controller is not None and controller.investigation_validated
                                              and controller.investigation_run_state == "partial"
                                              and outcome == "failed" and cleaned))
                                               if valid_specification else {"schema_version": 1, "files": {}})}
    return {"state": outcome, "failure": None, "result": result, "manifest": manifest}
