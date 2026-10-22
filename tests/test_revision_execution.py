"""Check the private revision sandbox and its attempt-scoped handoff."""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "server"))
from workspace import revision_execution as execution
sys.path.pop(0)


def test_revision_mounts_only_source_readonly_and_bounded_scratch(tmp_path):
    attempt = str(uuid4())
    command = execution.container_command(tmp_path / "source", tmp_path / "scratch",
        image="sha256:" + "a" * 64, attempt_id=attempt)
    for key, value in {"--network": "none", "--memory": "1024m", "--memory-swap": "1024m",
                       "--cpus": "1", "--pids-limit": "64", "--user": "10001:10001"}.items():
        assert command[command.index(key) + 1] == value
    assert "--read-only" in command
    assert any("target=/input/acquisition,readonly" in item for item in command)
    assert "meridian.component=sensor-revision" in command
    assert "meridian.sensor_hosted_revision" in command
    assert not any("docker.sock" in item or "DATABASE_URL" in item for item in command)
    assert execution.container_name(attempt) in command


def test_revision_cleanup_refuses_foreign_container(monkeypatch):
    calls = []
    def invoke(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="unrelated-container\n")
    monkeypatch.setattr(execution.subprocess, "run", invoke)
    assert execution.cleanup_revision(str(uuid4())) is False
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["link", "oversized", "wrong_shape"])
def test_handoff_rejects_links_unbounded_data_and_unexpected_shape(tmp_path, monkeypatch, bad):
    source, staging = tmp_path / "source", tmp_path / "scratch"
    source.mkdir()
    staging.mkdir()
    output = staging / "revision"
    output.mkdir()
    completion = output / "completion.json"
    outcome = {"state": "ready", "prepared_input": {}, "manifest": {}, "result": {}}
    if bad == "link":
        elsewhere = tmp_path / "foreign"
        elsewhere.write_text(json.dumps(outcome))
        completion.symlink_to(elsewhere)
    elif bad == "oversized":
        completion.write_bytes(b"x" * 129)
        monkeypatch.setattr(execution, "MAX_HANDOFF_BYTES", 128)
    else:
        outcome["unexpected"] = True
        completion.write_text(json.dumps(outcome))
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    hooks = []
    monkeypatch.setattr(execution, "supervise", lambda *a, **kw: hooks.append(kw))
    with pytest.raises((ValueError, OSError)):
        execution.execute_revision(source, staging, attempt_id=str(uuid4()),
            checkpoint=lambda *_: None, image="sha256:" + "a" * 64, request={})
    assert hooks[0]["cleanup"] is execution.cleanup_revision
    assert hooks[0]["name_for_attempt"] is execution.container_name


def test_request_overflow_is_rejected_before_child_launch(tmp_path, monkeypatch):
    source, staging = tmp_path / "source", tmp_path / "scratch"
    source.mkdir()
    staging.mkdir()
    monkeypatch.setattr(execution, "verify_scratch", lambda _: None)
    monkeypatch.setattr(execution, "MAX_REQUEST_BYTES", 100)
    with pytest.raises(execution.ImportFailure):
        execution.execute_revision(source, staging, attempt_id=str(uuid4()),
            checkpoint=lambda *_: None, image="sha256:" + "a" * 64,
            request={"unbounded": "x" * 101})
    assert not (staging / "request.json").exists()
