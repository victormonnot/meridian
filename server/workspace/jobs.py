"""Owner-scoped job metadata; the HTTP process never executes an estimator."""
from datetime import timedelta
import math
import uuid

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from meridian.vio_specification import specification_sha256, validate_specification

from .api import endpoint, error, not_found, response
from .experiments import owned_project, payload
from .job_queue import ACTIVE, TERMINAL, cancel_locked, enabled, limits, queue_lock, service_status
from .investigation import available as investigation_available
from .models import AuthWindow, Experiment, Job


def _date(value):
    return value.isoformat() if value else None


def job_payload(job):
    return {"id": str(job.pk), "project_id": str(job.project_id),
            "experiment_id": str(job.experiment_id), "experiment_name": job.experiment_name,
            "revision_version": job.revision_version, "specification_sha256": job.specification_sha256,
            "parameters": job.specification["parameters"], "state": job.state, "phase": job.phase,
            "created_at": _date(job.created_at), "updated_at": _date(job.updated_at),
            "started_at": _date(job.started_at), "finished_at": _date(job.finished_at),
            "attempt_count": job.attempt_count, "max_attempts": limits()["max_attempts"],
            "cancel_requested": bool(job.cancel_requested_at), "failure": job.failure,
            "result": job.result if job.state == "succeeded" else None,
            "artifacts_available": job.artifacts_available,
            "investigation_available": investigation_available(job),
            "artifacts_expire_at": _date(job.artifacts_expire_at),
            "attempts": [{"number": item.number, "state": item.state,
                          "started_at": _date(item.started_at), "finished_at": _date(item.finished_at),
                          "failure": item.failure} for item in job.attempts.all()]}


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("Expected UUID text")
    parsed = uuid.UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("Expected a canonical UUID")
    return parsed


def _submission_budget(owner_id):
    """Fixed 24h submission windows survive deletion of jobs and experiments."""
    now = timezone.now()
    windows = []
    for identity, maximum in ((str(owner_id), 10), ("global", 100)):
        key = salted_hmac("meridian.compute-window", identity, algorithm="sha256").hexdigest()
        window, _ = AuthWindow.objects.get_or_create(key=key, defaults={"started_at": now})
        if now >= window.started_at + timedelta(hours=24):
            window.started_at, window.count = now, 0
        if window.count >= maximum:
            return max(1, math.ceil((window.started_at + timedelta(hours=24) - now).total_seconds()))
        windows.append(window)
    for window in windows:
        window.count += 1
        window.save(update_fields=["started_at", "count"])
    return 0


@endpoint(["GET", "POST"])
def jobs(request, project_id):
    with transaction.atomic():
        if request.method == "POST":
            queue_lock()
        project = owned_project(request, project_id, lock=request.method == "POST")
        if project is None:
            return not_found(request)
        if request.method == "GET":
            if set(request.GET) - {"experiment_id", "revision_version"}:
                return error("invalid_request", "Unsupported job filter.", 400)
            query = project.jobs.prefetch_related("attempts")
            try:
                for key in request.GET:
                    if len(request.GET.getlist(key)) != 1:
                        raise ValueError("Repeated filter")
                if "experiment_id" in request.GET:
                    query = query.filter(experiment_id=_uuid(request.GET["experiment_id"]))
                if "revision_version" in request.GET:
                    value = request.GET["revision_version"]
                    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 50:
                        raise ValueError("Invalid revision")
                    query = query.filter(revision_version=int(value))
            except (ValueError, OverflowError):
                return error("invalid_request", "Select a valid experiment and revision.", 400)
            return response({"jobs": [job_payload(item) for item in query],
                             "limits": limits(), "service": service_status()})
        fields = {"schema_version", "experiment_id", "revision_version", "idempotency_key"}
        data, invalid = payload(request, fields, fields)
        if invalid is not None:
            return invalid
        try:
            experiment_id, key = _uuid(data["experiment_id"]), _uuid(data["idempotency_key"])
            version = data["revision_version"]
            if type(version) is not int or not 1 <= version <= 50:
                raise ValueError("Invalid revision")
        except (ValueError, AttributeError):
            return error("invalid_request", "Select a saved revision and provide a UUID idempotency key.", 400)
        previous = Job.objects.filter(owner=request.user, idempotency_key=key).first()
        if previous:
            if (previous.project_id, previous.experiment_id, previous.revision_version) != (project.pk, experiment_id, version):
                return error("idempotency_conflict", "This submission key already identifies a different request.", 409)
            return response({"job": job_payload(previous), "reused": True})
        if not enabled():
            return error("execution_unavailable", "Execution is not enabled on this deployment.", 503)
        experiment = Experiment.objects.filter(pk=experiment_id, project=project).first()
        revision = experiment.revisions.filter(version=version).first() if experiment else None
        if revision is None:
            return not_found(request)
        try:
            validate_specification(revision.specification)
            digest = specification_sha256(revision.specification)
            if digest != revision.specification_sha256:
                raise ValueError("Snapshot identity mismatch")
        except ValueError:
            return error("invalid_specification", "This saved revision is not supported by the execution profile.", 409)
        if (Job.objects.filter(owner=request.user).count() >= limits()["retained_per_account"]
                or Job.objects.count() >= limits()["retained_global"]):
            return error("job_limit", "The retained job limit has been reached. Delete completed jobs before submitting more.", 409)
        if (Job.objects.filter(owner=request.user, state__in=ACTIVE).count() >= limits()["active_per_account"]
                or Job.objects.filter(state__in=ACTIVE).count() >= limits()["active_global"]):
            return error("queue_full", "The active job limit has been reached. Wait for completion or cancel queued work.", 409)
        retry_after = _submission_budget(request.user.pk)
        if retry_after:
            result = error("submission_limit", "The execution submission budget has been reached. Try again later.", 429)
            result["Retry-After"] = str(retry_after)
            return result
        job = Job.objects.create(owner=request.user, project=project, experiment=experiment,
                                 experiment_name=revision.name, revision_version=version,
                                 specification=revision.specification, specification_sha256=digest,
                                 idempotency_key=key)
        return response({"job": job_payload(job), "reused": False}, 201)


@endpoint(["GET", "DELETE"])
def job(request, project_id, job_id):
    with transaction.atomic():
        if request.method == "DELETE":
            queue_lock()
        project = owned_project(request, project_id, lock=request.method == "DELETE")
        item = project.jobs.filter(pk=job_id).prefetch_related("attempts").first() if project else None
        if item is None:
            return not_found(request)
        if request.method == "GET":
            return response({"job": job_payload(item)})
        if item.state not in TERMINAL:
            return error("active_job", "Cancel this execution and wait for it to stop before deleting it.", 409)
        item.delete()
        return response({"deleted": True})


@endpoint(["POST"])
def cancel(request, project_id, job_id):
    with transaction.atomic():
        queue_lock()
        project = owned_project(request, project_id, lock=True)
        item = project.jobs.filter(pk=job_id).first() if project else None
        if item is None:
            return not_found(request)
        _, invalid = payload(request, {"schema_version"}, {"schema_version"})
        if invalid is not None:
            return invalid
        cancel_locked(item)
        return response({"job": job_payload(item)})
