"""Exercise hosted compute boundaries without an HTTP server or Docker daemon."""

import hashlib
import importlib.util
import json
import csv
import copy
import math
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from meridian import vio_reference as reference
from meridian.vio_job_evaluation import evaluate_request, main as evaluate_main
from meridian.vio_specification import DATASET_ID, build_specification


_MODULE = Path(__file__).parents[1] / "server/workspace/job_execution.py"
_SPEC = importlib.util.spec_from_file_location("meridian_job_execution_test", _MODULE)
execution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(execution)


@pytest.fixture
def specification():
    return build_specification(DATASET_ID, {})


def _controller(tmp_path, specification, checkpoint=lambda *_: None):
    return execution._Controller(specification, tmp_path / "run", str(uuid4()), checkpoint,
                                 "sha256:" + "c" * 64, tmp_path)


def _real_process(monkeypatch, controller, script, *, oom=False):
    """Use a real pipe/child with only Docker lifecycle operations substituted."""
    original = subprocess.Popen
    children, commands = [], []
    monkeypatch.setattr(controller, "_command", lambda *_: [sys.executable, "-c", script])

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    def docker(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "rm"]:
            for child in children:
                if child.poll() is None:
                    child.terminate()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"OOMKilled": oom}))

    monkeypatch.setattr(execution.subprocess, "Popen", start)
    monkeypatch.setattr(execution.subprocess, "run", docker)
    return children, commands


def test_supervision_records_real_bytes_and_reaps_child(tmp_path, monkeypatch, specification):
    events = []
    controller = _controller(tmp_path, specification, lambda *event: events.append(event))
    children, commands = _real_process(monkeypatch, controller, "print('native diagnostic')")
    code, content = controller._supervise(["unused"], "native", 2)
    assert code == 0 and content == "native diagnostic\n"
    assert events and children[0].poll() is not None
    assert commands[-1][:3] == ["docker", "rm", "--force"]


@pytest.mark.parametrize("exception", [execution.AttemptCancelled, execution.AttemptLeaseLost])
def test_cancel_and_lease_loss_stop_real_child_even_after_stdout_closes(tmp_path, monkeypatch, specification, exception):
    events = []

    def checkpoint(*event):
        events.append(event)
        if len(events) >= 2:
            raise exception()

    controller = _controller(tmp_path, specification, checkpoint)
    children, _ = _real_process(monkeypatch, controller, "import os,time; os.close(1); os.close(2); time.sleep(20)")
    with pytest.raises(exception):
        controller._supervise(["unused"], "native", 5)
    assert len(events) == 2 and children[0].poll() is not None


def test_log_budget_is_enforced_while_draining_and_does_not_store_excess(tmp_path, monkeypatch, specification):
    controller = _controller(tmp_path, specification)
    children, _ = _real_process(monkeypatch, controller, "import os,time; os.write(1,b'x'*10000); time.sleep(20)")
    monkeypatch.setattr(execution, "MAX_LOG_BYTES", 128)
    log = tmp_path / "runner.log"
    with pytest.raises(execution.ExecutionFailure) as caught:
        controller._supervise(["unused"], "native", 5, log)
    assert caught.value.code == "resource_exhaustion"
    assert log.stat().st_size == 128 and children[0].poll() is not None


def test_timeout_stops_process_instead_of_merely_marking_it_failed(tmp_path, monkeypatch, specification):
    controller = _controller(tmp_path, specification)
    children, _ = _real_process(monkeypatch, controller, "import time; time.sleep(20)")
    with pytest.raises(execution.ExecutionFailure) as caught:
        controller._supervise(["unused"], "native", 0.05)
    assert caught.value.code == "timeout" and children[0].poll() is not None


def test_container_oom_is_distinguished_from_generic_exit_failure(tmp_path, monkeypatch, specification):
    controller = _controller(tmp_path, specification)
    _real_process(monkeypatch, controller, "raise SystemExit(137)", oom=True)
    with pytest.raises(execution.ExecutionFailure) as caught:
        controller._supervise(["unused"], "native", 2)
    assert caught.value.code == "resource_exhaustion"


