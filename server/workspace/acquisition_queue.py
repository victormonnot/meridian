"""Quota reservations and import leases share the VIO compute mutex.

All mutations lock ComputeQueue before Project. Expired imports retain their
compute slot and storage reservations until their sandbox has been stopped.
"""
from datetime import timedelta
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import acquisition_storage as storage
from .job_queue import queue_lock
from .models import Acquisition, ComputeQueue, Job, InputRevision

LEASE_SECONDS = 30
MAX_ATTEMPTS = 2
UPLOAD_HOURS = 24
MESSAGES = {
    "invalid_input": "The archive does not satisfy the supported sensor-package profile.",
    "resource_exhaustion": "The import exceeded its memory, file or storage budget.",
    "timeout": "The import exceeded its time limit.",
    "infrastructure_failure": "The import service was interrupted or unavailable.",
    "cancelled": "The import was cancelled.",
    "expired_upload": "The incomplete transfer expired after 24 hours without progress.",
}


def limits():
    return {"upload_bytes": settings.MERIDIAN_ACQUISITION_UPLOAD_BYTES,
            "unpacked_bytes": settings.MERIDIAN_ACQUISITION_UNPACKED_BYTES,
            "prepared_bytes": settings.MERIDIAN_ACQUISITION_PREPARED_BYTES,
            "account_bytes": settings.MERIDIAN_ACQUISITION_ACCOUNT_BYTES,
            "global_bytes": settings.MERIDIAN_ACQUISITION_GLOBAL_BYTES,
            "chunk_bytes": settings.MERIDIAN_ACQUISITION_CHUNK_BYTES,
            "per_project": 10, "per_account": 20, "global_count": 100,
            "members": 30_000, "rows": 100_000, "image_pixels": 16_000_000,
            "reserved_files_per_import": 60_000, "account_files": 120_000, "global_files": 240_000,
            "upload_inactivity_hours": UPLOAD_HOURS, "concurrency_global": 1,
            "max_attempts": MAX_ATTEMPTS}


def usage(owner=None):
    totals = {key: 0 for key in ("reserved_bytes", "retained_bytes", "reserved_files", "retained_files")}
    for model in (Acquisition, InputRevision):
        query = model.objects.all()
        if owner is not None:
            query = query.filter(owner=owner)
        aggregate = query.aggregate(**{key: Sum(key) for key in totals})
        for key, value in aggregate.items():
            totals[key] += value or 0
    return totals


def failure(code):
    code = code if code in MESSAGES else "infrastructure_failure"
    return {"code": code, "message": MESSAGES[code]}


def pulse_import(capacity_available=None):
    with transaction.atomic():
        row = queue_lock()
        row.import_heartbeat_at = timezone.now()
        if capacity_available is not None:
            row.import_capacity_available = capacity_available
        row.save(update_fields=["import_heartbeat_at", "import_capacity_available"])


def service_status():
    row = ComputeQueue.objects.filter(pk=1).first()
    alive = bool(row and row.import_heartbeat_at and row.import_heartbeat_at > timezone.now() - timedelta(seconds=45))
    return {"enabled": settings.MERIDIAN_UPLOADS_ENABLED, "worker_available": alive,
            "capacity_available": bool(alive and row.import_capacity_available)}


def compute_active():
    return (Job.objects.filter(state__in=["running", "evaluating"]).exists()
            or Acquisition.objects.filter(state="validating", current_attempt__isnull=False).exists()
            or InputRevision.objects.filter(state="preparing", current_attempt__isnull=False).exists())


def claim_import(worker_id):
    with transaction.atomic():
        queue_lock()
        if not settings.MERIDIAN_UPLOADS_ENABLED or compute_active():
            return None
        value = Acquisition.objects.filter(state="validating", current_attempt__isnull=True).order_by("created_at", "id").first()
        if value is None:
            return None
        earlier = InputRevision.objects.filter(state="queued").order_by("created_at", "id").first()
        if earlier and (earlier.created_at, earlier.pk) < (value.created_at, value.pk):
            return None
        now, attempt = timezone.now(), uuid.uuid4()
        value.current_attempt, value.worker_id = attempt, worker_id
        value.attempt_count += 1
        value.phase, value.lease_until = "validating", now + timedelta(seconds=LEASE_SECONDS)
        value.failure = None
        value.save()
        return value, attempt


def heartbeat_import(acquisition_id, attempt_id, phase):
    with transaction.atomic():
        row = queue_lock()
        now = timezone.now()
        row.import_heartbeat_at = now
        row.save(update_fields=["import_heartbeat_at"])
        value = Acquisition.objects.filter(pk=acquisition_id).first()
        if (not value or value.state != "validating" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until <= now):
            return "lost"
        if phase in {"validating", "preparing", "publishing"}:
            value.phase = phase
        value.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        value.save(update_fields=["phase", "lease_until", "updated_at"])
        return "cancel" if value.cancel_requested_at else "continue"


def release_files_locked(value):
    """Reservation is released only after all retained files are removed."""
    storage.cleanup(value.pk)
    value.files_available = False
    value.reserved_bytes = value.retained_bytes = 0
    value.reserved_files = value.retained_files = 0
    value.deleted_at = timezone.now()


