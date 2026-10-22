"""Private worker handoff and evaluator isolation without Docker or a database."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from workspace import private_job_execution as execution
from meridian.vio_private_evaluation import main as evaluate_main
from meridian.vio_private_inputs import prepare_private_inputs, POSE_HEADER
from meridian.vio_private_specification import build_private_specification
from test_vio_private import fixture


def native_files(staging, handoff, *, empty=False):
    times = handoff["camera_timestamps_ns"]
    with (staging / "trajectory.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(POSE_HEADER)
        if not empty:
            writer.writerows([stamp, index, 0, 0, 0, 0, 0, 1] for index, stamp in enumerate(times))
    status = {"schema_version": 1, "state": "uninitialized" if empty else "complete",
        "upstream_revision": execution.reference.UPSTREAM_REVISION,
        "orientation": "Hamilton xyzw, IMU-body to local-world",
        "ground_truth_access": "no reference input or initialization",
        "counts": {"imu_input": handoff["input_manifest"]["imu_samples"], "cam0_input": len(times),
            "cam1_input": len(times), "imu_processed": handoff["input_manifest"]["imu_samples"],
            "camera_pairs_processed": len(times), "camera_pairs_without_imu_bracket": 0,
            "trajectory_states": 0 if empty else len(times)},
        "first_camera_timestamp_ns": int(times[0]), "last_camera_timestamp_ns": int(times[-1]),
        "first_estimate_timestamp_ns": None if empty else int(times[0]),
        "last_estimate_timestamp_ns": None if empty else int(times[-1]),
        "initialized_timestamp_ns": None if empty else int(times[0]), "error": None,
        "timing": {"wall_seconds": 0.5, "cpu_seconds": 0.25, "peak_rss_kib": 1000}}
    (staging / "status.json").write_text(json.dumps(status))
    return status


def controller_fixture(tmp_path, *, reference=False, empty=False):
    binding, prepared, source, calibration, revision = fixture(tmp_path, reference=reference)
    spec = build_private_specification(binding, prepared, source, calibration)
    handoff = prepare_private_inputs(spec, revision, tmp_path / "private-inputs")
    staging = tmp_path / "run"
    staging.mkdir()
    status = native_files(staging, handoff, empty=empty)
    controller = execution.PrivateController(spec, staging, str(uuid4()), lambda *_: None,
                                             "sha256:" + "c" * 64, tmp_path)
    return controller, handoff, status


def local_evaluator(controller, handoff, commands):
    def run(command, kind, timeout, log_path):
        assert kind == "evaluation" and timeout == 120
        commands.append(command)
        log_path.write_text("Synthetic child transport; real numerical evaluator.\n")
        parent, staging = controller.configuration_parent, controller.staging
        code = evaluate_main(["--request", str(parent / "private-evaluation-request.json"),
            "--estimate", str(staging / "trajectory.csv"),
            "--reference", handoff["evaluation"]["reference_path"],
            "--imu", handoff["evaluation"]["imu_path"], "--native-status", str(staging / "status.json"),
            "--evaluation-output", str(parent / "private-evaluation-output/evaluation.json"),
            "--investigation-output", str(parent / "private-evaluation-output/investigation.json")])
        return code, ""
    return run


@pytest.mark.parametrize("empty", [False, True])
def test_real_optional_reference_evaluator_and_failure_evidence(tmp_path, monkeypatch, empty):
    controller, handoff, status = controller_fixture(tmp_path, empty=empty)
    commands = []
    monkeypatch.setattr(controller, "_supervise", local_evaluator(controller, handoff, commands))
    result = controller.evaluate_private(handoff, run_state="partial" if empty else "complete")
    assert result["availability"] == {"status": "unavailable", "reason": "reference_absent"}
    assert result["metrics"] is None
    assert controller.investigation_validated
    saved = json.loads((controller.staging / "investigation.json").read_text())
    assert saved["run_state"] == ("partial" if empty else "complete")
    assert (saved["frames"][0]["estimate_raw"] is None) == empty
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "512m"
    assert any("/evaluation/reference.csv,dst=/inputs/reference.csv,readonly" in value for value in command)
    assert not any("docker.sock" in value or "DATABASE_URL" in value for value in command)
    failure_manifest = execution.manifest(controller.staging, controller.specification, complete=False,
                                           investigation_validated=True)
    assert "investigation.json" in failure_manifest["files"]
    assert "trajectory.csv" not in failure_manifest["files"]
    assert "evaluation.json" not in failure_manifest["files"]


def test_native_only_receives_sensors_and_exact_read_only_images(tmp_path):
    controller, handoff, _ = controller_fixture(tmp_path, reference=True)
    command = execution.native_command(controller.specification, tmp_path / "private-inputs",
        controller.staging, controller.specification["runtime"]["image_id"], handoff["camera_mounts"], "test")
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 5
    assert all("readonly" in mount for mount in mounts if "dst=/output" not in mount)
    assert not any("evaluation" in mount or "reference" in mount or "acquisition.json" in mount for mount in mounts)
    assert not any("src=" + str(tmp_path / "revision") + "," in mount for mount in mounts)
    assert {mount.split("dst=")[1].split(",")[0] for mount in mounts} == {
        "/sensors", "/config", "/output", "/sensors/mav0/cam0/data", "/sensors/mav0/cam1/data"}


def test_native_status_and_evaluation_cannot_claim_unobserved_samples(tmp_path, monkeypatch):
    controller, handoff, status = controller_fixture(tmp_path)
    trajectory = execution.read_trajectory(controller.staging / "trajectory.csv")
    execution.validate_native(status, trajectory, handoff["input_manifest"])
    for key, value in (("camera_pairs_without_imu_bracket", 1), ("imu_input", 999), ("cam1_input", True)):
        changed = deepcopy(status)
        changed["counts"][key] = value
        with pytest.raises(execution.ExecutionFailure):
            execution.validate_native(changed, trajectory, handoff["input_manifest"])
    monkeypatch.setattr(controller, "_supervise", local_evaluator(controller, handoff, []))
    evaluation = controller.evaluate_private(handoff, run_state="complete")
    changed = deepcopy(evaluation)
    changed["availability"] = {"status": "available", "reason": None}
    with pytest.raises(execution.ExecutionFailure):
        execution.validate_evaluation(changed, controller.specification, handoff["input_manifest"], 2)
    changed = deepcopy(evaluation)
    changed["protocol"]["version"] = True
    with pytest.raises(execution.ExecutionFailure):
        execution.validate_evaluation(changed, controller.specification, handoff["input_manifest"], 2)


@pytest.mark.parametrize("name", ["experiment-specification.json", "input-manifest.json", "config-estimator_config.yaml"])
def test_native_output_cannot_redefine_fixed_input_provenance(tmp_path, monkeypatch, name):
    controller, handoff, _ = controller_fixture(tmp_path)
    path = controller.staging / name
    path.write_bytes(b"trusted host metadata")
    controller.fixed_metadata = {name: {"size_bytes": path.stat().st_size, "sha256": execution.reference._sha(path)}}
    execution.verify_fixed_metadata(controller.staging, controller.fixed_metadata)
    path.write_bytes(b"changed host metadata")
    called = []
    monkeypatch.setattr(controller, "_supervise", lambda *_: called.append(True))
    for state in ("complete", "partial"):
        with pytest.raises(execution.ExecutionFailure) as caught:
            controller.evaluate_private(handoff, run_state=state)
        assert caught.value.code == "invalid_input"
    assert not called
    with pytest.raises(execution.ExecutionFailure):
        execution.manifest(controller.staging, controller.specification, complete=False,
            investigation_validated=True, fixed_metadata=controller.fixed_metadata)


@pytest.mark.parametrize("lost", [False, True])
def test_preparation_cancellation_and_lease_loss_cleanup_without_publication(tmp_path, monkeypatch, lost):
    binding, prepared, source, calibration, revision = fixture(tmp_path)
    spec = build_private_specification(binding, prepared, source, calibration)
    calls = []
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda attempt: calls.append(attempt) or True)
    def stop(*_):
        raise execution.AttemptLeaseLost() if lost else execution.AttemptCancelled()
    attempt = str(uuid4())
    kwargs = dict(attempt_id=attempt, checkpoint=stop, evaluation_image="sha256:" + "c" * 64,
                  configuration_parent=tmp_path)
    if lost:
        with pytest.raises(execution.AttemptLeaseLost):
            execution.execute_private_attempt(spec, revision, tmp_path / "run", **kwargs)
    else:
        outcome = execution.execute_private_attempt(spec, revision, tmp_path / "run", **kwargs)
        assert outcome["state"] == "cancelled" and outcome["manifest"]["files"] == {}
    assert calls == [attempt] and not (tmp_path / "run").exists()
    assert (revision / "prepared-input.json").is_file()
