"""Bounded owner-scoped imports of immutable calibration and clock declarations."""
from hashlib import sha256
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from meridian.sensor_contract import contract_sha256, loads_contract, validate_bundle
from . import acquisition_storage as storage
from .api import endpoint, error, not_found, response
from .experiments import owned_project
from .job_queue import queue_lock
from .models import Acquisition, SensorArtifact

MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_BODY_BYTES = 256 * 1024
ARTIFACT_LIMITS = {"descriptor_bytes": MAX_DESCRIPTOR_BYTES, "request_bytes": MAX_BODY_BYTES,
                   "source_acquisitions": 16, "per_project": 50, "per_account": 100, "global_count": 500}


def private(result):
    result["Cache-Control"] = "private, no-store"
    patch_vary_headers(result, ["Cookie"])
    return result


def canonical_uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError("Use a canonical UUID.")
    return value


def name_field(value):
    if (type(value) is not str or not 1 <= len(value.strip()) <= 100
            or any(ord(letter) < 32 or 127 <= ord(letter) < 160 for letter in value)):
        raise ValueError("Use a nonempty name of at most 100 characters.")
    value.encode("utf-8")
    return value.strip()


def read_request(request, allowed, required, *, maximum=MAX_BODY_BYTES):
    """This route alone streams a bounded JSON body above the ordinary 8 KiB cap."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = value
        return result

    def finite(value):
        raise ValueError("Nonfinite JSON value.")

    if request.content_type != "application/json":
        raise ValueError("Send an application/json object.")
    length = request.META.get("CONTENT_LENGTH", "")
    if not re.fullmatch(r"[1-9][0-9]{0,8}", length) or int(length) > maximum:
        raise ValueError("The JSON request exceeds its byte limit.")
    raw = request.read(maximum + 1)
    if len(raw) != int(length) or len(raw) > maximum:
        raise ValueError("The JSON request has invalid byte bounds.")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
    if type(value) is not dict or set(value) - allowed or required - set(value):
        raise ValueError("The request contains missing or unsupported fields.")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported request schema version.")
    return value


def owned_sources(owner, project, identifiers, contract):
    if type(identifiers) is not list or not 1 <= len(identifiers) <= 16:
        raise ValueError("Bind one to sixteen owned source acquisition UUIDs.")
    ids = [canonical_uuid(value) for value in identifiers]
    if len(set(ids)) != len(ids):
        raise ValueError("Source acquisition UUIDs must be distinct.")
    rows = list(Acquisition.objects.filter(owner=owner, project=project, project__owner=owner,
                                          pk__in=ids, state="ready").order_by("id"))
    if len(rows) != len(ids) or any(not row.descriptor for row in rows):
        raise LookupError("Source acquisition is unavailable.")
    expected = [contract["acquisition"]]
    if contract["kind"] == "calibration":
        expected += contract["validation"]["acquisitions"]
    expected = {(item["id"], item["sha256"]) for item in expected}
    records, bindings = {}, []
    for row in rows:
        descriptor = row.descriptor
        digest = contract_sha256(descriptor)
        identity = descriptor["id"], digest
        if identity not in expected or descriptor["id"] in records:
            raise ValueError("Source bindings must resolve each declared acquisition identity exactly once.")
        records[descriptor["id"]] = descriptor
        bindings.append({"acquisition_id": str(row.pk), "contract_id": descriptor["id"],
                         "contract_sha256": digest, "published_attempt": str(row.published_attempt) if row.published_attempt else None})
    if {(item["contract_id"], item["contract_sha256"]) for item in bindings} != expected:
        raise ValueError("Some declared acquisition identities remain unresolved.")
    validate_bundle([*records.values(), contract])
    return bindings


def artifact_payload(value, *, detail=False):
    report = (value.contract["validation"]["report"] if value.kind == "calibration"
              else value.contract["evidence"]["report"])
    result = {"id": str(value.pk), "project_id": str(value.project_id), "name": value.name,
              "kind": value.kind, "contract_id": value.contract["id"], "contract_sha256": value.contract_sha256,
              "descriptor_sha256": value.descriptor_sha256,
              "created_at": value.created_at.isoformat(),
              "archived_at": value.archived_at.isoformat() if value.archived_at else None,
              "source_bindings": value.source_bindings,
              "evidence": {"origin": "uploaded", "metadata_validation": "passed",
                           "report_payload_verification": "not_performed" if report else "not_provided",
                           "physical_validity": "not_assessed", "execution_readiness": "not_assessed"}}
    if detail:
        result["contract"] = value.contract
    return result


@endpoint(["GET", "POST"])
def artifacts(request, project_id):
    with transaction.atomic():
        if request.method == "POST":
            queue_lock()
        project = owned_project(request, project_id, lock=request.method == "POST")
        if project is None:
            return not_found(request)
        if request.method == "GET":
            return private(response({"artifacts": [artifact_payload(item) for item in project.sensor_artifacts.all()],
                                     "limits": ARTIFACT_LIMITS}))
        if not settings.MERIDIAN_UPLOADS_ENABLED:
            return error("uploads_disabled", "Private recording changes are unavailable.", 503)
        fields = {"schema_version", "name", "idempotency_key", "descriptor_text", "acquisition_ids"}
        try:
            data = read_request(request, fields, fields)
            name, key = name_field(data["name"]), canonical_uuid(data["idempotency_key"])
            raw = data["descriptor_text"]
            if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= MAX_DESCRIPTOR_BYTES:
                raise ValueError("The artifact descriptor exceeds its 64 KiB limit.")
            contract = loads_contract(raw)
            if contract["kind"] not in {"calibration", "clock_mapping"}:
                raise ValueError("Import a calibration or clock-mapping contract.")
            digest = sha256(storage.canonical({"project_id": str(project.pk), "name": name,
                "descriptor_text": raw, "acquisition_ids": data["acquisition_ids"]})).hexdigest()
            previous = SensorArtifact.objects.filter(owner=request.user, idempotency_key=key).first()
            if previous:
                if previous.request_sha256 != digest:
                    return error("idempotency_conflict", "This key already identifies another artifact import.", 409)
                return private(response({"artifact": artifact_payload(previous, detail=True)}))
            bindings = owned_sources(request.user, project, data["acquisition_ids"], contract)
        except LookupError:
            return not_found(request)
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            return error("invalid_artifact", "Use a supported bounded contract and exact owned acquisition bindings.", 400)
        if (project.sensor_artifacts.count() >= ARTIFACT_LIMITS["per_project"]
                or SensorArtifact.objects.filter(owner=request.user).count() >= ARTIFACT_LIMITS["per_account"]
                or SensorArtifact.objects.count() >= ARTIFACT_LIMITS["global_count"]):
            return error("artifact_limit", "The supplied artifact count limit has been reached.", 409)
        value = SensorArtifact.objects.create(owner=request.user, project=project, name=name, kind=contract["kind"],
            descriptor_text=raw, descriptor_sha256=sha256(raw.encode("utf-8")).hexdigest(), contract=contract,
            contract_sha256=contract_sha256(contract), source_bindings=bindings, idempotency_key=key, request_sha256=digest)
        return private(response({"artifact": artifact_payload(value, detail=True)}, 201))


def owned_artifact(request, project_id, artifact_id):
    return SensorArtifact.objects.filter(pk=artifact_id, owner=request.user, project_id=project_id,
                                         project__owner=request.user).first()


@endpoint(["GET", "DELETE"])
def artifact(request, project_id, artifact_id):
    with transaction.atomic():
        if request.method == "DELETE":
            queue_lock()
        value = owned_artifact(request, project_id, artifact_id)
        if value is None:
            return not_found(request)
        if request.method == "DELETE" and value.archived_at is None:
            SensorArtifact.objects.filter(pk=value.pk).update(archived_at=timezone.now())
            value.refresh_from_db()
        return private(response({"artifact": artifact_payload(value, detail=True)}))


@endpoint(["GET"])
def contract(request, project_id, artifact_id):
    value = owned_artifact(request, project_id, artifact_id)
    if value is None:
        return not_found(request)
    raw = value.descriptor_text.encode("utf-8")
    if sha256(raw).hexdigest() != value.descriptor_sha256:
        return error("artifact_unavailable", "The stored artifact failed its identity check.", 503)
    result = HttpResponse(raw, content_type="application/json")
    result["Content-Disposition"] = f'attachment; filename="{value.kind}-{value.pk}.json"'
    result["Content-Length"] = str(len(raw))
    result["X-Content-SHA256"] = value.descriptor_sha256
    result["X-Contract-SHA256"] = value.contract_sha256
    return private(result)
