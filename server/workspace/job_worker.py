"""Trusted compute orchestration, deliberately not imported by HTTP views."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from meridian.vio_specification import specification_sha256, validate_specification
from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from . import job_queue as queue
from .models import Job

ARTIFACT_BYTES = 32 * 1024 * 1024
ARTIFACT_FILES = 40


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def capacity_available():
    memory_kib = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            memory_kib = int(line.split()[1])
            break
    return bool(memory_kib is not None and memory_kib >= settings.MERIDIAN_MIN_AVAILABLE_MEMORY_MB * 1024
                and shutil.disk_usage(settings.MERIDIAN_JOB_ARTIFACT_ROOT).free
                >= settings.MERIDIAN_MIN_FREE_DISK_GB * 1024**3)


def validate_storage():
    roots = [settings.MERIDIAN_JOB_ARTIFACT_ROOT, settings.MERIDIAN_JOB_SCRATCH_ROOT]
    for root in roots:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root == Path("/"):
            raise ValueError("Compute storage must be provisioned at absolute, non-symlink directories")
    if roots[0] == roots[1] or any(a.is_relative_to(b) for a, b in (roots, roots[::-1])):
        raise ValueError("Durable storage and scratch must be separate directories")
    # The deployment provisions a dedicated host tmpfs mounted at the same path
    # in worker and child containers. This is a hard aggregate scratch bound.
    scratch = roots[1]
    found = False
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mountpoint = before.split()[4].replace("\\040", " ")
        if Path(mountpoint) == scratch and after.split()[0] == "tmpfs":
            found = True
    size = os.statvfs(scratch)
    if not found or size.f_blocks * size.f_frsize > 128 * 1024 * 1024:
        raise ValueError("Compute scratch must be a dedicated tmpfs of at most 128 MiB")
    if not settings.MERIDIAN_EVALUATION_IMAGE:
        raise ValueError("The operator must configure the pinned evaluation image")
    from meridian.vio_reference import _image_identity
    settings.MERIDIAN_EVALUATION_IMAGE = _image_identity(settings.MERIDIAN_EVALUATION_IMAGE)


class LeaseGuard:
    """Heartbeat independently of hashing/evaluation, with fail-closed callbacks."""
    def __init__(self, job_id, attempt_id, shutdown):
        self.job_id, self.attempt_id, self.shutdown = job_id, attempt_id, shutdown
        self.phase, self.reason = "preparing", "continue"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.deadline = 0.0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _renew(self):
        with self.lock:
            if self.reason == "lost":
                return
            phase = self.phase
        started = time.monotonic()
        try:
            reason = queue.heartbeat(self.job_id, self.attempt_id, phase)
        except Exception:
            reason = "lost"
        # Never hold the in-memory lock during database/network I/O. The
        # subprocess supervisor must remain able to enforce its own deadline.
        with self.lock:
            if self.reason == "lost" or (self.deadline and time.monotonic() >= self.deadline):
                self.reason = "lost"
            else:
                self.reason = reason
                self.deadline = started + queue.LEASE_SECONDS

    def _loop(self):
        close_old_connections()
        try:
            while not self.stop.is_set():
                self.wake.wait(5)
                self.wake.clear()
                if self.stop.is_set():
                    break
                self._renew()
        finally:
            close_old_connections()

    def __enter__(self):
        self._renew()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=10)

    def checkpoint(self, phase, progress=None):
        phase = {"running": "estimating", "validating": "evaluating"}.get(phase, phase)
        with self.lock:
            if phase != self.phase and phase in ("preparing", "estimating", "evaluating"):
                self.phase = phase
                self.wake.set()
            if self.shutdown.is_set() or self.reason == "lost" or time.monotonic() >= self.deadline:
                self.reason = "lost"
                raise AttemptLeaseLost("The execution attempt no longer owns a live lease")
            if self.reason == "cancel":
                raise AttemptCancelled("Cancellation requested")


def publish_files(job_id, attempt_id, staging, outcome):
    """Recheck and atomically promote only files named by a bounded manifest."""
    manifest = outcome.get("manifest")
    if not manifest:
        return False
    if set(manifest) != {"schema_version", "files"} or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("Invalid artifact manifest")
    files = manifest["files"]
    if files == {} and outcome["state"] != "succeeded":
        return False
    if not isinstance(files, dict) or not 1 <= len(files) <= ARTIFACT_FILES:
        raise ValueError("Invalid artifact count")
    total = 0
    for name, identity in files.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", name):
            raise ValueError("Artifact names must be simple relative filenames")
        if not isinstance(identity, dict) or set(identity) != {"size_bytes", "sha256"}:
            raise ValueError("Invalid artifact identity")
        if type(identity["size_bytes"]) is not int or identity["size_bytes"] < 0:
            raise ValueError("Invalid artifact size")
        if not isinstance(identity["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", identity["sha256"]):
            raise ValueError("Invalid artifact digest")
        total += identity["size_bytes"]
    if total > ARTIFACT_BYTES:
        raise ValueError("Artifact storage budget exceeded")
    encoded = canonical(manifest)
    result = outcome.get("result")
    if outcome["state"] == "succeeded":
        if len(canonical(result)) > 256 * 1024:
            raise ValueError("Result summary is too large")
        expected = {"file_count": len(files), "total_bytes": total, "manifest_sha256": sha256(encoded).hexdigest()}
        if result.get("artifacts") != expected:
            raise ValueError("Result and artifact manifest disagree")
    parent = settings.MERIDIAN_JOB_ARTIFACT_ROOT / str(job_id)
    parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / str(attempt_id)
    temporary = parent / (str(attempt_id) + ".publishing")
    if parent.is_symlink() or destination.exists() or temporary.exists():
        raise ValueError("Attempt storage already exists or is unsafe")
    temporary.mkdir(mode=0o700)
    try:
        for name, identity in files.items():
            source = staging / name
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != identity["size_bytes"]:
                    raise ValueError("Artifact is not the declared regular file")
                digest, size = sha256(), 0
                with (temporary / name).open("xb") as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > identity["size_bytes"]:
                            raise ValueError("Artifact grew during publication")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != identity["size_bytes"] or digest.hexdigest() != identity["sha256"]:
                    raise ValueError("Artifact changed before publication")
        with (temporary / "publication-manifest.json").open("xb") as output:
            output.write(encoded + b"\n")
            output.flush()
            os.fsync(output.fileno())
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def run_one(worker_id, shutdown):
    from .job_execution import cleanup_attempt, execute_attempt
    selected = queue.claim(worker_id)
    if selected is None:
        return False
    job, attempt = selected
    scratch = settings.MERIDIAN_JOB_SCRATCH_ROOT / str(attempt.pk)
    scratch.mkdir(mode=0o700)
    configuration_parent = scratch
    staging = scratch / "run"
    stopped = False
    validated_input = False
    with LeaseGuard(job.pk, attempt.pk, shutdown) as guard:
        try:
            validate_specification(job.specification)
            if specification_sha256(job.specification) != job.specification_sha256:
                raise ValueError("Stored specification identity mismatch")
            validated_input = True
            outcome = execute_attempt(job.specification, settings.MERIDIAN_VIO_PREPARED, staging,
                                      attempt_id=str(attempt.pk), checkpoint=guard.checkpoint,
                                      evaluation_image=settings.MERIDIAN_EVALUATION_IMAGE,
                                      configuration_parent=configuration_parent)
            # A failed cleanup must keep occupying the global slot. Lease
            # recovery will retry removal before admitting any later job.
            stopped = cleanup_attempt(str(attempt.pk))
            if not stopped:
                return True
            guard.checkpoint(guard.phase)
            published = publish_files(job.pk, attempt.pk, staging, outcome)
            guard.checkpoint(guard.phase)
            queue.finish(job.pk, attempt.pk, outcome, artifacts_available=published)
        except AttemptLeaseLost:
            stopped = cleanup_attempt(str(attempt.pk))
        except Exception as exc:
            stopped = cleanup_attempt(str(attempt.pk))
            if stopped:
                code = ("cancelled" if isinstance(exc, AttemptCancelled) else
                        ("numerical_failure" if validated_input else "invalid_input")
                        if isinstance(exc, ValueError) else "infrastructure_failure")
                queue.finish(job.pk, attempt.pk, {"state": "cancelled" if code == "cancelled" else "failed",
                                                "failure": queue.failure(code), "result": None, "manifest": None})
        finally:
            if stopped:
                shutil.rmtree(scratch, ignore_errors=True)
    return True


def _uuid_directory(path):
    try:
        return path.is_dir() and not path.is_symlink() and str(uuid.UUID(path.name)) == path.name
    except ValueError:
        return False


def reconcile():
    """Stop abandoned child processes before making their global slot reusable."""
    from .job_execution import cleanup_attempt
    for job_id, attempt_id in queue.expired_attempts():
        if cleanup_attempt(str(attempt_id)):
            queue.recover(job_id, attempt_id)


def collect_artifacts():
    """Private artifacts expire after seven days; explicit deletion also purges them."""
    from .job_execution import cleanup_attempt
    root = settings.MERIDIAN_JOB_ARTIFACT_ROOT
    for directory in root.iterdir():
        if not _uuid_directory(directory):
            continue
        with transaction.atomic():
            queue.queue_lock()
            job = Job.objects.filter(pk=directory.name).first()
            if job is None:
                shutil.rmtree(directory)
                continue
            expired = job.artifacts_expire_at and job.artifacts_expire_at <= timezone.now()
            for child in directory.iterdir():
                name = child.name.removesuffix(".publishing")
                try:
                    attempt_id = uuid.UUID(name)
                except ValueError:
                    continue
                active = job.state in queue.RUNNING and job.current_attempt == attempt_id
                retained = job.published_attempt == attempt_id and not expired
                if child.is_dir() and not child.is_symlink() and not active and not retained:
                    shutil.rmtree(child)
            if expired and job.artifacts_available:
                job.artifacts_available = False
                job.save(update_fields=["artifacts_available", "updated_at"])
    for directory in settings.MERIDIAN_JOB_SCRATCH_ROOT.iterdir():
        if not _uuid_directory(directory):
            continue
        if not Job.objects.filter(current_attempt=directory.name, state__in=queue.RUNNING).exists():
            if cleanup_attempt(directory.name):
                shutil.rmtree(directory)
