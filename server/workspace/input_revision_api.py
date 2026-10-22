"""Explicit immutable preparation selections; no calibration fitting or VIO admission."""
from hashlib import sha256
import os

from django.conf import settings
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone

from meridian.sensor_calibration import validate_application_request
from meridian.sensor_contract import contract_sha256, loads_contract
from . import acquisition_queue, acquisition_storage
from . import input_revision_queue as queue, input_revision_storage as storage
from .api import endpoint, error, not_found, response
from .job_queue import queue_lock
from .models import Acquisition, InputRevision, SensorArtifact
from .sensor_artifact_api import canonical_uuid, name_field, private, read_request

OPTIONS = {"calibration_id", "clock_mapping_ids", "apply_imu_intrinsics", "imu_target_frame", "imu_stream_id", "include_reference"}


def owned_source(request, project_id, acquisition_id):
    return Acquisition.objects.filter(pk=acquisition_id, owner=request.user, project_id=project_id,
                                      project__owner=request.user).first()


def owned_revision(request, project_id, acquisition_id, revision_id):
    return InputRevision.objects.select_related("acquisition").filter(pk=revision_id, acquisition_id=acquisition_id,
        project_id=project_id, owner=request.user, project__owner=request.user,
        acquisition__project_id=project_id, acquisition__owner=request.user).first()


def _artifact(value):
    parsed = loads_contract(value.descriptor_text)
    if (sha256(value.descriptor_text.encode("utf-8")).hexdigest() != value.descriptor_sha256
            or parsed != value.contract or contract_sha256(parsed) != value.contract_sha256
            or contract_sha256(value.contract) != value.contract_sha256):
        raise ValueError("A selected artifact failed its stored identity check.")
    return parsed


def application(source, user, data):
    if source.state != "ready" or not source.files_available or source.published_attempt is None:
        raise FileNotFoundError("Source recording files are unavailable.")
    calibration_id = data.get("calibration_id")
    if calibration_id is not None:
        canonical_uuid(calibration_id)
    mapping_ids = data.get("clock_mapping_ids", [])
    if type(mapping_ids) is not list or len(mapping_ids) > queue.limits()["clock_mappings"]:
        raise ValueError("Select a bounded list of clock mappings.")
    mapping_ids = [canonical_uuid(value) for value in mapping_ids]
    identifiers = ([calibration_id] if calibration_id else []) + mapping_ids
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Select distinct artifact identities.")
    artifacts = list(SensorArtifact.objects.filter(owner=user, project_id=source.project_id, project__owner=user,
                                                   pk__in=identifiers, archived_at__isnull=True))
    if len(artifacts) != len(identifiers):
        raise LookupError("Selected artifact is unavailable.")
    artifacts = {str(item.pk): item for item in artifacts}
    calibration = _artifact(artifacts[calibration_id]) if calibration_id else None
    mappings = [_artifact(artifacts[key]) for key in mapping_ids]
    if ((calibration is not None and calibration["kind"] != "calibration")
            or any(item["kind"] != "clock_mapping" for item in mappings)):
        raise ValueError("Selected artifacts have incompatible kinds.")
    dependencies = {}
    source_identity = {"id": source.descriptor["id"], "sha256": contract_sha256(source.descriptor)}
    for artifact in artifacts.values():
        for binding in artifact.source_bindings:
            record = Acquisition.objects.filter(pk=binding["acquisition_id"], owner=user,
                project_id=source.project_id, project__owner=user, state="ready").first()
            if record is None or not record.descriptor:
                raise LookupError("Bound acquisition metadata is unavailable.")
            identity = {"id": record.descriptor["id"], "sha256": contract_sha256(record.descriptor)}
            if identity != {"id": binding["contract_id"], "sha256": binding["contract_sha256"]}:
                raise ValueError("A bound source descriptor changed.")
            if identity == source_identity:
                continue
            if identity["id"] == source_identity["id"] or (identity["id"] in dependencies and dependencies[identity["id"]] != record.descriptor):
                raise ValueError("Different acquisitions reuse one contract identifier in this selection.")
            dependencies[identity["id"]] = record.descriptor
    if len(dependencies) > 16:
        raise ValueError("At most sixteen dependency acquisitions are supported per preparation.")
    selection = {"calibration_id": calibration_id, "clock_mapping_ids": sorted(mapping_ids),
        "apply_imu_intrinsics": data.get("apply_imu_intrinsics", False), "imu_target_frame": data.get("imu_target_frame"),
        "imu_stream_id": data.get("imu_stream_id"), "include_reference": data.get("include_reference", True),
        "artifacts": [{"id": key, "kind": artifacts[key].kind, "contract_sha256": artifacts[key].contract_sha256}
                      for key in sorted(artifacts)]}
    plan = validate_application_request(source.descriptor, calibration=calibration, clock_mappings=mappings,
        dependency_acquisitions=list(dependencies.values()), apply_imu_intrinsics=selection["apply_imu_intrinsics"],
        imu_target_frame=selection["imu_target_frame"], imu_stream=selection["imu_stream_id"],
        include_reference=selection["include_reference"], image_encoding="preserve")
    raw_by_id = {item.contract["id"]: item.descriptor_text for item in artifacts.values()}
    request = {"prepared_id": plan["prepared_id"], "source_identity": source_identity,
        "calibration": artifacts[calibration_id].descriptor_text if calibration_id else None,
        "clock_mappings": [raw_by_id[item["id"]] for item in plan["request"]["clock_mappings"]],
        "dependency_acquisitions": [acquisition_storage.canonical(item).decode("utf-8")
                                   for item in plan["request"]["dependency_acquisitions"]],
        "apply_imu_intrinsics": selection["apply_imu_intrinsics"], "imu_target_frame": selection["imu_target_frame"],
        "imu_stream_id": plan["request"]["imu_stream"], "include_reference": selection["include_reference"]}
    if len(acquisition_storage.canonical(request)) > 2 * 1024**2:
        raise ValueError("The frozen request exceeds its 2 MiB metadata budget.")
    plan = {key: value for key, value in plan.items() if key != "request"}
    return selection, plan, request