def test_hosted_container_preserves_native_budget_and_disables_docker_log_storage(tmp_path, specification):
    controller = _controller(tmp_path, specification)
    original = reference.container_command(tmp_path / "prepared", tmp_path / "run",
                                           specification["runtime"]["image_id"], "legacy-name",
                                           specification["resources"])
    command = controller._command(original, "native")
    assert "--rm" not in command
    assert command[command.index("--name") + 1] == controller.container_name
    assert command[command.index("--log-driver") + 1] == "none"
    assert command[command.index("--memory") + 1] == "4096m"
    assert command[command.index("--cpus") + 1] == "2.0"
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--ulimit" in command
    assert not any("DATABASE_URL" in item or "docker.sock" in item for item in command)


def _investigation_fixture(tmp_path, monkeypatch, specification, *, empty=False):
    """Small explicit protocol fixture; this is not the qualified room1 profile."""
    from meridian.vio_investigation import _IMU_HEADER
    spec = copy.deepcopy(specification)
    prepared = tmp_path / "prepared"
    staging = tmp_path / "run"
    staging.mkdir()
    (prepared / "reference").mkdir(parents=True)
    (prepared / "sensors/mav0/imu0").mkdir(parents=True)
    (prepared / "sensors/mav0/cam0").mkdir(parents=True)
    times = [1_000_000_000, 1_010_000_000, 1_020_000_000]
    for path, missing in ((prepared / "reference/trajectory.csv",False),(staging / "trajectory.csv",empty)):
        with path.open("w",newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp_ns","px_m","py_m","pz_m","qx","qy","qz","qw"])
            if not missing:
                writer.writerows([[stamp,*position,0,0,0,1] for stamp,position in zip(times,[(0,0,0),(1,0,0),(0,1,0)])])
    imu = prepared / "sensors/mav0/imu0/data.csv"
    with imu.open("w",newline="") as stream:
        writer=csv.writer(stream); writer.writerow(_IMU_HEADER)
        writer.writerows([stamp,0,0,0,0,0,9.81] for stamp in times)
    camera = prepared / "sensors/mav0/cam0/data.csv"
    with camera.open("w",newline="") as stream:
        writer=csv.writer(stream); writer.writerow(["#timestamp [ns]","filename"])
        writer.writerows([stamp,str(stamp)+".png"] for stamp in times)
    files = {f"sensors/mav0/{cam}/data/{stamp}.png":{"sha256":"d"*64,"size_bytes":123}
             for cam in ("cam0","cam1") for stamp in times}
    files["sensors/mav0/imu0/data.csv"]={"sha256":reference._sha(imu),"size_bytes":imu.stat().st_size}
    files["sensors/mav0/cam0/data.csv"]={"sha256":reference._sha(camera),"size_bytes":camera.stat().st_size}
    reference._json(prepared / "manifest.json", {"files":files,"license":{"spdx":"synthetic_fixture"}})
    spec["dataset"]["prepared_manifest_sha256"] = reference._sha(prepared / "manifest.json")
    spec["dataset"]["reference_sha256"] = reference._sha(prepared / "reference/trajectory.csv")
    spec["interval"].update({"camera_pairs":3,"imu_samples":3,"first_camera_timestamp_ns":str(times[0]),"last_camera_timestamp_ns":str(times[-1])})
    reference._json(staging / "status.json", {"schema_version":1,"state":"uninitialized" if empty else "complete",
        "counts":{"trajectory_states":0 if empty else 3},"orientation":"Hamilton xyzw, IMU-body to local-world",
        "initialized_timestamp_ns":None if empty else times[0],"error":None})
    monkeypatch.setattr(execution,"specification_sha256",lambda value:hashlib.sha256(execution._canonical(value)).hexdigest())
    return spec,prepared,staging,times


def _execute_evaluation_locally(controller, tmp_path, command):
    """Exercise the real child entry point, with Docker replaced only in this fixture."""
    prepared=tmp_path/"prepared"
    assert evaluate_main(["--estimate",str(controller.staging/"trajectory.csv"),"--reference",str(prepared/"reference/trajectory.csv"),
        "--request",str(tmp_path/"evaluation-request.json"),"--output",str(tmp_path/"evaluation-output/evaluation.json"),
        "--imu",str(prepared/"sensors/mav0/imu0/data.csv"),"--native-status",str(controller.staging/"status.json"),
        "--investigation-request",str(tmp_path/"investigation-request.json"),"--investigation-output",str(tmp_path/"evaluation-output/investigation.json")]) == 0
    return 0,None


def test_evaluation_mounts_only_explicit_inputs_without_sensor_tree_or_credentials(tmp_path, monkeypatch, specification):
    specification,prepared,staging,times=_investigation_fixture(tmp_path,monkeypatch,specification)
    controller = _controller(tmp_path, specification)
    commands = []

    def supervise(command, *args):
        commands.append(command)
        return _execute_evaluation_locally(controller,tmp_path,command)

    monkeypatch.setattr(controller, "_supervise", supervise)
    result = controller.evaluate(tmp_path / "run/trajectory.csv", tmp_path / "prepared/reference/trajectory.csv",
                                 expected_timestamps_ns=times)
    command = commands[0]
    mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
    assert len(mounts) == 7 and all(mount.endswith(",readonly") for mount in mounts[:6])
    assert "dst=/inputs/reference.csv" in mounts[1]
    assert not any("docker.sock" in item or "DATABASE_URL" in item for item in command)
    assert [mount for mount in mounts if "/sensors" in mount] == [next(mount for mount in mounts if "/imu0/data.csv" in mount)]
    assert not any("cam0/data" in mount or "cam1/data" in mount for mount in mounts)
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "512m"
    assert result["status"] == "ok" and result["counts"]["estimate_samples"] == 3
    assert controller.investigation_validated
    assert json.loads((staging/"investigation.json").read_text())["full_interval"]["counts"]["evaluated_camera_samples"] == 3


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "unknown", "hardlink", "oversize"])
def test_artifact_validation_rejects_unsafe_or_unbounded_outputs(tmp_path, monkeypatch, unsafe):
    output = tmp_path / "run"
    output.mkdir()
    path = output / "trajectory.csv"
    if unsafe == "symlink":
        path.symlink_to(tmp_path / "private")
    elif unsafe == "directory":
        path.mkdir()
    elif unsafe == "unknown":
        (output / "unexpected.txt").write_text("data")
    elif unsafe == "hardlink":
        original = tmp_path / "private"
        original.write_text("private")
        path.hardlink_to(original)
    else:
        path.write_bytes(b"0123456789")
        monkeypatch.setattr(execution, "MAX_ARTIFACT_BYTES", 9)
    with pytest.raises(execution.ExecutionFailure):
        execution._check_directory(output, {"trajectory.csv"})


