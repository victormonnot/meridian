"""Resolve exact owned prepared publications for private execution admission."""
from hashlib import sha256
import os
import uuid

from meridian.sensor_contract import contract_sha256, loads_contract
from meridian import vio_private_specification as profile

from . import acquisition_storage as storage
from .api import endpoint, error, not_found, response
from .experiments import owned_project, payload
from .models import InputRevision
from .sensor_artifact_api import private

MAX_METADATA_BYTES = 16 * 1024**2


def identifier(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError("Select a canonical input revision UUID.")
    return uuid.UUID(value)


def binding(value):
    return {"input_revision_id": str(value.pk), "acquisition_id": str(value.acquisition_id),
            "published_attempt": str(value.published_attempt),
            "acquisition_sha256": value.source_identity["sha256"],
            "prepared_input_sha256": contract_sha256(value.prepared_input),
            "publication_manifest_sha256": sha256(storage.canonical(value.manifest) + b"\n").hexdigest()}


def publication(value):
    if (value.state != "ready" or not value.files_available or not value.published_attempt
            or value.acquisition.owner_id != value.owner_id
            or value.acquisition.project_id != value.project_id):
        raise ValueError("The selected prepared input has no available publication.")
    path = storage.root() / "revisions" / str(value.pk) / "published" / str(value.published_attempt)
    if path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("The prepared input publication is unavailable or unsafe.")
    return path


def read_metadata(value, name):
    path = publication(value)
    expected = (value.manifest or {}).get("files", {}).get(name)
    if (type(expected) is not dict or type(expected.get("size_bytes")) is not int
            or not 0 < expected["size_bytes"] <= MAX_METADATA_BYTES):
        raise ValueError("Prepared metadata is missing or exceeds its bounded size.")
    with storage.open_relative(path, name) as source:
        before = os.fstat(source.fileno())
        content = source.read(expected["size_bytes"] + 1)
        after = os.fstat(source.fileno())
    if (before.st_size != expected["size_bytes"] or len(content) != expected["size_bytes"]
            or sha256(content).hexdigest() != expected.get("sha256")
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("Prepared metadata differs from its immutable inventory.")
    return loads_contract(content)


def records(value):
    """Metadata-only application records survive deletion of original payloads."""
    prepared = value.prepared_input
    source = value.acquisition.descriptor
    if any(type(item) is not dict for item in (prepared, source, value.result, value.request, value.source_identity)):
        raise ValueError("Saved preparation metadata is incomplete.")
    if ({"id": source["id"], "sha256": contract_sha256(source)} != value.source_identity
            or value.result.get("prepared_input") != {"id": prepared["id"], "sha256": contract_sha256(prepared)}):
        raise ValueError("Saved source or prepared contract identity changed.")
    calibration = loads_contract(value.request["calibration"]) if value.request["calibration"] else None
    mappings = [loads_contract(text) for text in value.request["clock_mappings"]]
    return prepared, source, calibration, mappings


def build(value, *, parameters=None, interval=None, with_timeline=False):
    root = publication(value)
    prepared, source, calibration, mappings = records(value)
    identity = binding(value)
    expected_marker = storage.canonical(value.manifest) + b"\n"
    if len(expected_marker) > MAX_METADATA_BYTES:
        raise ValueError("Publication metadata exceeds its bounded size.")
    with storage.open_relative(root, "publication-manifest.json") as handle:
        if handle.read(len(expected_marker) + 1) != expected_marker:
            raise ValueError("The input publication manifest changed.")
    for name, document in (("prepared-input.json", prepared), ("source-acquisition.json", source)):
        if contract_sha256(read_metadata(value, name)) != contract_sha256(document):
            raise ValueError("Retained descriptors differ from the saved input identities.")
    spec = profile.build_private_specification(identity, prepared, source, calibration,
        parameters=parameters, interval=interval, clock_mappings=mappings)
    from meridian.vio_private_inputs import inspect_private_timeline
    timeline = inspect_private_timeline(spec, root)
    return (spec, timeline) if with_timeline else spec


def owned(project, owner, value):
    return InputRevision.objects.select_related("acquisition").filter(
        pk=identifier(value), project=project, owner=owner, project__owner=owner).first()


def resolve_job(job):
    """Revalidate the frozen binding before every attempt, without retargeting it."""
    value = InputRevision.objects.select_related("acquisition").filter(
        pk=job.input_revision_id, project_id=job.project_id, owner_id=job.owner_id,
        project__owner_id=job.owner_id).first()
    if value is None:
        raise ValueError("The frozen prepared input is unavailable.")
    actual = build(value, parameters=job.specification["parameters"], interval=job.specification["interval"])
    if profile.specification_sha256(actual) != job.specification_sha256:
        raise ValueError("The frozen execution input or specification changed.")
    return value, publication(value)


@endpoint(["GET"])
def inputs(request, project_id):
    project = owned_project(request, project_id)
    if project is None:
        return not_found(request)
    if request.GET:
        return error("invalid_request", "Input lists do not accept query parameters.", 400)
    # Extract only compact JSON fields: a prepared inventory can be many MiB.
    rows = InputRevision.objects.filter(project=project, owner=request.user, state="ready").values(
        "id", "name", "acquisition_id", "acquisition__name", "files_available", "published_attempt",
        "prepared_input__id", "result__prepared_input__sha256", "source_identity")
    items = [{"id": str(row["id"]), "name": row["name"],
              "acquisition_id": str(row["acquisition_id"]), "acquisition_name": row["acquisition__name"],
              "availability": "available" if row["files_available"] and row["published_attempt"] else "deleted",
              "prepared_input": {"id": row["prepared_input__id"], "sha256": row["result__prepared_input__sha256"]},
              "source_identity": row["source_identity"]} for row in rows]
    return private(response({"profile": profile.configuration_profile(), "inputs": items}))


@endpoint(["POST"])
def assess(request, project_id):
    project = owned_project(request, project_id)
    if project is None:
        return not_found(request)
    data, failure = payload(request, {"schema_version", "input_revision_id", "parameters", "interval"},
                            {"schema_version", "input_revision_id"})
    if failure is not None:
        return failure
    try:
        value = owned(project, request.user, data["input_revision_id"])
    except (ValueError, AttributeError):
        return error("invalid_request", "Select a valid input revision UUID.", 400)
    if value is None:
        return not_found(request)
    selection = {"input_revision_id": str(value.pk), "interval": data.get("interval")}
    try:
        spec, timeline = build(value, parameters=data.get("parameters"), interval=data.get("interval"), with_timeline=True)
        selection["interval"] = spec["interval"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "The retained input metadata is unavailable or invalid."
        return private(response({"eligible": False, "profile_id": profile.PROFILE_ID,
            "reasons": [{"code": "unsupported_input", "message": message}], "selection": selection,
            "profile": profile.configuration_profile()}))
    return private(response({"eligible": True, "profile_id": profile.PROFILE_ID, "reasons": [],
        "selection": selection, "timeline": timeline, "specification": spec, "profile": profile.configuration_profile()}))