def revision_payload(value, *, detail=False):
    available = value.state == "ready" and value.files_available and value.published_attempt is not None
    availability = "available" if available else "deleted" if value.deleted_at else "unavailable"
    if available:
        try:
            relative = f"revisions/{value.pk}/published/{value.published_attempt}/publication-manifest.json"
            with acquisition_storage.open_relative(acquisition_storage.root(), relative):
                pass
        except (OSError, ValueError):
            available, availability = False, "missing"
    result = {"id": str(value.pk), "project_id": str(value.project_id), "acquisition_id": str(value.acquisition_id),
        "name": value.name, "state": value.state, "phase": value.phase,
        "source_attempt": str(value.source_attempt), "source_identity": value.source_identity,
        "selection": value.selection, "plan": value.plan, "request_sha256": value.request_sha256,
        "created_at": value.created_at.isoformat(), "updated_at": value.updated_at.isoformat(),
        "finished_at": value.finished_at.isoformat() if value.finished_at else None,
        "cancel_requested": bool(value.cancel_requested_at), "attempt_count": value.attempt_count,
        "files_available": bool(available), "availability": availability,
        "reserved_bytes": value.reserved_bytes, "retained_bytes": value.retained_bytes,
        "failure": value.failure, "execution_readiness": "not_assessed", "calibration_validity": "not_assessed"}
    if detail:
        result.update({"prepared_input": value.prepared_input, "result": value.result, "manifest": value.manifest})
    return result


def _problem(error_value):
    if isinstance(error_value, FileNotFoundError):
        return error("source_unavailable", "The source recording has no retained available observations.", 409)
    if isinstance(error_value, LookupError):
        return error("not_found", "The requested resource was not found.", 404)
    return error("unsupported_preparation", "The selected artifacts or operations do not support this recording.", 400,
                 {"selection": str(error_value)[:240]})


