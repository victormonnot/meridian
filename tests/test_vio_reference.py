"""Exercise isolation, failed execution, qualification, and artifact integrity."""

import csv
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

from meridian import vio_reference as reference
from meridian.vio_evaluation import TRAJECTORY_COLUMNS


def _poses(path, *, offset=0.0):
    times = [1_500_000_000_000_000_000 + i * 50_000_000 for i in range(101)]
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(TRAJECTORY_COLUMNS)
        for i, stamp in enumerate(times):
            a = i / 20
            writer.writerow([stamp, np.cos(a) + offset, np.sin(a), a / 5, 0, 0, 0, 1])
    return times


def _status(times):
    return {"schema_version": 1, "state": "complete", "error": None,
            "counts": {"trajectory_states": len(times)},
            "first_estimate_timestamp_ns": times[0], "last_estimate_timestamp_ns": times[-1],
            "timing": {"wall_seconds": 0.2, "cpu_seconds": 0.3, "peak_rss_kib": 100}}


def _runtime(tmp_path, monkeypatch, *, exitcode=0, timeout=False, corrupt_status=False):
    prepared = tmp_path / "prepared"
    for name in ("sensors", "config", "reference"):
        (prepared / name).mkdir(parents=True)
    times = _poses(prepared / "reference" / "trajectory.csv")
    reference._json(prepared / "manifest.json", {"schema_version": 1})
    monkeypatch.setattr(reference, "verify_prepared", lambda _: {"schema_version": 1})
    monkeypatch.setattr(reference, "camera_timestamps_ns", lambda _: times)
    monkeypatch.setattr(reference, "_image_identity", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(reference, "_runtime_version", lambda *_: {"protocol_version": 1})
    monkeypatch.setattr(reference, "_runtime_provenance", lambda *_: {"dependency-versions.txt": "fixture\t1\n"})
    invocations = []

    class Process:
        def __init__(self, command, **kwargs):
            invocations.append(command)
            mount = next(part for part in command if ",dst=/output" in part)
            directory = Path(mount.removeprefix("type=bind,src=").split(",dst=")[0])
            _poses(directory / "trajectory.csv")
            status = _status(times)
            if corrupt_status:
                status["counts"]["trajectory_states"] -= 1
            reference._json(directory / "status.json", status)
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if timeout_flag and self.calls == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return exitcode

    timeout_flag = timeout
    monkeypatch.setattr(reference.subprocess, "Popen", Process)
    monkeypatch.setattr(reference.subprocess, "run", lambda command, **kwargs:
                        invocations.append(command) or subprocess.CompletedProcess(command, 0))
    return prepared, invocations


def test_container_mounts_only_sensors_config_and_output(tmp_path):
    prepared, output = tmp_path / "private acquisition", tmp_path / "output"
    limits = reference._limits(2, 4096, 900)
    command = reference.container_command(prepared, output, "sha256:fixture", "run-fixture", limits)
    mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
    assert mounts == [f"type=bind,src={prepared}/sensors,dst=/sensors,readonly",
                      f"type=bind,src={prepared}/config,dst=/config,readonly",
                      f"type=bind,src={output},dst=/output"]
    assert not any("reference" in mount for mount in mounts)
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "4096m"
    assert "--read-only" in command and "--cap-drop" in command


def test_completed_run_evaluates_independently_and_records_artifacts(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    output = tmp_path / "run"
    result = reference.run_reference(prepared, "fixture", output)
    assert result["state"] == "complete" and result["qualification"]["passed"]
    assert result["qualification"]["checks"]["evaluated_samples"]
    assert set(result["artifacts"]) == {"trajectory.csv", "status.json", "evaluation.json", "run-spec.json", "runner.log", "dependency-versions.txt"}
    assert json.loads((output / "evaluation.json").read_text())["metrics"]["ate_rmse_m"] < 1e-12
    assert commands[-1][:3] == ["docker", "rm", "--force"]


@pytest.mark.parametrize("options,error", [({"exitcode": 1}, ValueError),
                                        ({"corrupt_status": True}, ValueError),
                                        ({"timeout": True}, subprocess.TimeoutExpired)])
def test_failures_remain_failed_and_cleanup_container(tmp_path, monkeypatch, options, error):
    prepared, commands = _runtime(tmp_path, monkeypatch, **options)
    output = tmp_path / "failed-run"
    with pytest.raises(error):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed" and result["error"]["type"] == error.__name__
    assert not (output / "evaluation.json").exists()
    assert commands[-1][:3] == ["docker", "rm", "--force"]
    if options.get("timeout"):
        assert any(command[:2] == ["docker", "kill"] for command in commands)


def test_invalid_or_existing_destinations_do_not_start_runtime(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    with pytest.raises(FileExistsError):
        reference.run_reference(prepared, "fixture", prepared)
    with pytest.raises(ValueError, match="outside"):
        reference.run_reference(prepared, "fixture", prepared / "new-result")
    with pytest.raises(ValueError, match="cpus"):
        reference.run_reference(prepared, "fixture", tmp_path / "new-result", cpus=float("nan"))
    assert commands == []


def test_empty_or_degenerate_evaluation_fails_qualification_without_fabricated_metrics():
    result = reference.qualification({"status": "insufficient_associations", "metrics": None,
                                      "counts": {"evaluated_camera_samples": 0},
                                      "initialization_delay_s": None})
    assert not result["passed"]
    assert not any(result["checks"].values())


def test_low_error_cannot_hide_missing_camera_coverage():
    result = reference.qualification({"status": "ok", "counts": {"evaluated_camera_samples": 100},
                                      "coverage_fraction": 0.2, "evaluated_fraction": 0.2,
                                      "reference_coverage_fraction": 1.0,
                                      "initialization_delay_s": 0,
                                      "metrics": {"ate_rmse_m": 0, "orientation_rmse_deg": 0}})
    assert result["checks"]["position_error"] and not result["passed"]


def test_repeatability_checks_identity_and_detects_artifact_tampering(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    directories = [tmp_path / "first", tmp_path / "second"]
    for directory in directories:
        reference.run_reference(prepared, "fixture", directory)
    report = reference.compare_repeats(directories)
    assert report["passed"] and report["comparisons"][0]["trajectory_bytes_identical"]
    with (directories[1] / "trajectory.csv").open("a") as stream:
        stream.write("modified after execution\n")
    with pytest.raises(ValueError, match="artifact changed"):
        reference.compare_repeats(directories)


def test_repeatability_rejects_different_resource_protocols(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    directories = [tmp_path / "first", tmp_path / "second"]
    reference.run_reference(prepared, "fixture", directories[0], cpus=1)
    reference.run_reference(prepared, "fixture", directories[1], cpus=2)
    with pytest.raises(ValueError, match="identical run specifications"):
        reference.compare_repeats(directories)


def test_dangling_output_symlink_is_not_followed(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    target, link = tmp_path / "unrelated", tmp_path / "requested"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        reference.run_reference(prepared, "fixture", link)
    assert not target.exists() and commands == []


def test_runtime_symlinks_are_not_read_or_followed_when_publishing_failure(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    output, unrelated = tmp_path / "run", tmp_path / "unrelated.json"
    unrelated.write_text('"unrelated content"')
    original = reference.subprocess.Popen

    def process_with_links(*args, **kwargs):
        process = original(*args, **kwargs)
        (output / "status.json").unlink()
        (output / "status.json").symlink_to(unrelated)
        (output / "result.json").symlink_to(unrelated)
        return process

    monkeypatch.setattr(reference.subprocess, "Popen", process_with_links)
    with pytest.raises(ValueError, match="regular files"):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed" and "status.json" not in result["artifacts"]
    assert not (output / "result.json").is_symlink()
    assert unrelated.read_text() == '"unrelated content"'


def test_input_manifest_change_during_execution_prevents_evaluation(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    original = reference.subprocess.Popen

    def process_with_changed_input(*args, **kwargs):
        process = original(*args, **kwargs)
        reference._json(prepared / "manifest.json", {"schema_version": 1, "changed": True})
        return process

    monkeypatch.setattr(reference.subprocess, "Popen", process_with_changed_input)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="inputs changed"):
        reference.run_reference(prepared, "fixture", output)
    assert not (output / "evaluation.json").exists()
    assert json.loads((output / "result.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("protocol", [True, 1.0, 2, None])
def test_runtime_version_rejects_unsupported_protocol(tmp_path, monkeypatch, protocol):
    payload = {"schema_version": 1, "protocol_version": protocol,
               "upstream_revision": reference.UPSTREAM_REVISION, "adapter_sha256": "a" * 64}
    monkeypatch.setattr(reference.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload)))
    with pytest.raises(ValueError, match="protocol"):
        reference._runtime_version("fixture", reference._limits(2, 4096, 900))


def test_failed_kill_preserves_original_timeout_and_failure_record(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch, timeout=True)
    original = reference.subprocess.run

    def failed_kill(command, **kwargs):
        if command[:2] == ["docker", "kill"]:
            raise OSError("fixture daemon unavailable")
        return original(command, **kwargs)

    monkeypatch.setattr(reference.subprocess, "run", failed_kill)
    output = tmp_path / "run"
    with pytest.raises(subprocess.TimeoutExpired):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["error"]["type"] == "TimeoutExpired" and result["kill_error"] == "OSError"


@pytest.mark.parametrize("cleanup_failure", [
    subprocess.TimeoutExpired(["docker", "rm", "--force"], 30),
    OSError("Docker cleanup is unavailable"),
])
def test_cleanup_failure_preserves_original_timeout_and_failure_record(tmp_path, monkeypatch, cleanup_failure):
    prepared, commands = _runtime(tmp_path, monkeypatch, timeout=True)
    original_run = reference.subprocess.run

    def failed_cleanup(command, **kwargs):
        if command[:3] == ["docker", "rm", "--force"]:
            raise cleanup_failure
        return original_run(command, **kwargs)

    monkeypatch.setattr(reference.subprocess, "run", failed_cleanup)
    output = tmp_path / "failed-with-cleanup-error"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        reference.run_reference(prepared, "fixture", output)
    assert caught.value.cmd == "docker"
    assert caught.value.timeout == 900
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed"
    assert result["error"] == {"type": "TimeoutExpired", "message": str(caught.value)}
    assert result["cleanup_error"] == type(cleanup_failure).__name__
    assert {"run-spec.json", "runner.log", "trajectory.csv", "status.json"} <= set(result["artifacts"])
    assert not (output / "evaluation.json").exists()
    assert any(command[:2] == ["docker", "kill"] for command in commands)