def test_partial_manifest_excludes_bad_paths_and_unvalidated_trajectory(tmp_path, specification):
    (tmp_path / "runner.log").write_text("bounded diagnostic")
    (tmp_path / "trajectory.csv").write_text("invalid pose")
    (tmp_path / "status.json").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "secret.txt").write_text("not allowed")
    (tmp_path / "investigation.json").write_text('{"forged":true}')
    (tmp_path / "image-index.json").write_text('{"forged":true}')
    manifest = execution._manifest(tmp_path, specification, complete=False)
    assert set(manifest["files"]) == {"runner.log"}
    assert manifest["files"]["runner.log"]["sha256"] == hashlib.sha256(b"bounded diagnostic").hexdigest()


@pytest.mark.parametrize("corruption", [None,"invalid_trajectory","forged_investigation"])
def test_native_initialization_failure_can_publish_only_validated_partial_investigation(tmp_path,monkeypatch,specification,corruption):
    specification,prepared,staging,times=_investigation_fixture(tmp_path,monkeypatch,specification,empty=True)
    if corruption == "invalid_trajectory":
        with (staging/"trajectory.csv").open("a") as stream:
            stream.write(f"{times[0]},nan,0,0,0,0,0,1\n")
    if corruption == "forged_investigation":
        reference._json(staging/"investigation.json",{"forged":True})
    template=tmp_path/"native-template"
    staging.rename(template)
    monkeypatch.setattr(execution,"validate_specification",lambda _:None)
    monkeypatch.setattr(execution,"verify_scratch",lambda _:None)
    monkeypatch.setattr(execution,"cleanup_attempt",lambda _:True)
    evaluations=[]
    def native(prepared,image,directory,**kwargs):
        shutil.copytree(template,directory)
        kwargs["controller"].checkpoint("running",{})
        raise execution.ExecutionFailure("initialization_failure")
    def supervise(self,command,*args):
        evaluations.append(command)
        return _execute_evaluation_locally(self,tmp_path,command)
    monkeypatch.setattr(reference,"run_reference",native)
    monkeypatch.setattr(execution._Controller,"_supervise",supervise)
    outcome=execution.execute_attempt(specification,prepared,staging,attempt_id=str(uuid4()),checkpoint=lambda *_:None,
                                      evaluation_image="sha256:"+"c"*64,configuration_parent=tmp_path)
    assert outcome["state"] == "failed" and outcome["failure"]["code"] == "initialization_failure"
    assert outcome["result"] is None and "trajectory.csv" not in outcome["manifest"]["files"]
    if corruption is None:
        assert len(evaluations) == 1
        assert {"investigation.json","image-index.json"} <= set(outcome["manifest"]["files"])
        artifact=json.loads((staging/"investigation.json").read_text())
        assert artifact["run_state"] == "partial"
        assert artifact["full_interval"]["counts"]["expected_camera_samples"] == 3
        assert artifact["full_interval"]["counts"]["estimate_samples"] == 0
        assert artifact["full_interval"]["metrics"] is None
    else:
        assert not evaluations
        assert "investigation.json" not in outcome["manifest"]["files"]
        assert "image-index.json" not in outcome["manifest"]["files"]


