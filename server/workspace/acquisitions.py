"""Owner-scoped resumable upload API. Decoding never occurs in HTTP requests."""
from datetime import timedelta
from hashlib import sha256
import os
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone

from . import acquisition_queue as queue, acquisition_storage as storage
from .api import endpoint, error, not_found, response
from .experiments import owned_project, payload
from .job_queue import queue_lock
from .models import Acquisition, UploadChunk


def _date(value):
    return value.isoformat() if value else None


def acquisition_payload(value):
    available = value.files_available and value.state == "ready"
    availability = "available" if available else "deleted" if value.deleted_at else "unavailable"
    if available:
        try:
            with storage.open_relative(storage.root(), f"{value.pk}/original") as source:
                intact = os.fstat(source.fileno()).st_size == value.size_bytes
            with storage.open_relative(storage.root(), f"{value.pk}/published/{value.published_attempt}/publication-manifest.json"):
                pass
            if not intact:
                raise ValueError("Original size changed")
        except (OSError, ValueError):
            available, availability = False, "missing"
    return {"id": str(value.pk), "project_id": str(value.project_id), "name": value.name,
            "filename": value.filename, "size_bytes": value.size_bytes, "bytes_received": value.bytes_received,
            "sha256": value.sha256 or None, "state": value.state, "phase": value.phase,
            "chunk_bytes": value.budget["chunk_bytes"], "options": value.options,
            "created_at": _date(value.created_at), "updated_at": _date(value.updated_at),
            "expires_at": _date(value.expires_at) if value.state == "receiving" else None,
            "finished_at": _date(value.finished_at), "cancel_requested": bool(value.cancel_requested_at),
            "failure": value.failure, "result": value.result if value.state == "ready" else None,
            "files_available": available, "availability": availability,
            "reserved_bytes": value.reserved_bytes, "retained_bytes": value.retained_bytes,
            "attempt_count": value.attempt_count, "execution_readiness": "not_assessed"}


def _owned(request, project_id, acquisition_id):
    return Acquisition.objects.filter(pk=acquisition_id, project_id=project_id, owner=request.user,
                                      project__owner=request.user).first()


def _enabled():
    return None if settings.MERIDIAN_UPLOADS_ENABLED else error("uploads_disabled", "Private recording uploads are unavailable.", 503)