@endpoint(["POST"])
def assess(request, project_id, acquisition_id):
    source = owned_source(request, project_id, acquisition_id)
    if source is None:
        return not_found(request)
    try:
        data = read_request(request, OPTIONS | {"schema_version"}, {"schema_version"}, maximum=8192)
        selection, plan, _ = application(source, request.user, data)
    except (ValueError, TypeError, LookupError, OSError, OverflowError, RecursionError) as problem:
        return _problem(problem)
    return private(response({"eligible": True, "application_status": "planned", "assessment": plan["assessment"],
                             "plan": plan, "selection": selection}))


@endpoint(["GET", "POST"])
def revisions(request, project_id, acquisition_id):
    with transaction.atomic():
        if request.method == "POST":
            queue_lock()
        source = owned_source(request, project_id, acquisition_id)
        if source is None:
            return not_found(request)
        if request.method == "GET":
            return private(response({"revisions": [revision_payload(item) for item in source.input_revisions.all()],
                "limits": queue.limits(), "storage": acquisition_queue.usage(request.user), "service": acquisition_queue.service_status()}))
        if not settings.MERIDIAN_UPLOADS_ENABLED:
            return error("uploads_disabled", "Private recording changes are unavailable.", 503)
        try:
            data = read_request(request, OPTIONS | {"schema_version", "name", "idempotency_key"},
                                {"schema_version", "name", "idempotency_key"}, maximum=8192)
            name, key = name_field(data["name"]), canonical_uuid(data["idempotency_key"])
            # Replays remain idempotent after an artifact is archived or source files are deleted.
            body_identity = {"project_id": str(project_id), "acquisition_id": str(acquisition_id),
                             **{key: data.get(key) for key in sorted(OPTIONS)}, "name": name}
            digest = sha256(acquisition_storage.canonical(body_identity)).hexdigest()
            previous = InputRevision.objects.filter(owner=request.user, idempotency_key=key).first()
            if previous:
                if previous.request_sha256 != digest:
                    return error("idempotency_conflict", "This key already identifies another preparation request.", 409)
                return private(response({"revision": revision_payload(previous, detail=True)}))
            selection, plan, frozen = application(source, request.user, data)
        except (ValueError, TypeError, LookupError, OSError, OverflowError, RecursionError) as problem:
            return _problem(problem)
        bounds, owner, totals = queue.limits(), acquisition_queue.usage(request.user), acquisition_queue.usage()
        if (source.input_revisions.count() >= bounds["per_acquisition"]
                or InputRevision.objects.filter(owner=request.user).count() >= bounds["per_account"]
                or InputRevision.objects.count() >= bounds["global_count"]):
            return error("revision_limit", "The input revision count limit has been reached.", 409)
        overall = acquisition_queue.limits()
        if (owner["reserved_bytes"] + bounds["prepared_bytes"] > overall["account_bytes"]
                or totals["reserved_bytes"] + bounds["prepared_bytes"] > overall["global_bytes"]
                or owner["reserved_files"] + bounds["prepared_files"] > overall["account_files"]
                or totals["reserved_files"] + bounds["prepared_files"] > overall["global_files"]):
            return error("storage_quota", "There is insufficient shared recording quota for this derived revision.", 409)
        try:
            acquisition_storage.require_free_bytes(bounds["prepared_bytes"])
        except (OSError, ValueError):
            return error("storage_capacity", "The host currently lacks preparation publication capacity.", 409)
        value = InputRevision.objects.create(owner=request.user, project=source.project, acquisition=source, name=name,
            source_attempt=source.published_attempt, source_identity=frozen["source_identity"], selection=selection,
            request=frozen, plan=plan, idempotency_key=key, request_sha256=digest,
            reserved_bytes=bounds["prepared_bytes"], reserved_files=bounds["prepared_files"])
        return private(response({"revision": revision_payload(value, detail=True)}, 201))


@endpoint(["GET"])
def revision(request, project_id, acquisition_id, revision_id):
    value = owned_revision(request, project_id, acquisition_id, revision_id)
    return private(response({"revision": revision_payload(value, detail=True)})) if value else not_found(request)