@pytest.mark.parametrize("error", [execution.AttemptCancelled,execution.AttemptLeaseLost])
def test_partial_investigation_does_not_override_cancellation_or_lost_lease(tmp_path,monkeypatch,specification,error):
    monkeypatch.setattr(execution,"verify_scratch",lambda _:None)
    cleaned=[]
    monkeypatch.setattr(execution,"cleanup_attempt",lambda _:cleaned.append(True) or True)
    def native(prepared,image,directory,**kwargs):
        directory.mkdir()
        kwargs["controller"].checkpoint("running",{})
        raise execution.ExecutionFailure("initialization_failure")
    def salvage(self,prepared):
        raise error()
    monkeypatch.setattr(reference,"run_reference",native)
    monkeypatch.setattr(execution._Controller,"salvage_investigation",salvage)
    def run():
        return execution.execute_attempt(specification,tmp_path/"prepared",tmp_path/"run",attempt_id=str(uuid4()),
            checkpoint=lambda *_:None,evaluation_image="sha256:"+"c"*64,configuration_parent=tmp_path)
    if error is execution.AttemptLeaseLost:
        with pytest.raises(error):
            run()
    else:
        outcome=run()
        assert outcome["state"] == "cancelled" and outcome["manifest"]["files"] == {}
    assert cleaned == [True]


def test_cleanup_requires_verified_absence_and_never_removes_unrelated_names(monkeypatch):
    identity = str(uuid4())
    name = execution.container_names(identity)["native"]
    commands = []
    answers = iter([name + "\n", "", ""])

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=next(answers))

    monkeypatch.setattr(execution.subprocess, "run", run)
    assert execution.cleanup_attempt(identity)
    assert commands[1] == ["docker", "rm", "--force", name]
    commands.clear()
    monkeypatch.setattr(execution.subprocess, "run", lambda command, **kwargs:
                        commands.append(command) or subprocess.CompletedProcess(command, 0, stdout="another-service\n"))
    assert not execution.cleanup_attempt(identity)
    assert len(commands) == 1


def test_cleanup_daemon_failure_is_not_reported_as_success(monkeypatch):
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 15)

    monkeypatch.setattr(execution.subprocess, "run", unavailable)
    assert not execution.cleanup_attempt(str(uuid4()))


def test_ordinary_disk_scratch_is_rejected(tmp_path):
    with pytest.raises(execution.ExecutionFailure) as caught:
        execution.verify_scratch(tmp_path)
    assert caught.value.code == "infrastructure_failure"


@pytest.mark.parametrize("failure,code,state", [
    (execution.AttemptCancelled(), "cancelled", "cancelled"),
    (execution.ExecutionFailure("initialization_failure"), "initialization_failure", "failed"),
    (execution.ExecutionFailure("timeout"), "timeout", "failed"),
    (OSError("private machine path"), "infrastructure_failure", "failed"),
    (ValueError("bad prepared identity"), "invalid_input", "failed"),
])
def test_adapter_classifies_failures_without_exposing_exception_paths(tmp_path, monkeypatch, specification, failure, code, state):
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda _: True)

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(reference, "run_reference", run)
    outcome = execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
        attempt_id=str(uuid4()), checkpoint=lambda *_: None,
        evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert outcome["state"] == state and outcome["failure"]["code"] == code
    assert outcome["result"] is None and "private machine path" not in json.dumps(outcome)


def test_lease_loss_is_propagated_after_cleanup_without_publishable_outcome(tmp_path, monkeypatch, specification):
    cleaned = []
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda identity: cleaned.append(identity) or True)
    def lost(*args, **kwargs):
        raise execution.AttemptLeaseLost()
    monkeypatch.setattr(reference, "run_reference", lost)
    identity = str(uuid4())
    with pytest.raises(execution.AttemptLeaseLost):
        execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
            attempt_id=identity, checkpoint=lambda *_: None,
            evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert cleaned == [identity]