def finish_import(acquisition_id, attempt_id, outcome):
    from meridian.sensor_contract import validate_contract
    with transaction.atomic():
        queue_lock()
        now = timezone.now()
        value = Acquisition.objects.filter(pk=acquisition_id).first()
        if (not value or value.state != "validating" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until <= now):
            return False
        state = outcome.get("state")
        if state not in {"ready", "rejected", "cancelled"}:
            raise ValueError("Import outcome must be terminal.")
        if value.cancel_requested_at:
            state = "cancelled"
        if state == "ready":
            descriptor = outcome.get("descriptor")
            validate_contract(descriptor)
            if descriptor["kind"] != "acquisition" or len(storage.canonical(descriptor)) > 32 * 1024**2:
                raise ValueError("Invalid published acquisition descriptor.")
            if not isinstance(outcome.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", outcome["sha256"]):
                raise ValueError("Successful publication requires the original archive identity.")
            manifest, result = outcome["manifest"], outcome["result"]
            total = storage.checked_manifest(manifest)
            manifest_path = f"{value.pk}/published/{attempt_id}/publication-manifest.json"
            with storage.open_relative(storage.root(), manifest_path) as stream:
                expected = storage.canonical(manifest) + b"\n"
                if stream.read(len(expected) + 1) != expected:
                    raise ValueError("Published manifest is unavailable or changed.")
            if (total + value.size_bytes > value.reserved_bytes
                    or len(manifest["files"]) > value.reserved_files
                    or len(storage.canonical(result)) > 256 * 1024):
                raise ValueError("Publication exceeds its reservation or summary budget.")
            value.descriptor, value.result, value.manifest = descriptor, result, manifest
            value.sha256, value.published_attempt = outcome["sha256"], attempt_id
            value.files_available, value.failure = True, None
            # The manifest itself is small but is real retained storage.
            value.retained_bytes = total + value.size_bytes + len(expected)
            if value.retained_bytes > value.reserved_bytes:
                raise ValueError("Publication metadata exceeds the byte reservation.")
            value.reserved_bytes = value.retained_bytes
            value.retained_files = len(manifest["files"]) + 2  # Original and publication manifest.
            if value.retained_files > value.reserved_files:
                raise ValueError("Publication metadata exceeds the file reservation.")
            value.reserved_files = value.retained_files
        else:
            value.failure = failure("cancelled" if state == "cancelled" else (outcome.get("failure") or {}).get("code"))
            release_files_locked(value)
        value.state, value.phase, value.finished_at = state, "complete", now
        value.lease_until, value.current_attempt, value.worker_id = None, None, None
        value.save()
        return True


def expired_imports():
    return list(Acquisition.objects.filter(state="validating", current_attempt__isnull=False,
                                          lease_until__lte=timezone.now()).values_list("id", "current_attempt"))


def recover_import(acquisition_id, attempt_id):
    """Call only after the corresponding child container and scratch are gone."""
    with transaction.atomic():
        queue_lock()
        value = Acquisition.objects.filter(pk=acquisition_id).first()
        if (not value or value.state != "validating" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until > timezone.now()):
            return False
        storage.discard_attempt(value.pk, attempt_id)
        terminal = value.cancel_requested_at or value.attempt_count >= MAX_ATTEMPTS
        if terminal:
            value.state = "cancelled" if value.cancel_requested_at else "rejected"
            value.phase, value.finished_at = "complete", timezone.now()
            release_files_locked(value)
        else:
            value.phase = "queued"
        value.failure = failure("cancelled" if value.cancel_requested_at else "infrastructure_failure")
        value.current_attempt, value.worker_id, value.lease_until = None, None, None
        value.save()
        return True


def cleanup_abandoned():
    """Expire inactive transfers and retry cleanup after prior filesystem errors."""
    cleaned = 0
    with transaction.atomic():
        queue_lock()
        now = timezone.now()
        busy = compute_active()
        for value in Acquisition.objects.filter(state="receiving", expires_at__lte=now):
            value.state, value.phase, value.finished_at = "cancelled", "complete", now
            value.failure = failure("expired_upload")
            if busy:
                value.phase = "cleanup_pending"
                value.save()
            else:
                release_files_locked(value)
                value.save()
                cleaned += 1
        if busy:
            return cleaned
        for value in Acquisition.objects.filter(state__in=["rejected", "cancelled"], reserved_bytes__gt=0):
            release_files_locked(value)
            value.phase = "complete"
            value.save()
            cleaned += 1
        # A process can die after creating storage but before its DB transaction
        # commits. The mutex prevents removing any in-flight valid creation.
        if settings.MERIDIAN_UPLOADS_ENABLED and settings.MERIDIAN_ACQUISITION_ROOT.exists():
            known = {str(value) for value in Acquisition.objects.values_list("id", flat=True)}
            for path in storage.root().iterdir():
                try:
                    canonical = str(uuid.UUID(path.name)) == path.name
                except ValueError:
                    continue
                if canonical and path.name not in known:
                    storage.cleanup(path.name)
                    cleaned += 1
    return cleaned
