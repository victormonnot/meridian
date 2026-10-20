"""Bounded, owner-scoped downloads of independently reproducible executions."""
from hashlib import sha256
import os
from pathlib import Path
import stat

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from meridian.vio_export import (
    MAX_EXPORT_BYTES, MAX_FILE_BYTES, REQUIRED_ARTIFACTS, SOURCE_FILES, build_export,
)
from meridian.vio_specification import specification_sha256, validate_specification
from .api import endpoint, error, not_found
from .investigation import ArtifactUnavailable, _identity, _private, _read
from .models import Job


def unavailable_reason(job):
    """Metadata hint only; each download verifies the retained files again."""
    if job.state != "succeeded":
        return "Independent reproduction requires a successfully completed execution."
    if job.artifacts_expire_at and job.artifacts_expire_at <= timezone.now():
        return "The retained files have expired. Saved job metrics remain available."
    manifest = job.artifact_manifest or {}
    if (not job.artifacts_available or not job.published_attempt or not job.artifacts_expire_at
            or type(manifest) is not dict or type(manifest.get("files")) is not dict
            or not set(REQUIRED_ARTIFACTS) <= set(manifest["files"])):
        return "This execution has no complete retained reproduction evidence."
    return None


def _source_files():
    """Read only the deployment's explicit source inventory, never a repository tree."""
    root = Path(settings.MERIDIAN_EXPORT_SOURCE_ROOT)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ArtifactUnavailable("Invalid reproduction source root")
    files, total = {}, 0
    for name in SOURCE_FILES:
        parts = Path(name).parts
        if not parts or Path(name).is_absolute() or any(part in {".", ".."} for part in parts):
            raise ArtifactUnavailable("Invalid reproduction source name")
        descriptors = []
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in parts[:-1]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=descriptors[-1]))
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptors[-1])
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or not 0 <= before.st_size <= MAX_FILE_BYTES):
                    raise ArtifactUnavailable("Invalid reproduction source file")
                total += before.st_size
                if total > MAX_EXPORT_BYTES:
                    raise ArtifactUnavailable("Reproduction source exceeds its budget")
                data = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
                if (len(data) != before.st_size
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ArtifactUnavailable("Reproduction source changed during reading")
                files[name] = data
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    return files


def _artifacts(job):
    manifest = job.artifact_manifest
    if (type(manifest) is not dict or set(manifest) != {"schema_version", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or type(manifest["files"]) is not dict):
        raise ArtifactUnavailable("Unsupported publication manifest")
    total = 0
    for name in REQUIRED_ARTIFACTS:
        total += _identity(manifest["files"].get(name), MAX_FILE_BYTES)["size_bytes"]
    if total > MAX_EXPORT_BYTES:
        raise ArtifactUnavailable("Reproduction evidence exceeds its budget")
    return {name: _read(settings.MERIDIAN_JOB_ARTIFACT_ROOT,
                        [str(job.pk), str(job.published_attempt), name],
                        manifest["files"][name], MAX_FILE_BYTES)
            for name in REQUIRED_ARTIFACTS}


def _retention_failure(job):
    if job.artifacts_expire_at and job.artifacts_expire_at <= timezone.now():
        return error("artifacts_expired", "The retained files have expired. Saved job metrics remain available.", 410)
    reason = unavailable_reason(job)
    return error("export_unavailable", reason, 409) if reason else None


@endpoint(["GET"])
def download(request, project_id, job_id):
    owned = Job.objects.filter(pk=job_id, project_id=project_id, owner=request.user,
                               project__owner=request.user)
    job = owned.first()
    if job is None:
        return not_found(request)
    if set(request.GET) != {"schema_version"} or len(request.GET.getlist("schema_version")) != 1:
        return error("invalid_request", "Provide exactly one export schema_version.", 400)
    if request.GET["schema_version"] != "1":
        return error("unsupported_schema", "Only export schema version 1 is supported.", 400)
    failure = _retention_failure(job)
    if failure is not None:
        return failure
    try:
        validate_specification(job.specification)
        if specification_sha256(job.specification) != job.specification_sha256:
            raise ArtifactUnavailable("Invalid saved specification identity")
        execution = {
            "job_id": str(job.pk), "experiment_id": str(job.experiment_id),
            "revision_version": job.revision_version, "state": job.state,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "specification_sha256": job.specification_sha256,
        }
        data = build_export(job.specification, artifacts=_artifacts(job),
                            publication_manifest=job.artifact_manifest,
                            source_files=_source_files(), execution=execution)
        if not data or len(data) > MAX_EXPORT_BYTES:
            raise ArtifactUnavailable("Export exceeds its budget")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return error("export_integrity_failed", "The reproduction evidence or sources are unavailable or failed their integrity check.", 503)
    # A deletion/expiry while assembling the bounded archive revokes a response
    # not yet sent. Bytes already downloaded cannot be recalled by the service.
    current = owned.first()
    if current is None:
        return not_found(request)
    failure = _retention_failure(current)
    if failure is not None:
        return failure
    if current.published_attempt != job.published_attempt or current.artifact_manifest != job.artifact_manifest:
        return error("export_integrity_failed", "The retained execution changed during export.", 503)
    result = HttpResponse(data, content_type="application/zip")
    result["Content-Disposition"] = f'attachment; filename="meridian-run-{job.pk}-v1.zip"'
    result["Content-Length"] = str(len(data))
    result["X-Content-SHA256"] = sha256(data).hexdigest()
    return _private(result)