def test_invalid_specification_never_starts_native_job(tmp_path, monkeypatch, specification):
    specification["parameters"]["num_pts"] = True
    monkeypatch.setattr(execution, "cleanup_attempt", lambda _: True)
    monkeypatch.setattr(reference, "run_reference", lambda *args, **kwargs: pytest.fail("native job started"))
    outcome = execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
        attempt_id=str(uuid4()), checkpoint=lambda *_: None,
        evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert outcome["failure"]["code"] == "invalid_input"


def test_evaluation_entrypoint_rejects_wrong_source_identity_before_inputs(tmp_path):
    request = {"schema_version": 1, "expected_timestamps_ns": [1, 2],
               "max_reference_gap_ns": 20_000_000, "max_estimate_timestamp_error_ns": 1000,
               "evaluator_sha256": "0" * 64}
    with pytest.raises(ValueError, match="pinned evaluator"):
        evaluate_request(request, tmp_path / "missing-estimate", tmp_path / "missing-reference")


def test_evaluation_entrypoint_records_a_failure_without_fabricated_metrics(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text('{"schema_version": 2}')
    output = tmp_path / "evaluation.json"
    assert evaluate_main(["--estimate", str(tmp_path / "estimate"), "--reference", str(tmp_path / "reference"),
                          "--request", str(request), "--output", str(output)]) == 2
    assert not output.exists()
    assert str(tmp_path) not in capsys.readouterr().out


def _completed_output(directory, specification, *, missing_start=0, controller=None):
    """Synthetic curved motion, evaluated independently; never native evidence."""
    directory.mkdir()
    interval = specification["interval"]
    first, last = int(interval["first_camera_timestamp_ns"]), int(interval["last_camera_timestamp_ns"])
    times = [first + index * (last - first) // (interval["camera_pairs"] - 1)
             for index in range(interval["camera_pairs"])]
    truth = directory.parent / "reference.csv"
    def poses(path, start=0):
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp_ns", "px_m", "py_m", "pz_m", "qx", "qy", "qz", "qw"])
            for index in range(start, len(times)):
                writer.writerow([times[index], math.cos(index / 100), math.sin(index / 100), index / 1000,
                                 0, 0, 0, 1])
    poses(truth)
    poses(directory / "trajectory.csv", missing_start)
    evaluation = reference.evaluate_trajectory(directory / "trajectory.csv", truth,
                                               expected_timestamps_ns=times)
    status = {"schema_version": 1, "state": "complete", "upstream_revision": reference.UPSTREAM_REVISION,
              "counts": {"imu_input": interval["imu_samples"], "cam0_input": len(times), "cam1_input": len(times),
                         "imu_processed": interval["imu_samples"] - 1, "camera_pairs_processed": len(times),
                         "camera_pairs_without_imu_bracket": 0, "trajectory_states": len(times) - missing_start},
              "first_camera_timestamp_ns": first, "last_camera_timestamp_ns": last,
              "first_estimate_timestamp_ns": times[missing_start], "last_estimate_timestamp_ns": last,
              "initialized_timestamp_ns": times[missing_start],
              "orientation": "Hamilton xyzw, IMU-body to local-world",
              "ground_truth_access": "no reference input or initialization",
              "timing": {"wall_seconds": 1.0, "cpu_seconds": 0.9, "peak_rss_kib": 4096}}
    reference._json(directory / "status.json", status)
    reference._json(directory / "evaluation.json", evaluation)
    reference._json(directory / "run-spec.json", {"schema_version": 1, "synthetic_test_fixture": True})
    reference._json(directory / "experiment-specification.json", specification)
    source = Path(__file__).parents[1] / "integrations/openvins/config"
    for name in specification["configuration"]["rendered_files"]:
        shutil.copyfile(source / name, directory / ("config-" + name))
    for name in ("runner.log", "evaluation.log", "binary-sha256.txt", "dependency-versions.txt"):
        (directory / name).write_text("Synthetic test fixture.\n")
    # These tests substitute the whole already-validated numerical pipeline.
    # The real child/builder/acceptance path is exercised separately above.
    for name in ("investigation.json", "image-index.json"):
        reference._json(directory/name,{"schema_version":1,"synthetic_test_fixture":True})
    if controller is not None:
        controller.investigation_validated = True
        controller.investigation_run_state = "complete"
    run = {"schema_version": 1, "state": "complete", "native_status": status,
           "qualification": reference.qualification(evaluation), "container_wall_seconds": 1.2,
           "artifacts": reference._artifacts(directory)}
    reference._json(directory / "result.json", run)
    return run


@pytest.mark.parametrize("missing_start", [0, 400])
def test_success_requires_complete_validated_artifacts_and_keeps_failed_qualification_visible(tmp_path, monkeypatch, specification, missing_start):
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda _: True)
    monkeypatch.setattr(reference, "run_reference", lambda prepared, image, directory, **kwargs:
                        _completed_output(directory, specification, missing_start=missing_start,controller=kwargs["controller"]))
    outcome = execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
        attempt_id=str(uuid4()), checkpoint=lambda *_: None,
        evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert outcome["state"] == "succeeded" and outcome["failure"] is None
    assert outcome["result"]["qualification"]["passed"] is (missing_start == 0)
    assert outcome["result"]["evaluation"]["counts"]["estimate_samples"] == 2821 - missing_start
    files = outcome["manifest"]["files"]
    assert set(files) == execution._allowed_files(specification)
    assert outcome["result"]["artifacts"]["file_count"] == len(files)
    assert outcome["result"]["artifacts"]["manifest_sha256"] == hashlib.sha256(execution._canonical(outcome["manifest"])).hexdigest()
    assert str(tmp_path) not in json.dumps(outcome)


