"""Supervise a bounded preparation of one immutable acquisition revision."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import UUID

from .acquisition_execution import ImportFailure, MAX_HANDOFF_BYTES, supervise, verify_scratch


MAX_REQUEST_BYTES = 2 * 1024**2
MAX_FILE_BYTES = 2 * 1024**3


def container_name(attempt_id):
    value = UUID(str(attempt_id))
    if str(value) != str(attempt_id):
        raise ValueError("Revision attempts require a canonical UUID.")
    return "meridian-sensor-revision-" + value.hex


def cleanup_revision(attempt_id):
    """Remove only the exact labelled attempt before its lease is released."""
    name = container_name(attempt_id)
    query = ["docker", "container", "ls", "--all", "--filter",
             "label=meridian.component=sensor-revision", "--filter",
             f"label=meridian.attempt={attempt_id}", "--format", "{{.Names}}"]
    try:
        for _ in range(2):
            names = set(subprocess.run(query, check=True, capture_output=True,
                                       text=True, timeout=15).stdout.splitlines())
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


def container_command(source, staging, *, image, attempt_id):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("Revision image must resolve to an immutable image ID.")
    for path in (source, staging):
        if not path.is_absolute() or any(c in str(path) for c in ",\n\r"):
            raise ValueError("Revision mounts require absolute operator-controlled paths.")
    return [
        "docker", "run", "--name", container_name(attempt_id),
        "--label", "meridian.component=sensor-revision", "--label", f"meridian.attempt={attempt_id}",
        "--network", "none", "--read-only", "--user", "10001:10001",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--cpus", "1", "--memory", "1024m", "--memory-swap", "1024m",
        "--pids-limit", "64", "--log-driver", "none",
        "--ulimit", f"fsize={MAX_FILE_BYTES}:{MAX_FILE_BYTES}",
        "--ulimit", "nofile=256:256", "--ulimit", "cpu=900:900",
        "--tmpfs", "/tmp:size=32m,mode=1777,nosuid,nodev,noexec",
        "--mount", f"type=bind,source={source},target=/input/acquisition,readonly",
        "--mount", f"type=bind,source={staging},target=/work",
        "--entrypoint", "python", image, "-m", "meridian.sensor_hosted_revision",
        "/input/acquisition", "/work/revision", "/work/request.json",
    ]


def execute_revision(source, staging, *, attempt_id, checkpoint, image, request):
    source, staging = Path(source), Path(staging)
    verify_scratch(staging)
    if not source.is_absolute() or source.resolve(strict=True) != source or not source.is_dir():
        raise ImportFailure("invalid_package")
    content = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(content) > MAX_REQUEST_BYTES:
        raise ImportFailure("invalid_package")
    with (staging / "request.json").open("xb") as handle:
        handle.write(content)
    supervise(container_command(source, staging, image=image, attempt_id=attempt_id),
              attempt_id=attempt_id, checkpoint=checkpoint,
              name_for_attempt=container_name, cleanup=cleanup_revision)
    descriptor = os.open(staging / "revision/completion.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
            or set(outcome) != {"state", "prepared_input", "manifest", "result"}
            or outcome["state"] != "ready"):
        raise ImportFailure("invalid_package")
    checkpoint("publishing", {})
    return outcome
