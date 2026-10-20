"""Lease-fenced orchestration for isolated private recording imports."""
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import UUID

from django.conf import settings
from django.db import close_old_connections

from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from . import acquisition_queue as queue
from . import acquisition_storage as storage
from .acquisition_execution import ImportFailure, cleanup_import, execute_import, verify_scratch
from .models import Acquisition


FAILURES = {
    "invalid_input": "The transfer package does not match the supported import and preparation profile.",
    "invalid_package": "The transfer package does not match the supported import and preparation profile.",
    "resource_exhaustion": "The import exceeded its memory, file or storage budget.",
    "timeout": "The import exceeded its processing time limit.",
    "infrastructure_failure": "The import service was interrupted or unavailable.",
    "cancelled": "The import was cancelled.",
}


def capacity_available():
    values = [int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith("MemAvailable:")]
    return bool(values and values[0] >= 2 * 1024**2
                # Preserve the host's 10 GiB floor after at most 6 GiB publication.
                and shutil.disk_usage(settings.MERIDIAN_ACQUISITION_ROOT).free >= 16 * 1024**3
                and shutil.disk_usage(settings.MERIDIAN_IMPORT_SCRATCH_ROOT).free >= 6 * 1024**3)


def validate_storage():
    scratch, durable = settings.MERIDIAN_IMPORT_SCRATCH_ROOT, settings.MERIDIAN_ACQUISITION_ROOT
    verify_scratch(scratch)
    for root in (scratch, durable):
        if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
                or root.resolve() == Path("/")):
            raise ValueError("Import storage requires provisioned absolute directories.")
    if scratch.resolve().is_relative_to(durable.resolve()) or durable.resolve().is_relative_to(scratch.resolve()):
        raise ValueError("Import scratch and retained recordings must be separate.")
    image = settings.MERIDIAN_IMPORT_IMAGE
    if not image:
        raise ValueError("The operator must configure the import image.")
    identity = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
        raise ValueError("Import image identity is invalid.")
    settings.MERIDIAN_IMPORT_IMAGE = identity


class LeaseGuard:
    def __init__(self, acquisition_id, attempt_id, shutdown):
        self.acquisition_id, self.attempt_id, self.shutdown = acquisition_id, attempt_id, shutdown
        self.lock, self.stop, self.wake = threading.RLock(), threading.Event(), threading.Event()
        self.reason, self.phase, self.deadline = "continue", "validating", 0.0
        self.attempt_deadline = time.monotonic() + 900
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _renew(self):
        with self.lock:
            if self.reason == "lost":
                return
            phase = self.phase
        started = time.monotonic()
        try:
            reason = queue.heartbeat_import(self.acquisition_id, self.attempt_id, phase)
        except Exception:
            reason = "lost"
        with self.lock:
            if self.reason == "lost" or (self.deadline and time.monotonic() >= self.deadline):
                self.reason = "lost"
            else:
                self.reason, self.deadline = reason, started + queue.LEASE_SECONDS

    def _loop(self):
        close_old_connections()
        try:
            while not self.stop.is_set():
                self.wake.wait(5)
                self.wake.clear()
                if not self.stop.is_set():
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

    def checkpoint(self, phase="validating", progress=None):
        with self.lock:
            if phase in {"validating", "publishing"} and phase != self.phase:
                self.phase = phase
                self.wake.set()
            if self.shutdown.is_set() or self.reason == "lost" or time.monotonic() >= self.deadline:
                self.reason = "lost"
                raise AttemptLeaseLost("The import attempt no longer owns a live lease.")
            if self.reason == "cancel":
                raise AttemptCancelled("Import cancellation requested.")
            if time.monotonic() >= self.attempt_deadline:
                raise ImportFailure("timeout")


def run_one(worker_id, shutdown):
    selected = queue.claim_import(worker_id)
    if selected is None:
        return False
    acquisition, attempt_id = selected
    scratch = settings.MERIDIAN_IMPORT_SCRATCH_ROOT / str(attempt_id)
    stopped = False
    with LeaseGuard(acquisition.pk, attempt_id, shutdown) as guard:
        try:
            scratch.mkdir(mode=0o700)
            outcome = execute_import(
                storage.source_path(acquisition.pk), scratch, attempt_id=str(attempt_id),
                checkpoint=guard.checkpoint, image=settings.MERIDIAN_IMPORT_IMAGE,
                prepared_id="prepared-" + str(acquisition.pk), options=acquisition.options,
            )
            stopped = cleanup_import(str(attempt_id))
            if not stopped:
                return True
            guard.checkpoint("publishing")
            published = storage.publish_import(acquisition.pk, attempt_id,
                                               scratch / "import/result", outcome,
                                               checkpoint=guard.checkpoint)
            guard.checkpoint("publishing")
            if published:
                queue.finish_import(acquisition.pk, attempt_id, outcome)
        except AttemptLeaseLost:
            stopped = cleanup_import(str(attempt_id))
        except Exception as error:
            stopped = cleanup_import(str(attempt_id))
            if stopped:
                code = ("cancelled" if isinstance(error, AttemptCancelled)
                        else ("invalid_input" if error.code == "invalid_package" else error.code) if isinstance(error, ImportFailure)
                        else "invalid_input" if isinstance(error, ValueError)
                        else "infrastructure_failure")
                queue.finish_import(acquisition.pk, attempt_id, {
                    "state": "cancelled" if code == "cancelled" else "rejected",
                    "failure": {"code": code, "message": FAILURES[code]},
                })
        finally:
            if stopped:
                shutil.rmtree(scratch, ignore_errors=True)
    return True


def reconcile():
    for acquisition_id, attempt_id in queue.expired_imports():
        if cleanup_import(str(attempt_id)):
            scratch = settings.MERIDIAN_IMPORT_SCRATCH_ROOT / str(attempt_id)
            if scratch.exists():
                shutil.rmtree(scratch)
            queue.recover_import(acquisition_id, attempt_id)


def collect_scratch():
    """Recover scratch left by a crash after a terminal database publication."""
    for path in settings.MERIDIAN_IMPORT_SCRATCH_ROOT.iterdir():
        try:
            attempt = UUID(path.name)
        except ValueError:
            continue
        if (str(attempt) != path.name or not path.is_dir() or path.is_symlink()
                or Acquisition.objects.filter(current_attempt=attempt, state="validating").exists()):
            continue
        if cleanup_import(path.name):
            shutil.rmtree(path)
