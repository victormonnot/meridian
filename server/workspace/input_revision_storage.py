"""Verified, independent publication of prepared input revisions."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import uuid

from meridian.sensor_contract import contract_sha256, loads_contract, validate_bundle
from . import acquisition_storage as storage

MAX_FILES = 30_000
MAX_BYTES = 2 * 1024**3


def root(*, create=False):
    path = storage.root() / "revisions"
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.exists() and (path.is_symlink() or path.resolve(strict=True) != path or not path.is_dir()):
        raise ValueError("Unsafe revision storage root.")
    return path


def directory(revision_id):
    return root() / str(uuid.UUID(str(revision_id)))


def cleanup(revision_id):
    path = directory(revision_id)
    if path.is_symlink():
        raise ValueError("Unsafe revision directory.")
    if path.exists():
        shutil.rmtree(path)
        storage.sync_directory(path.parent)


def discard_attempt(revision_id, attempt_id):
    parent = directory(revision_id) / "published"
    if parent.exists() and (parent.is_symlink() or parent.resolve() != parent):
        raise ValueError("Unsafe revision publication directory.")
    for suffix in ("", ".publishing"):
        path = parent / (str(uuid.UUID(str(attempt_id))) + suffix)
        if path.is_symlink():
            raise ValueError("Unsafe revision attempt directory.")
        if path.exists():
            shutil.rmtree(path)


def checked_manifest(manifest):
    total = storage.checked_manifest(manifest)
    marker = storage.canonical(manifest) + b"\n"
    if len(manifest["files"]) + 1 > MAX_FILES or total + len(marker) > MAX_BYTES:
        raise ValueError("Prepared publication exceeds its file or byte budget.")
    return total + len(marker), len(manifest["files"]) + 1


def source_path(value):
    source = value.acquisition
    if (source.owner_id != value.owner_id or source.project_id != value.project_id
            or source.state != "ready" or not source.files_available
            or source.published_attempt != value.source_attempt
            or {"id": source.descriptor["id"], "sha256": contract_sha256(source.descriptor)} != value.source_identity):
        raise ValueError("The frozen source acquisition is unavailable or changed.")
    path = storage.directory(source.pk) / "published" / str(value.source_attempt) / "acquisition"
    if path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("Unsafe source acquisition directory.")
    return path


def records(value):
    request = value.request
    return [value.acquisition.descriptor,
            *[loads_contract(text) for text in request["dependency_acquisitions"]],
            *([loads_contract(request["calibration"])] if request["calibration"] is not None else []),
            *[loads_contract(text) for text in request["clock_mappings"]]]


def validate_outcome(value, outcome):
    manifest, prepared, report = outcome["manifest"], outcome["prepared_input"], outcome["result"]
    total, files = checked_manifest(manifest)
    validate_bundle([*records(value), prepared])
    calibration = loads_contract(value.request["calibration"]) if value.request["calibration"] else None
    mappings = [loads_contract(text) for text in value.request["clock_mappings"]]
    identity = lambda document: {"id": document["id"], "sha256": contract_sha256(document)}
    if (prepared["kind"] != "prepared_input" or prepared["acquisition"] != value.source_identity
            or prepared["id"] != value.request["prepared_id"] or prepared["method"] != value.plan["method"]
            or prepared["operations"] != value.plan["operations"] or prepared["outputs"] != value.plan["outputs"]
            or prepared["calibration"] != (identity(calibration) if calibration else None)
            or prepared["clock_mappings"] != [identity(item) for item in mappings]):
        raise ValueError("Published preparation differs from the frozen application plan.")
    selected = {item["stream_id"] for item in value.plan["outputs"]}
    selections = [{"stream_id": stream["id"], "segment_id": segment["id"],
                   "first_tick": segment["first_tick"], "last_tick": segment["last_tick"]}
                  for stream in value.acquisition.descriptor["streams"] if stream["id"] in selected
                  for segment in stream["segments"]]
    if prepared["selections"] != selections:
        raise ValueError("Prepared selections differ from the frozen full-source request.")
    expected = {item["path"]: {key: item[key] for key in ("sha256", "size_bytes")} for item in prepared["files"]}
    metadata = {"prepared-input.json", "source-acquisition.json", "preparation-report.json"}
    if set(manifest["files"]) != set(expected) | metadata or any(manifest["files"][name] != identity for name, identity in expected.items()):
        raise ValueError("Revision publication inventory is not the exact prepared payload and metadata closure.")
    if total > value.reserved_bytes or files > value.reserved_files or len(storage.canonical(report)) > 256 * 1024:
        raise ValueError("Prepared result exceeds its reservation or report budget.")
    if report.get("execution_readiness") != "not_assessed" or report.get("calibration_validity") != "not_assessed":
        raise ValueError("Preparation cannot establish physical or execution readiness.")
    if (report.get("status") != "accepted" or report.get("acquisition") != value.source_identity
            or report.get("prepared_input") != identity(prepared) or report.get("method") != value.plan["method"]
            or (report.get("application") or {}).get("request_sha256") != value.plan["request_sha256"]
            or (report.get("application") or {}).get("application_status") != "completed"):
        raise ValueError("Preparation report differs from its exact source, output or application identity.")
    return total, files


def _read_json(staging, name, limit, *, contract=False):
    with storage.open_relative(staging, name) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Prepared metadata exceeds its bound.")
    if contract:
        return loads_contract(content)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate prepared report field.")
            value[key] = item
        return value
    def finite(value):
        raise ValueError("Nonfinite prepared report value.")
    return json.loads(content.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)


def publish(value, attempt_id, staging, outcome, *, checkpoint):
    validate_outcome(value, outcome)
    staging = Path(staging)
    prepared = _read_json(staging, "prepared-input.json", 32 * 1024**2, contract=True)
    report = _read_json(staging, "preparation-report.json", 256 * 1024)
    source = _read_json(staging, "source-acquisition.json", 32 * 1024**2, contract=True)
    if (contract_sha256(prepared) != contract_sha256(outcome["prepared_input"])
            or storage.canonical(report) != storage.canonical(outcome["result"])
            or {"id": source["id"], "sha256": contract_sha256(source)} != value.source_identity):
        raise ValueError("Published metadata differs from its handoff or source descriptor.")
    artifacts = records(value)[1:]
    expected = {item["id"]: item for item in artifacts}
    found = {}
    for item in prepared["files"]:
        if item["role"] == "evidence":
            document = _read_json(staging, item["path"], 2 * 1024**2, contract=True)
            if (document["id"] not in expected or document["id"] in found
                    or contract_sha256(document) != contract_sha256(expected[document["id"]])):
                raise ValueError("Prepared evidence differs from the selected immutable contracts.")
            found[document["id"]] = document
    if set(found) != set(expected):
        raise ValueError("Prepared artifact evidence is incomplete.")
    root(create=True)
    parent = directory(value.pk) / "published"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.resolve(strict=True) != parent:
        raise ValueError("Unsafe revision publication directory.")
    destination, temporary = parent / str(attempt_id), parent / (str(attempt_id) + ".publishing")
    if destination.exists() or temporary.exists():
        raise ValueError("Revision attempt publication already exists.")
    temporary.mkdir(mode=0o700)
    try:
        for name, identity in sorted(outcome["manifest"]["files"].items()):
            checkpoint("publishing")
            output = temporary / name
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest, size = sha256(), 0
            with storage.open_relative(staging, name) as source, output.open("xb") as target:
                before = os.fstat(source.fileno())
                if before.st_size != identity["size_bytes"]:
                    raise ValueError("Publication size differs from the frozen manifest.")
                while block := source.read(storage.BLOCK):
                    size += len(block)
                    if size > identity["size_bytes"]:
                        raise ValueError("Publication file grew during copying.")
                    digest.update(block)
                    target.write(block)
                    checkpoint("publishing")
                target.flush()
                os.fsync(target.fileno())
                after = os.fstat(source.fileno())
            if (size != identity["size_bytes"] or digest.hexdigest() != identity["sha256"]
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("Revision publication bytes changed or failed their identity check.")
        with (temporary / "publication-manifest.json").open("xb") as target:
            target.write(storage.canonical(outcome["manifest"]) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        for path, _, _ in os.walk(temporary, topdown=False):
            storage.sync_directory(path)
        checkpoint("publishing")
        os.rename(temporary, destination)
        storage.sync_directory(parent)
        return True
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
