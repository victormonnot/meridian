"""Exercise import sandbox boundaries with real pipes and substituted Docker lifecycle."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


SPEC = importlib.util.spec_from_file_location(
    "acquisition_execution_test", Path(__file__).parents[1] / "server/workspace/acquisition_execution.py")
execution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(execution)


def real_process(monkeypatch, script, *, oom=False):
    original = subprocess.Popen
    children, commands = [], []
    removed = False
    def start(command, **kwargs):
        child = original([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child
    def docker(command, **kwargs):
        nonlocal removed
        commands.append(command)
        if command[:3] == ["docker", "container", "ls"]:
            attempt = next(value.rsplit("=", 1)[1] for value in command if value.startswith("label=meridian.attempt="))
            return subprocess.CompletedProcess(command, 0,
                stdout="" if removed else execution.container_name(attempt) + "\n")
        if command[:2] == ["docker", "rm"]:
            removed = True
            for child in children:
                if child.poll() is None:
                    child.terminate()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"OOMKilled": oom}))
    monkeypatch.setattr(execution.subprocess, "Popen", start)
    monkeypatch.setattr(execution.subprocess, "run", docker)
    return children, commands


def test_command_has_immutable_input_and_hard_compute_limits(tmp_path):
    attempt = str(uuid4())
    command = execution.container_command(tmp_path / "archive", tmp_path / "scratch",
        image="sha256:" + "a" * 64, attempt_id=attempt)
    for key, value in {"--network": "none", "--memory": "1024m", "--memory-swap": "1024m",
                       "--cpus": "1", "--pids-limit": "64", "--log-driver": "none",
                       "--user": "10001:10001"}.items():
        assert command[command.index(key) + 1] == value
    assert "--read-only" in command and "--rm" not in command
    assert any("target=/input/archive,readonly" in value for value in command)
    assert not any("docker.sock" in value or "DATABASE_URL" in value for value in command)
    assert execution.container_name(attempt) in command


def test_successful_process_is_reaped(tmp_path, monkeypatch):
    children, commands = real_process(monkeypatch, "print('completed')")
    events = []
    execution.supervise(["substituted"], attempt_id=str(uuid4()), checkpoint=lambda *args: events.append(args), timeout=2)
    assert children[0].poll() == 0 and events
    assert any(command[:3] == ["docker", "rm", "--force"] for command in commands)


@pytest.mark.parametrize("error", [execution.AttemptCancelled, execution.AttemptLeaseLost])
def test_cancellation_and_lost_lease_stop_child_with_closed_stdout(monkeypatch, error):
    children, _ = real_process(monkeypatch, "import os,time; os.close(1); os.close(2); time.sleep(20)")
    calls = []
    def checkpoint(*args):
        calls.append(args)
        if len(calls) >= 2:
            raise error()
    with pytest.raises(error):
        execution.supervise(["substituted"], attempt_id=str(uuid4()), checkpoint=checkpoint, timeout=4)
    assert children[0].poll() is not None


@pytest.mark.parametrize("mode,code", [("timeout", "timeout"), ("log", "resource_exhaustion"),
                                       ("oom", "resource_exhaustion"), ("reject", "invalid_package")])
def test_resource_and_invalid_package_failures_are_explicit(monkeypatch, mode, code):
    script = {"timeout": "import time;time.sleep(20)", "log": "print('x'*10000)",
              "oom": "raise SystemExit(137)", "reject": "raise SystemExit(2)"}[mode]
    children, _ = real_process(monkeypatch, script, oom=mode == "oom")
    monkeypatch.setattr(execution, "MAX_LOG_BYTES", 128)
    with pytest.raises(execution.ImportFailure) as caught:
        execution.supervise(["substituted"], attempt_id=str(uuid4()), checkpoint=lambda *_: None,
                            timeout=0.05 if mode == "timeout" else 2)
    assert caught.value.code == code and children[0].poll() is not None


def test_cleanup_refuses_unrecognized_container_names(monkeypatch):
    calls = []
    def docker(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="unrelated-container\n")
    monkeypatch.setattr(execution.subprocess, "run", docker)
    assert execution.cleanup_import(str(uuid4())) is False
    assert len(calls) == 1 and calls[0][:3] == ["docker", "container", "ls"]


@pytest.mark.parametrize("filesystem,size,accepted", [("ext4", 8 * 1024**3, True),
    ("ext4", 100 * 1024**3, False), ("tmpfs", 8 * 1024**3, False), ("ext4", 128 * 1024**2, False)])
def test_scratch_requires_finite_dedicated_disk_filesystem(tmp_path, monkeypatch, filesystem, size, accepted):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw:
        f"1 0 0:1 / {tmp_path} rw,nosuid,nodev,noexec - {filesystem} /dev/loop0 rw\n")
    monkeypatch.setattr(execution.os, "statvfs", lambda _: SimpleNamespace(f_blocks=size // 4096, f_frsize=4096))
    if accepted:
        execution.verify_scratch(tmp_path)
    else:
        with pytest.raises(ValueError, match="scratch"):
            execution.verify_scratch(tmp_path)
