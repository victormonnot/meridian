"""Authenticated reads of published investigation evidence and pinned camera frames.

The HTTP service has read-only mounts. It neither runs a numerical evaluator nor
accepts filesystem paths from the client. Every read is bound to a live owner's
job and the manifest of its published attempt, including image requests.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from meridian import vio_investigation_metrics
from meridian.vio_investigation_metrics import summarize_interval
from .execution_profiles import is_private, specification_sha256, validate_specification
from .api import endpoint, error, not_found, response
from .models import Job

MAX_INVESTIGATION_BYTES = 32 * 1024 * 1024
MAX_IMAGE_INDEX_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024


class ArtifactUnavailable(ValueError):
    pass


def available(job):
    """Metadata capability; actual file availability is verified on each read."""
    return bool(job.state in {"succeeded", "failed"} and job.artifacts_available
                and job.published_attempt and job.artifacts_expire_at
                and job.artifacts_expire_at > timezone.now()
                and "investigation.json" in (job.artifact_manifest or {}).get("files", {}))


def _identity(value, limit):
    if (not isinstance(value, dict) or set(value) != {"sha256", "size_bytes"}
            or type(value["size_bytes"]) is not int or not 0 < value["size_bytes"] <= limit
            or not isinstance(value["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None):
        raise ArtifactUnavailable("Invalid file identity")
    return value


def _read(root, parts, identity, limit, *, package_paths=False):
    """Open every relative component without following links; verify exact bytes."""
    identity = _identity(identity, limit)
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ArtifactUnavailable("Invalid read-only root")
    if package_paths:
        from .acquisition_storage import safe_name
        safe_name("/".join(parts), maximum=512)
    elif not parts or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", part) is None
                          or part in {".", ".."} for part in parts):
        raise ArtifactUnavailable("Invalid internal file name")
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
                    or before.st_size != identity["size_bytes"]):
                raise ArtifactUnavailable("File is not the published regular file")
            data = stream.read(identity["size_bytes"] + 1)
            after = os.fstat(stream.fileno())
            if (len(data) != identity["size_bytes"] or sha256(data).hexdigest() != identity["sha256"]
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ArtifactUnavailable("Published file changed")
        return data
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactUnavailable("Duplicate JSON key")
        value[key] = item
    return value


def _nonfinite(value):
    raise ArtifactUnavailable("Nonfinite JSON value")


def _artifact(job, name, limit):
    manifest = job.artifact_manifest
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or not isinstance(manifest.get("files"), dict)):
        raise ArtifactUnavailable("Invalid published manifest")
    data = _read(settings.MERIDIAN_JOB_ARTIFACT_ROOT,
                 [str(job.pk), str(job.published_attempt), name], manifest["files"].get(name), limit)
    value = json.loads(data, object_pairs_hook=_unique, parse_constant=_nonfinite)
    validate_specification(job.specification)
    if is_private(job.specification):
        expected_input = job.specification["input"]
        if name == "investigation.json":
            expected_input = expected_input | {"profile_id": job.specification["profile_id"],
                "clock_id": job.specification["clock_id"], "imu_frame_id": job.specification["imu"]["frame_id"],
                "processing": job.specification["processing"]}
        if (type(value) is not dict or type(value.get("schema_version")) is not int
                or value["schema_version"] != 2
                or value.get("specification_sha256") != job.specification_sha256
                or specification_sha256(job.specification) != job.specification_sha256
                or value.get("input") != expected_input):
            raise ArtifactUnavailable("Private evidence does not belong to this input specification.")
        if name == "investigation.json" and (value.get("run_state") not in {"complete", "partial"}
                or (job.state == "succeeded") != (value["run_state"] == "complete")):
            raise ArtifactUnavailable("Private evidence completion state disagrees with the job.")
        return value
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("specification_sha256") != job.specification_sha256
            or specification_sha256(job.specification) != job.specification_sha256
            or value.get("dataset_manifest_sha256") != job.specification["dataset"]["prepared_manifest_sha256"]):
        raise ArtifactUnavailable("Evidence does not belong to this specification")
    if name == "investigation.json":
        if (value.get("run_state") not in {"complete", "partial"}
                or (job.state == "succeeded") != (value["run_state"] == "complete")):
            raise ArtifactUnavailable("Evidence completion state disagrees with the job")
    return value


def _owned(request, project_id, job_id):
    # Check both ownership relations before consulting files or disclosing status.
    job = Job.objects.filter(pk=job_id, project_id=project_id, owner=request.user,
                             project__owner=request.user).first()
    if job is None:
        return None, not_found(request)
    if job.artifacts_expire_at and job.artifacts_expire_at <= timezone.now():
        return None, error("artifacts_expired", "The retained files have expired. Saved job metrics remain available.", 410)
    if not available(job):
        return None, error("investigation_unavailable", "This execution has no retained investigation evidence.", 409)
    return job, None


def _unavailable():
    return error("artifact_unavailable", "The published evidence is unavailable or failed its integrity check.", 503)


def _private(result):
    result["Cache-Control"] = "no-store"
    patch_vary_headers(result, ["Cookie"])
    return result


@endpoint(["GET"])
def investigation(request, project_id, job_id):
    job, failure = _owned(request, project_id, job_id)
    if failure is not None:
        return failure
    if request.GET:
        return error("invalid_request", "Investigation reads do not accept query parameters.", 400)
    try:
        value = _artifact(job, "investigation.json", MAX_INVESTIGATION_BYTES)
        return _private(response({"investigation": value}))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _unavailable()


@endpoint(["GET"])
def metrics(request, project_id, job_id):
    job, failure = _owned(request, project_id, job_id)
    if failure is not None:
        return failure
    try:
        if set(request.GET) != {"start_ns", "end_ns"} or any(len(request.GET.getlist(key)) != 1 for key in request.GET):
            raise ValueError("Expected two unique interval bounds")
        start, end = request.GET["start_ns"], request.GET["end_ns"]
        if any(re.fullmatch(r"0|[1-9][0-9]{0,18}", item) is None for item in (start, end)):
            raise ValueError("Expected decimal integer nanoseconds")
        keys = ("first_ns", "last_ns") if is_private(job.specification) else ("first_camera_timestamp_ns", "last_camera_timestamp_ns")
        first, last = (int(job.specification["interval"][key]) for key in keys)
        if not first <= int(start) <= int(end) <= last:
            raise ValueError("Interval outside the saved recording")
    except (ValueError, TypeError, KeyError):
        return error("invalid_request", "Select ordered integer nanosecond bounds within the recording.", 400)
    try:
        value = _artifact(job, "investigation.json", MAX_INVESTIGATION_BYTES)
        scorer_hash = sha256(Path(vio_investigation_metrics.__file__).read_bytes()).hexdigest()
        if value["provenance"]["interval_scorer_sha256"] != scorer_hash:
            raise ArtifactUnavailable("Interval scorer differs from the recorded implementation")
        if is_private(job.specification):
            from meridian.vio_private_specification import summarize_private_interval
            if not int(value["time"]["first_timestamp_ns"]) <= int(start) <= int(end) <= int(value["time"]["last_timestamp_ns"]):
                return error("invalid_request", "Select bounds within the retained camera schedule.", 400)
            return _private(response({"interval": summarize_private_interval(value, start, end)}))
        return _private(response({"interval": summarize_interval(value, start, end)}))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _unavailable()


@endpoint(["GET"])
def image(request, project_id, job_id, camera, frame_index):
    job, failure = _owned(request, project_id, job_id)
    if failure is not None:
        return failure
    if request.GET:
        return error("invalid_request", "Camera reads do not accept query parameters.", 400)
    if is_private(job.specification):
        return _private_image(request, job, camera, frame_index)
    if camera not in {"cam0", "cam1"} or not 0 <= frame_index < job.specification["interval"]["camera_pairs"]:
        return not_found(request)
    try:
        index = _artifact(job, "image-index.json", MAX_IMAGE_INDEX_BYTES)
        entries = index["cameras"][camera]
        if not isinstance(entries, list) or len(entries) != job.specification["interval"]["camera_pairs"]:
            raise ArtifactUnavailable("Camera index does not cover the recording")
        item = entries[frame_index]
        stamp = item["timestamp_ns"]
        if (type(item["index"]) is not int or item["index"] != frame_index
                or not isinstance(stamp, str) or re.fullmatch(r"[0-9]{1,19}", stamp) is None
                or item["relative_path"] != f"sensors/mav0/{camera}/data/{stamp}.png"):
            raise ArtifactUnavailable("Invalid camera identity")
        identity = {key: item[key] for key in ("sha256", "size_bytes")}
        data = _read(settings.MERIDIAN_CAMERA_ROOT, [camera, f"{stamp}.png"], identity, MAX_IMAGE_BYTES)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ArtifactUnavailable("Camera frame is not a PNG")
        result = HttpResponse(data, content_type="image/png")
        result["Content-Length"] = str(len(data))
        result["Content-Disposition"] = f'inline; filename="{camera}-{stamp}.png"'
        result["X-Content-SHA256"] = identity["sha256"]
        result["X-Timestamp-Ns"] = stamp
        return _private(result)
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        return _unavailable()


def _private_image(request, job, camera, frame_index):
    from . import acquisition_storage
    from .models import InputRevision
    if camera not in {"cam0", "cam1"} or type(frame_index) is not int or frame_index < 0:
        return not_found(request)
    source = InputRevision.objects.filter(pk=job.input_revision_id, owner_id=job.owner_id,
        project_id=job.project_id, project__owner_id=job.owner_id).first()
    frozen = job.specification["input"]
    if (source is None or not source.files_available or str(source.pk) != frozen["input_revision_id"]
            or str(source.published_attempt) != frozen["published_attempt"]):
        return error("input_files_unavailable", "Input images are unavailable; retained execution evidence remains accessible.", 410)
    try:
        index = _artifact(job, "image-index.json", MAX_IMAGE_INDEX_BYTES)
        entries = index["cameras"][camera]
        if type(entries) is not list or len(entries) > 10_000:
            raise ArtifactUnavailable("Invalid private camera index.")
        if frame_index >= len(entries):
            return not_found(request)
        item = entries[frame_index]
        name = acquisition_storage.safe_name(item["relative_path"])
        stamp = item["timestamp_ns"]
        identity = {key: item[key] for key in ("sha256", "size_bytes")}
        if (index.get("camera_metadata") != job.specification["cameras"]
                or type(item.get("index")) is not int or item["index"] != frame_index
                or type(stamp) is not str or re.fullmatch(r"0|[1-9][0-9]{0,18}", stamp) is None
                or int(stamp) > 2**63 - 1 or not name.startswith("sensors/")
                or (source.manifest or {}).get("files", {}).get(name) != identity):
            raise ArtifactUnavailable("Camera evidence differs from its frozen prepared inventory.")
        data = _read(acquisition_storage.root(), ["revisions", str(source.pk), "published",
                     str(source.published_attempt), *name.split("/")], identity, MAX_IMAGE_BYTES, package_paths=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ArtifactUnavailable("Private camera frame is not PNG.")
        if not InputRevision.objects.filter(pk=source.pk, files_available=True,
                                            published_attempt=source.published_attempt).exists():
            return error("input_files_unavailable", "The prepared input images were deleted.", 410)
        result = HttpResponse(data, content_type="image/png")
        result["Content-Length"] = str(len(data))
        result["Content-Disposition"] = f'inline; filename="{camera}-{frame_index}.png"'
        result["X-Content-SHA256"] = identity["sha256"]
        result["X-Timestamp-Ns"] = stamp
        return _private(result)
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        return _unavailable()
