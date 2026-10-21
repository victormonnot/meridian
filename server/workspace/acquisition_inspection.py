"""Owner-scoped reads of immutable pre-estimation inspection evidence.

The API verifies bounded published bytes and queries their recorded observations.
Image decoding and preview generation belong to the isolated import child.
"""
from hashlib import sha256
import json
import os
import re

from django.http import HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers

from meridian.sensor_contract import contract_sha256
from meridian.sensor_conventions import timestamp_ns
from . import acquisition_storage as storage
from .api import endpoint, error, not_found, response
from .models import Acquisition


SUMMARY_PATH = "inspection/summary.json"
METHOD = "sensor-inspection-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_PREVIEW_BYTES = 128 * 1024
MAX_WINDOW_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}")
SAMPLE_ID = re.compile(r"0|[1-9][0-9]{0,18}")


class ArtifactUnavailable(ValueError):
    pass


def _private(result):
    result["Cache-Control"] = "no-store"
    patch_vary_headers(result, ["Cookie"])
    return result


def _owned(request, project_id, acquisition_id):
    return Acquisition.objects.filter(pk=acquisition_id, project_id=project_id,
                                      owner=request.user, project__owner=request.user).first()


def _availability(value):
    if value.state != "ready":
        return "not_ready", "The recording import has not published supported inputs."
    if not value.files_available or not value.published_attempt:
        return "deleted" if value.deleted_at else "unavailable", "The recording files are unavailable."
    if value.result is not None and type(value.result) is not dict:
        raise ArtifactUnavailable("Invalid import report")
    evidence = (value.result or {}).get("inspection")
    if evidence is None:
        return "not_generated", "This recording predates inspection generation. Import a new copy to inspect it."
    if evidence == {"status": "unavailable", "reason": "inspection_resource_limit"}:
        return "unavailable", "Inspection exceeded its resource budget; validated recording inputs remain available."
    if evidence != {"status": "available", "method_version": METHOD, "summary_path": SUMMARY_PATH}:
        raise ArtifactUnavailable("Unsupported inspection publication metadata")
    return "available", None


def _still_available(value):
    return Acquisition.objects.filter(pk=value.pk, owner_id=value.owner_id,
                                      project_id=value.project_id, project__owner_id=value.owner_id,
                                      state="ready", files_available=True,
                                      published_attempt=value.published_attempt).exists()


