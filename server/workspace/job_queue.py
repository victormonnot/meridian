"""Durable PostgreSQL queue with one active computation and fenced publication.

Every lifecycle mutation takes the queue row lock first. Project deletion takes
its project lock and refuses active jobs; submission takes that same project
lock after the queue lock. No compute process runs inside a database transaction.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Acquisition, ComputeQueue, Job, JobAttempt

ACTIVE = ("queued", "running", "evaluating")
RUNNING = ("running", "evaluating")
TERMINAL = ("succeeded", "failed", "cancelled")
LEASE_SECONDS = 30
MAX_ATTEMPTS = 2
RETENTION_DAYS = 7
FAILURES = {
    "invalid_input": "The saved inputs do not match the supported execution profile.",
    "initialization_failure": "The estimator did not initialize on this recording.",
    "numerical_failure": "The estimator output failed numerical or structural validation.",
    "resource_exhaustion": "The execution exceeded its memory or storage budget.",
    "timeout": "The execution exceeded its time limit.",
    "infrastructure_failure": "The execution service was interrupted or unavailable.",
    "cancelled": "The execution was cancelled.",
}


def limits():
    return {"active_per_account": 2, "active_global": 8,
            "retained_per_account": 20, "retained_global": 200,
            "concurrency_global": 1, "max_attempts": MAX_ATTEMPTS,
            "submissions_per_account_24h": 10, "submissions_global_24h": 100,
            "artifact_retention_days": RETENTION_DAYS}


def enabled():
    return settings.MERIDIAN_EXECUTION_ENABLED


def queue_lock():
    # get_or_create also supports a freshly flushed test database. Production
    # installs the singleton with the schema migration before serving requests.
    row, _ = ComputeQueue.objects.select_for_update().get_or_create(pk=1)
    return row


def failure(code):
    code = code if code in FAILURES else "infrastructure_failure"
    return {"code": code, "message": FAILURES[code]}


def pulse(capacity_available=None):
    with transaction.atomic():
        queue = queue_lock()
        queue.heartbeat_at = timezone.now()
        if capacity_available is not None:
            queue.capacity_available = capacity_available
        queue.save()


def service_status():
    queue = ComputeQueue.objects.filter(pk=1).first()
    alive = bool(queue and queue.heartbeat_at and
                 queue.heartbeat_at > timezone.now() - timedelta(seconds=45))
    return {"enabled": enabled(), "worker_available": alive,
            "capacity_available": bool(alive and queue.capacity_available)}


def claim(worker_id):
    """Claim FIFO work only after expired containers have been reconciled."""
    with transaction.atomic():
        queue_lock()
        if (not enabled() or Job.objects.filter(state__in=RUNNING).exists()
                or Acquisition.objects.filter(state="validating", current_attempt__isnull=False).exists()):
            return None
        job = Job.objects.filter(state="queued").order_by("created_at", "id").first()
        if job is None:
            return None
        now = timezone.now()
        attempt = JobAttempt.objects.create(job=job, number=job.attempt_count + 1,
                                            worker_id=worker_id, heartbeat_at=now)
        job.state, job.phase = "running", "preparing"
        job.attempt_count, job.current_attempt = attempt.number, attempt.pk
        job.started_at = job.started_at or now
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        job.failure = None
        job.save()
        return job, attempt


def heartbeat(job_id, attempt_id, phase):
    """Return continue/cancel/lost. An expired lease can never be renewed."""
    with transaction.atomic():
        queue = queue_lock()
        now = timezone.now()
        queue.heartbeat_at = now
        queue.save(update_fields=["heartbeat_at"])
        job = Job.objects.filter(pk=job_id).first()
        if (not job or job.state not in RUNNING or job.current_attempt != attempt_id
                or not job.lease_until or job.lease_until <= now):
            return "lost"
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        if phase in ("preparing", "estimating", "evaluating"):
            job.phase = phase
            job.state = "evaluating" if phase == "evaluating" else "running"
        job.save(update_fields=["lease_until", "phase", "state", "updated_at"])
        JobAttempt.objects.filter(pk=attempt_id, job=job).update(heartbeat_at=now, state=job.state)
        return "cancel" if job.cancel_requested_at else "continue"


def cancel_locked(job):
    """Caller holds the queue lock; repeats and terminal cancellation are no-ops."""
    if job.state in TERMINAL or job.cancel_requested_at:
        return
    now = timezone.now()
    job.cancel_requested_at = now
    if job.state == "queued":
        job.state, job.phase, job.finished_at = "cancelled", "complete", now
        job.failure = failure("cancelled")
    job.save()


def finish(job_id, attempt_id, outcome, *, artifacts_available=False):
    """Only the live current attempt may atomically publish a complete outcome."""
    with transaction.atomic():
        queue_lock()
        now = timezone.now()
        job = Job.objects.filter(pk=job_id).first()
        if (not job or job.state not in RUNNING or job.current_attempt != attempt_id
                or not job.lease_until or job.lease_until <= now):
            return False
        state = outcome["state"]
        if state not in TERMINAL:
            raise ValueError("An execution outcome must be terminal")
        code = (outcome.get("failure") or {}).get("code", "infrastructure_failure")
        cancelled_success = bool(job.cancel_requested_at and state == "succeeded")
        if job.cancel_requested_at:
            state, code = "cancelled", "cancelled"
        if state == "succeeded" and (not outcome.get("result") or not outcome.get("manifest")):
            raise ValueError("Successful publication requires validated results and a manifest")
        detail = None if state == "succeeded" else failure(code)
        JobAttempt.objects.filter(pk=attempt_id, job=job).update(state=state, finished_at=now, failure=detail)
        retry = state == "failed" and code == "infrastructure_failure" and job.attempt_count < MAX_ATTEMPTS
        job.state = "queued" if retry else state
        job.phase = "queued" if retry else "complete"
        job.finished_at = None if retry else now
        job.lease_until = None
        job.failure = detail
        if state == "succeeded":
            job.result = outcome["result"]
        if outcome.get("manifest") and artifacts_available and not cancelled_success:
            job.artifact_manifest = outcome["manifest"]
            job.published_attempt = attempt_id
            job.artifacts_available = artifacts_available
            job.artifacts_expire_at = now + timedelta(days=RETENTION_DAYS)
        job.save()
        return True


def expired_attempts():
    return list(Job.objects.filter(state__in=RUNNING, lease_until__lte=timezone.now())
                .values_list("id", "current_attempt"))


def recover(job_id, attempt_id):
    """Called only after all deterministic child containers were stopped.

    Expired work continues to occupy the global slot until cleanup succeeds.
    Its former worker cannot heartbeat or publish, even before this transition.
    """
    with transaction.atomic():
        queue_lock()
        now = timezone.now()
        job = Job.objects.filter(pk=job_id).first()
        if (not job or job.state not in RUNNING or job.current_attempt != attempt_id
                or not job.lease_until or job.lease_until > now):
            return False
        detail = failure("cancelled" if job.cancel_requested_at else "infrastructure_failure")
        JobAttempt.objects.filter(pk=attempt_id, job=job).update(state="expired", finished_at=now, failure=detail)
        job.state = ("cancelled" if job.cancel_requested_at else
                     "queued" if job.attempt_count < MAX_ATTEMPTS else "failed")
        job.phase = "queued" if job.state == "queued" else "complete"
        job.finished_at = None if job.state == "queued" else now
        job.failure, job.lease_until = detail, None
        job.save()
        return True
