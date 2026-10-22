"""Private immutable comparisons of two exact, terminal execution identities."""
from hashlib import sha256
import json
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from meridian import vio_investigation_metrics
from meridian.vio_comparison import ComparisonError, compare_runs
from meridian.vio_investigation_metrics import timestamp_ns

from .api import endpoint, error, not_found, response
from .experiments import owned_project, payload
from .investigation import ArtifactUnavailable, MAX_INVESTIGATION_BYTES, _artifact, available
from .job_queue import TERMINAL
from .jobs import _uuid
from .models import Job, RunComparison

MAX_COMPARISON_BYTES = 2 * 1024 * 1024


def limits():
    return {"comparisons": getattr(settings, "MERIDIAN_COMPARISON_LIMIT", 20)}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _access(item, job_id):
    job = Job.objects.filter(pk=job_id, project_id=item.project_id, owner_id=item.owner_id,
                             project__owner_id=item.owner_id).first()
    return {"exists": job is not None, "investigation_available": available(job) if job else False}


def comparison_payload(item, detail=False):
    value = {"id": str(item.pk), "project_id": str(item.project_id), "name": item.name,
             "description": item.description, "created_at": item.created_at.isoformat(),
             "baseline_job_id": str(item.baseline_job_id), "candidate_job_id": str(item.candidate_job_id),
             "analysis_sha256": item.analysis_sha256, "summary": item.summary}
    if detail:
        value.update(analysis=item.analysis, baseline_access=_access(item, item.baseline_job_id),
                     candidate_access=_access(item, item.candidate_job_id))
    return value


def _run(job):
    """Use only authorized immutable publication; corruption never becomes absence."""
    evidence, digest = None, None
    reason = ("expired" if job.artifacts_expire_at and job.artifacts_expire_at <= timezone.now()
              else "cancelled" if job.state == "cancelled" else "not_published")
    if available(job):
        evidence = _artifact(job, "investigation.json", MAX_INVESTIGATION_BYTES)
        scorer_hash = sha256(Path(vio_investigation_metrics.__file__).read_bytes()).hexdigest()
        if evidence["provenance"]["interval_scorer_sha256"] != scorer_hash:
            raise ArtifactUnavailable("The recorded interval scorer differs from this service")
        digest = job.artifact_manifest["files"]["investigation.json"]["sha256"]
        reason = None
    return {"id": str(job.pk), "state": job.state, "specification": job.specification,
            "specification_sha256": job.specification_sha256, "result": job.result,
            "failure": job.failure, "investigation": evidence, "investigation_sha256": digest,
            "investigation_unavailable_reason": reason, "experiment_id": str(job.experiment_id),
            "experiment_name": job.experiment_name, "revision_version": job.revision_version,
            "attempt_count": job.attempt_count, "published_attempt": str(job.published_attempt) if job.published_attempt else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


@endpoint(["GET", "POST"])
def comparisons(request, project_id):
    with transaction.atomic():
        project = owned_project(request, project_id, lock=request.method == "POST")
        if project is None:
            return not_found(request)
        if request.GET:
            return error("invalid_request", "Comparison requests do not accept query parameters.", 400)
        if request.method == "GET":
            items = project.comparisons.filter(owner=request.user).defer("analysis")
            return response({"comparisons": [comparison_payload(item) for item in items], "limits": limits()})
        required = {"schema_version", "name", "baseline_job_id", "candidate_job_id", "idempotency_key"}
        data, invalid = payload(request, required | {"description", "start_ns", "end_ns"}, required)
        if invalid is not None:
            return invalid
        try:
            baseline_id, candidate_id, key = (_uuid(data[field]) for field in
                                              ("baseline_job_id", "candidate_job_id", "idempotency_key"))
            if baseline_id == candidate_id:
                raise ValueError("Select two distinct executions")
            if ("start_ns" in data) != ("end_ns" in data):
                raise ValueError("Provide both interval bounds")
            if "start_ns" in data:
                if timestamp_ns(data["start_ns"]) > timestamp_ns(data["end_ns"]):
                    raise ValueError("Order the closed interval bounds")
        except (ValueError, TypeError, AttributeError):
            return error("invalid_request", "Select two distinct run UUIDs and ordered decimal nanosecond bounds.", 400)
        identity = {"project_id": str(project.pk), "name": data["name"], "description": data.get("description", ""),
                    "baseline_job_id": str(baseline_id), "candidate_job_id": str(candidate_id),
                    "start_ns": data.get("start_ns"), "end_ns": data.get("end_ns")}
        request_hash = sha256(_canonical(identity)).hexdigest()
        previous = project.comparisons.filter(owner=request.user, idempotency_key=key).first()
        if previous:
            if previous.request_sha256 != request_hash:
                return error("idempotency_conflict", "This creation key already identifies a different comparison.", 409)
            return response({"comparison": comparison_payload(previous, True), "reused": True})
        jobs = {item.pk: item for item in project.jobs.filter(pk__in=[baseline_id, candidate_id], owner=request.user)}
        if len(jobs) != 2:
            return not_found(request)
        from .execution_profiles import is_private
        if any(is_private(item.specification) for item in jobs.values()):
            return error("private_comparison_unavailable", "Controlled private-input comparisons are a separate increment.", 409)
        if any(item.state not in TERMINAL for item in jobs.values()):
            return error("active_runs", "Wait for both executions to finish before saving their comparison.", 409)
        if project.comparisons.count() >= limits()["comparisons"]:
            return error("comparison_limit", "This project has reached its saved comparison limit.", 409)
        try:
            baseline, candidate = (_run(jobs[identifier]) for identifier in (baseline_id, candidate_id))
            analysis = compare_runs(baseline, candidate, start_ns=data.get("start_ns"), end_ns=data.get("end_ns"))
            encoded = _canonical(analysis)
        except ComparisonError as exc:
            return error("incompatible_runs", "These runs cannot be compared under the same evaluation contract.", 409,
                         {"compatibility": [str(exc)]})
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return error("artifact_unavailable", "Comparison evidence is unavailable or failed its integrity check.", 503)
        if len(encoded) > MAX_COMPARISON_BYTES:
            return error("comparison_limit", "The derived comparison exceeds the retained-result size limit.", 409)
        summary = {"kind": analysis["kind"]}
        for role, job in (("baseline", jobs[baseline_id]), ("candidate", jobs[candidate_id])):
            summary[role] = {"parameters": job.specification["parameters"], "state": job.state,
                             "experiment_name": job.experiment_name, "revision_version": job.revision_version}
        item = RunComparison.objects.create(
            owner=request.user, project=project, name=data["name"], description=data.get("description", ""),
            baseline_job_id=baseline_id, candidate_job_id=candidate_id, idempotency_key=key,
            request_sha256=request_hash, summary=summary, analysis=analysis,
            analysis_sha256=sha256(encoded).hexdigest())
        return response({"comparison": comparison_payload(item, True), "reused": False}, 201)


@endpoint(["GET", "DELETE"])
def comparison(request, project_id, comparison_id):
    with transaction.atomic():
        project = owned_project(request, project_id, lock=request.method == "DELETE")
        item = project.comparisons.filter(pk=comparison_id, owner=request.user).first() if project else None
        if item is None:
            return not_found(request)
        if request.GET:
            return error("invalid_request", "Comparison reads do not accept query parameters.", 400)
        if request.method == "GET":
            return response({"comparison": comparison_payload(item, True)})
        item.delete()
        return response({"deleted": True})