@endpoint(["POST"])
def cancel(request, project_id, acquisition_id, revision_id):
    with transaction.atomic():
        queue_lock()
        value = owned_revision(request, project_id, acquisition_id, revision_id)
        if value is None:
            return not_found(request)
        try:
            read_request(request, {"schema_version"}, {"schema_version"}, maximum=8192)
        except (ValueError, TypeError, UnicodeError):
            return error("invalid_request", "Send only schema_version 1.", 400)
        if value.state in {"queued", "preparing"}:
            value.cancel_requested_at = value.cancel_requested_at or timezone.now()
            if value.state == "queued":
                value.state, value.phase, value.finished_at = "cancelled", "complete", timezone.now()
                value.failure = queue.failure("cancelled")
                if acquisition_queue.compute_active():
                    value.phase = "cleanup_pending"
                else:
                    queue.release_files_locked(value)
            value.save()
        return private(response({"revision": revision_payload(value, detail=True)}))


@endpoint(["GET"])
def contract(request, project_id, acquisition_id, revision_id):
    value = owned_revision(request, project_id, acquisition_id, revision_id)
    if value is None:
        return not_found(request)
    if not value.prepared_input:
        return error("inputs_unavailable", "This revision has no completed prepared contract.", 409)
    return private(response({"prepared_input": value.prepared_input}))


@endpoint(["GET"])
def report(request, project_id, acquisition_id, revision_id):
    value = owned_revision(request, project_id, acquisition_id, revision_id)
    return private(response({"report": value.result, "failure": value.failure})) if value else not_found(request)


@endpoint(["GET", "DELETE"])
def files(request, project_id, acquisition_id, revision_id):
    if request.method == "DELETE":
        with transaction.atomic():
            queue_lock()
            value = owned_revision(request, project_id, acquisition_id, revision_id)
            if value is None:
                return not_found(request)
            if value.state in {"queued", "preparing"}:
                return error("active_preparation", "Cancel the preparation and wait for it to stop before deleting files.", 409)
            if acquisition_queue.compute_active():
                return error("cleanup_busy", "Wait for active computations to stop before deleting files.", 409)
            try:
                queue.release_files_locked(value)
                value.save()
            except (OSError, ValueError):
                return error("storage_unavailable", "Revision cleanup has not completed; quota remains reserved.", 503)
            return private(response({"deleted": True, "revision": revision_payload(value, detail=True)}))
    value = owned_revision(request, project_id, acquisition_id, revision_id)
    if value is None:
        return not_found(request)
    if value.state != "ready" or not value.files_available or not value.published_attempt:
        return error("inputs_unavailable", "This revision has no retained available files.", 410)
    if set(request.GET) != {"path"} or len(request.GET.getlist("path")) != 1:
        return error("invalid_request", "Select exactly one published file path.", 400)
    try:
        name = acquisition_storage.safe_name(request.GET["path"])
    except (ValueError, UnicodeError):
        return error("invalid_request", "Use a safe relative published file path.", 400)
    identity = (value.manifest or {}).get("files", {}).get(name)
    if identity is None:
        return not_found(request)
    stream = None
    try:
        relative = f"revisions/{value.pk}/published/{value.published_attempt}/{name}"
        stream = acquisition_storage.open_relative(acquisition_storage.root(), relative)
        if os.fstat(stream.fileno()).st_size != identity["size_bytes"]:
            raise ValueError("Published revision size changed.")
        if not InputRevision.objects.filter(pk=value.pk, files_available=True, published_attempt=value.published_attempt).exists():
            stream.close()
            return error("inputs_unavailable", "The revision files were deleted.", 410)
        result = FileResponse(stream, as_attachment=True, filename=name.rsplit("/", 1)[-1], content_type="application/octet-stream")
        result["Content-Length"] = str(identity["size_bytes"])
        result["X-Content-SHA256"] = identity["sha256"]
        return private(result)
    except (OSError, ValueError):
        if stream:
            stream.close()
        return error("inputs_unavailable", "A retained revision file is missing or failed its storage check.", 503)