@pytest.mark.parametrize("corruption", ["count_type", "coordinate_convention", "artifact_tampering", "missing_file", "bad_pose",
                                        "bad_evaluation_count", "bad_coverage", "non_finite_metric"])
def test_zero_exit_and_nominal_metrics_cannot_publish_malformed_outputs(tmp_path, monkeypatch, specification, corruption):
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda _: True)
    def run(prepared, image, directory, **kwargs):
        kwargs["controller"].checkpoint("validating", {})
        result = _completed_output(directory, specification, controller=kwargs["controller"])
        if corruption == "count_type":
            result["native_status"]["counts"]["trajectory_states"] = 2821.0
        elif corruption == "coordinate_convention":
            result["native_status"]["orientation"] = "world-to-body"
        elif corruption == "artifact_tampering":
            (directory / "runner.log").write_text("modified")
        elif corruption == "missing_file":
            (directory / "evaluation.log").unlink()
        elif corruption == "bad_pose":
            with (directory / "trajectory.csv").open("a") as stream:
                stream.write("1,nan,0,0,0,0,0,1\n")
        else:
            evaluation = json.loads((directory / "evaluation.json").read_text())
            if corruption == "bad_evaluation_count":
                evaluation["counts"]["expected_camera_samples"] -= 1
            elif corruption == "bad_coverage":
                evaluation["coverage_fraction"] = 0.75
            else:
                evaluation["metrics"]["ate_rmse_m"] = float("nan")
            (directory / "evaluation.json").write_text(json.dumps(evaluation))
            # Recompute transport identities to exercise semantic validation,
            # rather than merely detecting a changed file hash.
            result["artifacts"] = reference._artifacts(directory)
            result["qualification"] = reference.qualification(evaluation)
            reference._json(directory / "result.json", result)
        return result
    monkeypatch.setattr(reference, "run_reference", run)
    outcome = execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
        attempt_id=str(uuid4()), checkpoint=lambda *_: None,
        evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert outcome["state"] == "failed" and outcome["failure"]["code"] == "numerical_failure"
    assert outcome["result"] is None and "trajectory.csv" not in outcome["manifest"]["files"]
    assert "investigation.json" not in outcome["manifest"]["files"]
    assert "image-index.json" not in outcome["manifest"]["files"]


def test_failed_cleanup_prevents_a_successful_outcome(tmp_path, monkeypatch, specification):
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "cleanup_attempt", lambda _: False)
    monkeypatch.setattr(reference, "run_reference", lambda prepared, image, directory, **kwargs:
                        _completed_output(directory, specification,controller=kwargs["controller"]))
    outcome = execution.execute_attempt(specification, tmp_path / "prepared", tmp_path / "run",
        attempt_id=str(uuid4()), checkpoint=lambda *_: None,
        evaluation_image="sha256:" + "c" * 64, configuration_parent=tmp_path)
    assert outcome["state"] == "failed" and outcome["failure"]["code"] == "infrastructure_failure"
    assert outcome["result"] is None