def _metadata(request):
    fields = {"schema_version", "name", "filename", "size_bytes", "idempotency_key", "imu_stream_id", "include_reference"}
    data, problem = payload(request, fields, fields - {"imu_stream_id", "include_reference"})
    if problem is not None:
        return None, problem
    try:
        if (type(data["filename"]) is not str or not 1 <= len(data["filename"]) <= 200
                or any(ord(c) < 32 or ord(c) == 127 for c in data["filename"])
                or any(c in data["filename"] for c in "/\\:")
                or not data["filename"].lower().endswith((".tar", ".tar.gz", ".tgz", ".zip"))):
            raise ValueError("Select a Meridian .tar, .tar.gz or .zip package.")
        if type(data["size_bytes"]) is not int or not 0 < data["size_bytes"] <= queue.limits()["upload_bytes"]:
            raise ValueError("Archive exceeds the allowed upload size.")
        key = uuid.UUID(data["idempotency_key"])
        if str(key) != data["idempotency_key"]:
            raise ValueError("Use a canonical upload UUID.")
        stream = data.get("imu_stream_id")
        if stream is not None and (not isinstance(stream, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", stream)):
            raise ValueError("Select a valid IMU stream identifier.")
        if type(data.get("include_reference", True)) is not bool:
            raise ValueError("Reference selection must be an explicit boolean.")
        data["filename"].encode("utf-8")
        data["options"] = {"imu_stream_id": stream, "include_reference": data.get("include_reference", True)}
        data["idempotency_key"] = key
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return None, error("invalid_request", "Use a supported archive, positive bounded size, valid UUID and preparation options.", 400)
    return data, None


@endpoint(["GET", "POST"])
def acquisitions(request, project_id):
    with transaction.atomic():
        if request.method == "POST":
            queue_lock()
        project = owned_project(request, project_id, lock=request.method == "POST")
        if project is None:
            return not_found(request)
        if request.method == "GET":
            return response({"acquisitions": [acquisition_payload(value) for value in project.acquisitions.all()],
                             "limits": queue.limits(), "storage": queue.usage(request.user), "service": queue.service_status()})
        problem = _enabled()
        if problem is not None:
            return problem
        data, problem = _metadata(request)
        if problem is not None:
            return problem
        identity = {"project_id": str(project.pk), "name": data["name"], "filename": data["filename"],
                    "size_bytes": data["size_bytes"], "options": data["options"]}
        digest = sha256(storage.canonical(identity)).hexdigest()
        previous = Acquisition.objects.filter(owner=request.user, idempotency_key=data["idempotency_key"]).first()
        if previous:
            if previous.request_sha256 != digest:
                return error("idempotency_conflict", "This upload key already identifies different content.", 409)
            return response({"acquisition": acquisition_payload(previous)})
        bounds, owner, global_usage = queue.limits(), queue.usage(request.user), queue.usage()
        reservation = data["size_bytes"] + bounds["unpacked_bytes"] + bounds["prepared_bytes"]
        files = bounds["reserved_files_per_import"]
        if (project.acquisitions.count() >= bounds["per_project"]
                or Acquisition.objects.filter(owner=request.user).count() >= bounds["per_account"]
                or Acquisition.objects.count() >= bounds["global_count"]):
            return error("acquisition_limit", "The recording count limit has been reached.", 409)
        if (owner["reserved_bytes"] + reservation > bounds["account_bytes"]
                or global_usage["reserved_bytes"] + reservation > bounds["global_bytes"]
                or owner["reserved_files"] + files > bounds["account_files"]
                or global_usage["reserved_files"] + files > bounds["global_files"]):
            return error("storage_quota", "There is insufficient recording storage quota for this upload and its preparation.", 409)
        try:
            storage.require_free_bytes(data["size_bytes"])
        except (OSError, ValueError):
            return error("storage_capacity", "The host currently has insufficient free recording storage. Try again later.", 409)
        value = Acquisition.objects.create(owner=request.user, project=project, name=data["name"], filename=data["filename"],
            size_bytes=data["size_bytes"], idempotency_key=data["idempotency_key"], request_sha256=digest,
            options=data["options"], budget={key: bounds[key] for key in ("unpacked_bytes", "prepared_bytes", "members", "rows", "image_pixels", "chunk_bytes")},
            reserved_bytes=reservation, reserved_files=files, expires_at=timezone.now() + timedelta(hours=queue.UPLOAD_HOURS))
        try:
            storage.provision(value.pk)
        except (OSError, ValueError):
            value.state, value.phase, value.finished_at = "cancelled", "complete", timezone.now()
            value.failure = queue.failure("infrastructure_failure")
            try:
                queue.release_files_locked(value)
            except (OSError, ValueError):
                value.save()
            else:
                value.delete()
            return error("storage_unavailable", "Recording storage is temporarily unavailable.", 503)
        return response({"acquisition": acquisition_payload(value)}, 201)


@endpoint(["GET", "DELETE"])
def acquisition(request, project_id, acquisition_id):
    with transaction.atomic():
        if request.method == "DELETE":
            queue_lock()
        value = _owned(request, project_id, acquisition_id)
        if value is None:
            return not_found(request)
        if request.method == "DELETE":
            if value.state in {"receiving", "validating"}:
                return error("active_import", "Cancel the transfer or import and wait for it to stop before deleting its files.", 409)
            if queue.compute_active():
                return error("cleanup_busy", "Wait for active executions and recording imports to stop before deleting files.", 409)
            try:
                queue.release_files_locked(value)
            except (OSError, ValueError):
                return error("storage_unavailable", "Recording cleanup has not completed; its quota remains reserved.", 503)
            if value.state == "ready":
                # Immutable metadata remains an explicit unavailable version for future dependencies.
                value.save()
            else:
                value.delete()
            return response({"deleted": True})
        return response({"acquisition": acquisition_payload(value)})


@endpoint(["GET"])
def chunks(request, project_id, acquisition_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    return response({"acquisition": acquisition_payload(value), "chunks": list(value.chunks.values("offset", "size_bytes", "sha256"))})


@endpoint(["PUT"])
def content(request, project_id, acquisition_id):
    # Authentication/CSRF run before reading binary content. The ordinary JSON
    # request.body limit remains 8 KiB; this route streams an explicitly bounded body.
    with transaction.atomic():
        queue_lock()
        value = _owned(request, project_id, acquisition_id)
        if value is None:
            return not_found(request)
        problem = _enabled()
        if problem is not None:
            return problem
        if value.state != "receiving" or value.expires_at <= timezone.now():
            return error("upload_not_receiving", "This acquisition no longer accepts upload chunks.", 409)
        try:
            offset_text, digest = request.headers.get("Upload-Offset", ""), request.headers.get("X-Chunk-SHA256", "")
            if not re.fullmatch(r"0|[1-9][0-9]{0,10}", offset_text) or not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError("Invalid chunk identity")
            offset = int(offset_text)
            length_text = request.META.get("CONTENT_LENGTH", "")
            if not re.fullmatch(r"[1-9][0-9]{0,8}", length_text):
                raise ValueError("Invalid content length")
            length = int(length_text)
            expected = min(value.budget["chunk_bytes"], value.size_bytes - offset)
            if (request.content_type != "application/octet-stream" or offset % value.budget["chunk_bytes"]
                    or not 0 < length == expected or offset > value.bytes_received):
                raise ValueError("Invalid chunk bounds")
            data = request.read(length + 1)
            if len(data) != length or sha256(data).hexdigest() != digest:
                raise ValueError("Chunk identity differs")
        except (ValueError, OSError):
            return error("invalid_chunk", "Send the next bounded chunk with its exact offset, length and SHA-256.", 400)
        if offset < value.bytes_received:
            previous = value.chunks.filter(offset=offset, size_bytes=length, sha256=digest).first()
            try:
                valid = previous and storage.verify_replayed_chunk(value, offset, length, digest)
            except (OSError, ValueError):
                valid = False
            if not valid:
                return error("chunk_conflict", "Previously acknowledged bytes differ; restart this upload.", 409)
            return response({"acquisition": acquisition_payload(value)})
        try:
            storage.append_chunk(value, offset, data)
        except (OSError, ValueError):
            return error("storage_unavailable", "The upload could not be stored safely; retry after refreshing its status.", 503)
        UploadChunk.objects.create(acquisition=value, offset=offset, size_bytes=length, sha256=digest)
        value.bytes_received += length
        value.expires_at = timezone.now() + timedelta(hours=queue.UPLOAD_HOURS)
        value.save(update_fields=["bytes_received", "expires_at", "updated_at"])
        return response({"acquisition": acquisition_payload(value)})


def _action_payload(request):
    return payload(request, {"schema_version"}, {"schema_version"})[1]


@endpoint(["POST"])
def complete(request, project_id, acquisition_id):
    with transaction.atomic():
        queue_lock()
        value = _owned(request, project_id, acquisition_id)
        if value is None:
            return not_found(request)
        problem = _action_payload(request)
        if problem is not None:
            return problem
        if value.state in {"validating", "ready"}:
            return response({"acquisition": acquisition_payload(value)})
        if (value.state != "receiving" or value.bytes_received != value.size_bytes or value.expires_at <= timezone.now()):
            return error("upload_incomplete", "Finish the complete transfer before requesting validation.", 409)
        value.state, value.phase = "validating", "queued"
        value.save(update_fields=["state", "phase", "updated_at"])
        return response({"acquisition": acquisition_payload(value)})


@endpoint(["POST"])
def cancel(request, project_id, acquisition_id):
    with transaction.atomic():
        queue_lock()
        value = _owned(request, project_id, acquisition_id)
        if value is None:
            return not_found(request)
        problem = _action_payload(request)
        if problem is not None:
            return problem
        if value.state not in {"receiving", "validating"}:
            return response({"acquisition": acquisition_payload(value)})
        value.cancel_requested_at = value.cancel_requested_at or timezone.now()
        if value.current_attempt is None:
            value.state, value.phase, value.finished_at = "cancelled", "complete", timezone.now()
            value.failure = queue.failure("cancelled")
            if queue.compute_active():
                value.phase = "cleanup_pending"
                value.save()
                return response({"acquisition": acquisition_payload(value)})
            try:
                queue.release_files_locked(value)
            except (OSError, ValueError):
                value.save()
                return error("storage_unavailable", "Cancellation was recorded; cleanup remains pending and quota is reserved.", 503)
        value.save()
        return response({"acquisition": acquisition_payload(value)})


def _download(request, project_id, acquisition_id, name=None):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    if value.state != "ready" or not value.files_available or not value.published_attempt:
        return error("inputs_unavailable", "This acquisition has no available published inputs.", 410)
    if name is None:
        identity = {"sha256": value.sha256, "size_bytes": value.size_bytes}
        relative, filename = f"{value.pk}/original", value.filename
    else:
        if name not in (value.manifest or {}).get("files", {}):
            return not_found(request)
        identity = value.manifest["files"][name]
        relative, filename = f"{value.pk}/published/{value.published_attempt}/{name}", name.rsplit("/", 1)[-1]
    stream = None
    try:
        stream = storage.open_relative(storage.root(), relative)
        if os.fstat(stream.fileno()).st_size != identity["size_bytes"]:
            raise ValueError("Published input size changed")
        # Deletion completed before response construction revokes this new read.
        if not Acquisition.objects.filter(pk=value.pk, files_available=True, published_attempt=value.published_attempt).exists():
            stream.close()
            return error("inputs_unavailable", "The recording files were deleted.", 410)
        result = FileResponse(stream, as_attachment=True, filename=filename, content_type="application/octet-stream")
        result["Content-Length"] = str(identity["size_bytes"])
        result["X-Content-SHA256"] = identity["sha256"]
        result["Cache-Control"] = "private, no-store"
        return result
    except (OSError, ValueError):
        if stream:
            stream.close()
        return error("inputs_unavailable", "The retained input file is missing or failed its storage check.", 503)


@endpoint(["GET"])
def original(request, project_id, acquisition_id):
    return _download(request, project_id, acquisition_id)


@endpoint(["GET"])
def descriptor(request, project_id, acquisition_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    if not value.descriptor:
        return error("inputs_unavailable", "The acquisition descriptor is not available yet.", 409)
    return response({"descriptor": value.descriptor})


@endpoint(["GET"])
def report(request, project_id, acquisition_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    return response({"report": value.result, "failure": value.failure, "acquisition": acquisition_payload(value)})


@endpoint(["GET"])
def files(request, project_id, acquisition_id):
    if _owned(request, project_id, acquisition_id) is None:
        return not_found(request)
    if set(request.GET) != {"path"} or len(request.GET.getlist("path")) != 1:
        return error("invalid_request", "Select exactly one published file path.", 400)
    try:
        name = storage.safe_name(request.GET["path"])
    except (ValueError, UnicodeError):
        return error("invalid_request", "Select a safe published relative file path.", 400)
    return _download(request, project_id, acquisition_id, name)