def _read(value, name, limit):
    storage.safe_name(name)
    if not name.startswith("inspection/"):
        raise ArtifactUnavailable("Inspection cannot read another publication branch")
    manifest = value.manifest
    if (type(manifest) is not dict or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or type(manifest.get("files")) is not dict):
        raise ArtifactUnavailable("Invalid publication manifest")
    identity = manifest["files"].get(name)
    if (type(identity) is not dict or set(identity) != {"size_bytes", "sha256"}
            or type(identity["size_bytes"]) is not int or not 0 < identity["size_bytes"] <= limit
            or type(identity["sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", identity["sha256"]) is None):
        raise ArtifactUnavailable("Inspection file lacks a bounded published identity")
    relative = f"{value.pk}/published/{value.published_attempt}/{name}"
    with storage.open_relative(storage.root(), relative) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != identity["size_bytes"]:
            raise ArtifactUnavailable("Inspection file size changed")
        data = stream.read(identity["size_bytes"] + 1)
        after = os.fstat(stream.fileno())
        if (len(data) != identity["size_bytes"] or sha256(data).hexdigest() != identity["sha256"]
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ArtifactUnavailable("Inspection file failed its identity check")
    return data, identity


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactUnavailable("Duplicate JSON key")
        value[key] = item
    return value


def _nonfinite(value):
    raise ArtifactUnavailable("Nonfinite JSON value")


def _json(value, name):
    data, _ = _read(value, name, MAX_JSON_BYTES)
    result = json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
    if type(result) is not dict:
        raise ArtifactUnavailable("Expected an inspection JSON object")
    return result


def _summary(value):
    from meridian.sensor_inspection import validate_summary
    result = _json(value, SUMMARY_PATH)
    if type(result.get("schema_version")) is not int:
        raise ArtifactUnavailable("Invalid inspection schema version")
    validate_summary(result)
    if result["acquisition"] != {"id": value.descriptor["id"], "sha256": contract_sha256(value.descriptor)}:
        raise ArtifactUnavailable("Inspection belongs to different acquisition bytes")
    return result


def _reader(value, summary):
    allowed = {chunk["path"] for stream in summary["streams"]
               for segment in stream["segments"] for chunk in segment["chunks"]}

    def read_chunk(name):
        if name not in allowed or not name.startswith("inspection/streams/") or not name.endswith(".json"):
            raise ArtifactUnavailable("Chunk does not belong to the inspection index")
        chunk = _json(value, name)
        if (type(chunk.get("schema_version")) is not int or chunk["schema_version"] != 1
                or type(chunk.get("rows")) is not list or not 1 <= len(chunk["rows"]) <= 256):
            raise ArtifactUnavailable("Chunk exceeds its observation bound")
        return chunk

    return read_chunk


def _unavailable():
    return _private(error("inspection_unavailable", "Published inspection evidence is missing or failed its integrity check.", 503))


def _deleted():
    return _private(error("inputs_unavailable", "The recording files were deleted or became unavailable.", 410))


def _require_available(value):
    availability, reason = _availability(value)
    if availability == "available":
        return None
    if availability == "deleted" or not value.files_available and value.state == "ready":
        return _deleted()
    return _private(error("inspection_unavailable", reason, 409))


@endpoint(["GET"])
def inspection(request, project_id, acquisition_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    if request.GET:
        return error("invalid_request", "Inspection summaries do not accept query parameters.", 400)
    try:
        availability, reason = _availability(value)
        if availability != "available":
            return _private(response({"inspection": None, "availability": availability, "reason": reason}))
        summary = _summary(value)
        if not _still_available(value):
            return _deleted()
        return _private(response({"inspection": summary, "availability": "available", "reason": None}))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return _unavailable()


@endpoint(["GET"])
def window(request, project_id, acquisition_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    try:
        if set(request.GET) != {"stream_id", "segment_id", "first_ns", "last_ns"} or any(
                len(request.GET.getlist(key)) != 1 for key in request.GET):
            raise ValueError("Expected unique window fields")
        selection = {key: request.GET[key] for key in request.GET}
        if any(IDENTIFIER.fullmatch(selection[key]) is None for key in ("stream_id", "segment_id")):
            raise ValueError("Invalid observation identity")
        first, last = timestamp_ns(selection["first_ns"]), timestamp_ns(selection["last_ns"])
        if first > last:
            raise ValueError("Reversed interval")
    except (ValueError, TypeError):
        return error("invalid_request", "Select one stream, one segment and ordered signed integer nanosecond bounds.", 400)
    try:
        failure = _require_available(value)
        if failure is not None:
            return failure
        summary = _summary(value)
        stream = next((item for item in summary["streams"] if item["id"] == selection["stream_id"]), None)
        segment = next((item for item in stream["segments"] if item["id"] == selection["segment_id"]), None) if stream else None
        if segment is None:
            return not_found(request)
        if not int(segment["first_ns"]) <= first <= last <= int(segment["last_ns"]):
            return error("invalid_request", "Select an interval within one declared segment.", 400)
        from meridian.sensor_inspection import InspectionLimitError, inspection_window
        try:
            result = inspection_window(summary, _reader(value, summary), **selection, max_points=1200)
        except InspectionLimitError:
            return _private(error("inspection_window_limit", "Select a narrower inspection interval.", 413))
        result = JsonResponse({"schema_version": 1, "window": result},
                              json_dumps_params={"separators": (",", ":"), "allow_nan": False})
        if len(result.content) > MAX_WINDOW_BYTES:
            return _private(error("inspection_window_limit", "Select a narrower inspection interval.", 413))
        if not _still_available(value):
            return _deleted()
        return _private(result)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, OverflowError):
        return _unavailable()


@endpoint(["GET"])
def image(request, project_id, acquisition_id, stream_id, sample_id):
    value = _owned(request, project_id, acquisition_id)
    if value is None:
        return not_found(request)
    if request.GET:
        return error("invalid_request", "Image previews do not accept query parameters.", 400)
    if IDENTIFIER.fullmatch(stream_id) is None or SAMPLE_ID.fullmatch(sample_id) is None:
        return not_found(request)
    try:
        failure = _require_available(value)
        if failure is not None:
            return failure
        summary = _summary(value)
        stream = next((item for item in summary["streams"] if item["id"] == stream_id and item["kind"] == "camera"), None)
        if stream is None or not any(int(chunk["first_sample_id"]) <= int(sample_id) <= int(chunk["last_sample_id"])
                                     for segment in stream["segments"] for chunk in segment["chunks"]):
            return not_found(request)
        from meridian.sensor_inspection import inspection_image
        item = inspection_image(summary, _reader(value, summary), stream_id=stream_id, sample_id=sample_id)
        if item is None:
            return not_found(request)
        if item["stream_id"] != stream_id or item["sample_id"] != sample_id:
            raise ArtifactUnavailable("Preview refers to a different observation")
        source = next((entry for entry in value.descriptor["files"] if entry["path"] == item["source_path"]), None)
        if (source is None or source["role"] != "sensor" or source["sha256"] != item["source_sha256"]
                or source["size_bytes"] != item["source_size_bytes"]
                or [item["width"], item["height"]] != stream["camera"]["resolution"]
                or any(type(item[key]) is not int or not 1 <= item[key] <= 320 for key in ("preview_width", "preview_height"))
                or not item["preview_path"].startswith("inspection/previews/") or not item["preview_path"].endswith(".png")):
            raise ArtifactUnavailable("Preview lacks exact source image provenance")
        data, identity = _read(value, item["preview_path"], MAX_PREVIEW_BYTES)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ArtifactUnavailable("Published preview is not PNG")
        if not _still_available(value):
            return _deleted()
        result = HttpResponse(data, content_type="image/png")
        result["Content-Length"] = str(len(data))
        result["Content-Disposition"] = f'inline; filename="{stream_id}-{sample_id}.png"'
        result["X-Content-SHA256"] = identity["sha256"]
        result["X-Source-SHA256"] = item["source_sha256"]
        result["X-Timestamp-Ns"] = item["timestamp_ns"]
        result["X-Sample-ID"] = sample_id
        return _private(result)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        return _unavailable()
