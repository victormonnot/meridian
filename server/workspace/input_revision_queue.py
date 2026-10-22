"""Fenced preparation attempts sharing the acquisition/VIO lifecycle mutex."""
from datetime import timedelta
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import acquisition_queue, acquisition_storage
from . import input_revision_storage as storage
from .job_queue import queue_lock
from .models import Acquisition, InputRevision

LEASE_SECONDS = 30
MAX_ATTEMPTS = 2


def limits():
    return {"prepared_bytes": settings.MERIDIAN_ACQUISITION_PREPARED_BYTES, "prepared_files": 30_000,
            "per_acquisition": 20, "per_account": 50, "global_count": 200,
            "clock_mappings": 16, "request_bytes": 2 * 1024**2,
            "max_attempts": MAX_ATTEMPTS, "concurrency_global": 1}


def failure(code):
    messages = {"invalid_input": "The supplied artifacts or operations cannot prepare this recording.",
        "resource_exhaustion": "Preparation exceeded its memory, file or storage budget.",
        "timeout": "Preparation exceeded its time limit.",
        "infrastructure_failure": "The preparation service was interrupted or unavailable.",
        "cancelled": "The preparation was cancelled."}
    code = code if code in messages else "infrastructure_failure"
    return {"code": code, "message": messages[code]}


def claim_revision(worker_id):
    with transaction.atomic():
        queue_lock()
        if not settings.MERIDIAN_UPLOADS_ENABLED or acquisition_queue.compute_active():
            return None
        value = InputRevision.objects.select_related("acquisition").filter(state="queued").order_by("created_at", "id").first()
        if value is None:
            return None
        earlier = Acquisition.objects.filter(state="validating", current_attempt__isnull=True).order_by("created_at", "id").first()
        if earlier and (earlier.created_at, earlier.pk) < (value.created_at, value.pk):
            return None
        now, attempt = timezone.now(), uuid.uuid4()
        value.state, value.phase = "preparing", "preparing"
        value.current_attempt, value.worker_id = attempt, worker_id
        value.attempt_count += 1
        value.lease_until, value.failure = now + timedelta(seconds=LEASE_SECONDS), None
        value.save()
        return value, attempt


def heartbeat_revision(revision_id, attempt_id, phase):
    with transaction.atomic():
        row = queue_lock()
        now = timezone.now()
        row.import_heartbeat_at = now
        row.save(update_fields=["import_heartbeat_at"])
        value = InputRevision.objects.filter(pk=revision_id).first()
        if (not value or value.state != "preparing" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until <= now):
            return "lost"
        if phase in {"validating", "preparing", "publishing"}:
            value.phase = phase
        value.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        value.save(update_fields=["phase", "lease_until", "updated_at"])
        return "cancel" if value.cancel_requested_at else "continue"


def release_files_locked(value):
    storage.cleanup(value.pk)
    value.files_available = False
    value.reserved_bytes = value.retained_bytes = 0
    value.reserved_files = value.retained_files = 0
    value.deleted_at = timezone.now()


def finish_revision(revision_id, attempt_id, outcome):
    with transaction.atomic():
        queue_lock()
        now = timezone.now()
        value = InputRevision.objects.select_related("acquisition").filter(pk=revision_id).first()
        if (not value or value.state != "preparing" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until <= now):
            return False
        state = outcome.get("state")
        if state not in {"ready", "rejected", "cancelled"}:
            raise ValueError("Preparation outcome must be terminal.")
        if value.cancel_requested_at:
            state = "cancelled"
        if state == "ready":
            total, count = storage.validate_outcome(value, outcome)
            name = f"revisions/{value.pk}/published/{attempt_id}/publication-manifest.json"
            expected = acquisition_storage.canonical(outcome["manifest"]) + b"\n"
            with acquisition_storage.open_relative(acquisition_storage.root(), name) as stream:
                if stream.read(len(expected) + 1) != expected:
                    raise ValueError("Published revision manifest is missing or changed.")
            value.prepared_input, value.manifest, value.result = outcome["prepared_input"], outcome["manifest"], outcome["result"]
            value.published_attempt, value.files_available, value.failure = attempt_id, True, None
            value.reserved_bytes = value.retained_bytes = total
            value.reserved_files = value.retained_files = count
        else:
            value.failure = failure("cancelled" if state == "cancelled" else (outcome.get("failure") or {}).get("code"))
            release_files_locked(value)
        value.state, value.phase, value.finished_at = state, "complete", now
        value.lease_until, value.current_attempt, value.worker_id = None, None, None
        value.save()
        return True


def expired_revisions():
    return list(InputRevision.objects.filter(state="preparing", current_attempt__isnull=False,
        lease_until__lte=timezone.now()).values_list("id", "current_attempt"))


def recover_revision(revision_id, attempt_id):
    """Caller must confirm the matching child and scratch have been removed."""
    with transaction.atomic():
        queue_lock()
        value = InputRevision.objects.filter(pk=revision_id).first()
        if (not value or value.state != "preparing" or value.current_attempt != attempt_id
                or not value.lease_until or value.lease_until > timezone.now()):
            return False
        storage.discard_attempt(value.pk, attempt_id)
        terminal = value.cancel_requested_at or value.attempt_count >= MAX_ATTEMPTS
        if terminal:
            value.state = "cancelled" if value.cancel_requested_at else "rejected"
            value.phase, value.finished_at = "complete", timezone.now()
            release_files_locked(value)
        else:
            value.state, value.phase = "queued", "queued"
        value.failure = failure("cancelled" if value.cancel_requested_at else "infrastructure_failure")
        value.current_attempt, value.worker_id, value.lease_until = None, None, None
        value.save()
        return True


def cleanup_abandoned():
    with transaction.atomic():
        queue_lock()
        if acquisition_queue.compute_active():
            return
        for value in InputRevision.objects.filter(state__in=["rejected", "cancelled"], reserved_bytes__gt=0):
            release_files_locked(value)
            value.phase = "complete"
            value.save()
        if not storage.root().exists():
            return
        known = {str(value) for value in InputRevision.objects.values_list("id", flat=True)}
        for path in storage.root().iterdir():
            try:
                canonical = str(uuid.UUID(path.name)) == path.name
            except ValueError:
                continue
            if canonical and path.name not in known:
                storage.cleanup(path.name)
