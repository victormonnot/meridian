"""Isolated sensor-import process supervision; no HTTP or database operations."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
from uuid import UUID

from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost


MAX_SCRATCH_BYTES = 8 * 1024**3
MAX_FILE_BYTES = 4 * 1024**3
MAX_LOG_BYTES = 2 * 1024**2
MAX_HANDOFF_BYTES = 32 * 1024**2
TIMEOUT_SECONDS = 900


class ImportFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def container_name(attempt_id):
    value = UUID(str(attempt_id))
    if str(value) != str(attempt_id):
        raise ValueError("Import attempts require a canonical UUID.")
    return "meridian-sensor-import-" + value.hex


def cleanup_import(attempt_id):
    """Stop only this labelled attempt; failure keeps the shared slot occupied."""
    name = container_name(attempt_id)
    query = ["docker", "container", "ls", "--all", "--filter",
             "label=meridian.component=sensor-import", "--filter",
             f"label=meridian.attempt={attempt_id}", "--format", "{{.Names}}"]
    try:
        for _ in range(2):
            found = subprocess.run(query, check=True, capture_output=True, text=True, timeout=15)
            names = set(found.stdout.splitlines())
            if not names:
                return True
            if names != {name}:
                return False
            subprocess.run(["docker", "rm", "--force", name], check=True,
                           capture_output=True, timeout=30)
        return not subprocess.run(query, check=True, capture_output=True,
                                  text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def verify_scratch(path):
    """Require a finite dedicated disk filesystem, never the VIO tmpfs or host root."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("Import scratch must be a provisioned absolute directory.")
    path = path.resolve(strict=True)
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), before.split()[4])
        root = Path(mountpoint)
        if path == root or path.is_relative_to(root):
            mounts.append((len(root.parts), root, after.split()[0]))
    capacity = os.statvfs(path)
    if (not mounts or max(mounts)[2] != "ext4"
            or not 6 * 1024**3 < capacity.f_blocks * capacity.f_frsize <= MAX_SCRATCH_BYTES):
        raise ValueError("Import scratch must use its dedicated ext4 filesystem of at most 8 GiB.")


def container_command(source, staging, *, image, attempt_id):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("Import image must be resolved to an immutable local image ID.")
    for path in (source, staging):
        if not path.is_absolute() or any(c in str(path) for c in ",\n\r"):
            raise ValueError("Import mounts require absolute operator-controlled paths.")
    return [
        "docker", "run", "--name", container_name(attempt_id),
        "--label", "meridian.component=sensor-import", "--label", f"meridian.attempt={attempt_id}",
        "--network", "none", "--read-only", "--user", "10001:10001",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--cpus", "1", "--memory", "1024m", "--memory-swap", "1024m",
        "--pids-limit", "64", "--log-driver", "none",
        "--ulimit", f"fsize={MAX_FILE_BYTES}:{MAX_FILE_BYTES}",
        "--ulimit", "nofile=256:256", "--ulimit", "cpu=900:900",
        "--tmpfs", "/tmp:size=32m,mode=1777,nosuid,nodev,noexec",
        "--mount", f"type=bind,source={source},target=/input/archive,readonly",
        "--mount", f"type=bind,source={staging},target=/work",
        "--entrypoint", "python", image, "-m", "meridian.sensor_hosted_import",
        "/input/archive", "/work/import", "/work/request.json",
    ]


def supervise(command, *, attempt_id, checkpoint, timeout=TIMEOUT_SECONDS):
    """Drain bounded logs while wall time, cancellation and lease expiry remain live."""
    process, captured, started = None, io.BytesIO(), time.monotonic()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            next_check = 0.0
            while selector.get_map() or process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    raise ImportFailure("timeout")
                if elapsed >= next_check:
                    checkpoint("validating", {"elapsed_seconds": round(elapsed, 3)})
                    next_check = elapsed + 1
                for key, _ in selector.select(timeout=min(0.2, timeout - elapsed)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if captured.tell() + len(chunk) > MAX_LOG_BYTES:
                        raise ImportFailure("resource_exhaustion")
                    captured.write(chunk)
            code = process.wait(timeout=5)
        checkpoint("validating", {})
        inspected = subprocess.run(["docker", "inspect", "--format", "{{json .State}}",
                                    container_name(attempt_id)], check=True, capture_output=True,
                                   text=True, timeout=15)
        if json.loads(inspected.stdout).get("OOMKilled") or code in {137, 152, 153}:
            raise ImportFailure("resource_exhaustion")
        if code:
            raise ImportFailure("invalid_package" if code == 2 else "infrastructure_failure")
    finally:
        # The worker separately confirms cleanup before releasing any lease.
        cleanup_import(attempt_id)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def execute_import(source, staging, *, attempt_id, checkpoint, image, prepared_id, options):
    source, staging = Path(source), Path(staging)
    verify_scratch(staging)
    metadata = source.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_FILE_BYTES):
        raise ImportFailure("invalid_package")
    request = {"prepared_id": prepared_id, "imu_stream_id": options["imu_stream_id"],
               "include_reference": options["include_reference"]}
    with (staging / "request.json").open("x", encoding="utf-8") as handle:
        json.dump(request, handle, sort_keys=True, allow_nan=False)
    command = container_command(source, staging, image=image, attempt_id=attempt_id)
    supervise(command, attempt_id=attempt_id, checkpoint=checkpoint)
    completion = staging / "import/completion.json"
    descriptor = os.open(completion, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > MAX_HANDOFF_BYTES):
            raise ImportFailure("invalid_package")
        content = handle.read(MAX_HANDOFF_BYTES + 1)
    if len(content) > MAX_HANDOFF_BYTES:
        raise ImportFailure("invalid_package")
    outcome = json.loads(content)
    if (type(outcome) is not dict
            or set(outcome) != {"state", "descriptor", "result", "manifest", "sha256"}
            or outcome["state"] != "ready"):
        raise ImportFailure("invalid_package")
    checkpoint("publishing", {})
    return outcome
