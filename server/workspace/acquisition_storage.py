"""Private, verified publication and bounded upload IO; no image decoding."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from django.conf import settings

BLOCK = 1024 * 1024
MAX_PUBLISHED_FILES = 60_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def root():
    path = Path(settings.MERIDIAN_ACQUISITION_ROOT)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("Private acquisition storage is unavailable or unsafe.")
    return path


def directory(acquisition_id):
    return root() / str(uuid.UUID(str(acquisition_id)))


def source_path(acquisition_id):
    return directory(acquisition_id) / "original"


def safe_name(name, *, maximum=256):
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > maximum
            or name.startswith("/") or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ord(letter) < 32 or 127 <= ord(letter) < 160 for letter in name)):
        raise ValueError("Unsafe retained file path.")
    if maximum == 256 and len(json.dumps(name, ensure_ascii=False).encode("utf-8")) - 2 > 256:
        raise ValueError("Hosted JSON-serialized paths exceed their byte budget.")
    return name


def open_relative(base, name, *, writable=False):
    """Walk every directory without following links; the final file is regular."""
    safe_name(name, maximum=512)
    base = Path(base)
    if base.resolve(strict=True) != base:
        raise ValueError("Unsafe storage root.")
    descriptors = [os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
        fd = os.open(parts[-1], (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptors[-1])
        stream = os.fdopen(fd, "r+b" if writable else "rb")
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            stream.close()
            raise ValueError("Retained content is not an independent regular file.")
        return stream
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def provision(acquisition_id):
    path = directory(acquisition_id)
    path.mkdir(mode=0o700)
    fd = os.open(path / "original", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    sync_directory(path)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def require_free_bytes(additional):
    floor = settings.MERIDIAN_MIN_FREE_DISK_GB * 1024**3
    if shutil.disk_usage(root()).free < additional + floor:
        raise ValueError("Private recording writes must retain the configured free-disk floor.")


def append_chunk(acquisition, offset, content):
    """Caller holds the global lifecycle mutex through filesystem and DB commit."""
    require_free_bytes(len(content))
    with open_relative(root(), f"{acquisition.pk}/original", writable=True) as stream:
        length = os.fstat(stream.fileno()).st_size
        if length < acquisition.bytes_received:
            raise ValueError("Acknowledged upload bytes are missing.")
        # A crash after fsync but before DB commit may leave unacknowledged bytes.
        stream.truncate(acquisition.bytes_received)
        stream.seek(offset)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def verify_replayed_chunk(acquisition, offset, size, digest):
    with open_relative(root(), f"{acquisition.pk}/original") as stream:
        stream.seek(offset)
        content = stream.read(size)
    return len(content) == size and sha256(content).hexdigest() == digest


def cleanup(acquisition_id):
    path = directory(acquisition_id)
    if path.is_symlink():
        raise ValueError("Unsafe acquisition directory.")
    if path.exists():
        shutil.rmtree(path)
        sync_directory(path.parent)


def discard_attempt(acquisition_id, attempt_id):
    parent = directory(acquisition_id) / "published"
    if parent.exists() and (parent.is_symlink() or parent.resolve() != parent):
        raise ValueError("Unsafe publication directory.")
    for suffix in ("", ".publishing"):
        target = parent / (str(uuid.UUID(str(attempt_id))) + suffix)
        if target.is_symlink():
            raise ValueError("Unsafe publication attempt.")
        if target.exists():
            shutil.rmtree(target)


def checked_manifest(manifest):
    if (type(manifest) is not dict or set(manifest) != {"schema_version", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or type(manifest["files"]) is not dict or not 1 <= len(manifest["files"]) <= MAX_PUBLISHED_FILES):
        raise ValueError("Invalid acquisition publication manifest.")
    total = 0
    for name, identity in manifest["files"].items():
        safe_name(name)
        if name == "publication-manifest.json":
            raise ValueError("Publication manifest cannot inventory itself.")
        if (type(identity) is not dict or set(identity) != {"size_bytes", "sha256"}
                or type(identity["size_bytes"]) is not int or not 0 <= identity["size_bytes"] <= 4 * 1024**3
                or not isinstance(identity["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", identity["sha256"])):
            raise ValueError("Invalid publication file identity.")
        total += identity["size_bytes"]
    if len(canonical(manifest)) > 64 * 1024**2:
        raise ValueError("Publication metadata exceeds its limit.")
    return total


def _inspection_json(staging, name):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate inspection metadata key.")
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError("Nonfinite inspection metadata.")

    with open_relative(Path(staging), name) as stream:
        encoded = stream.read(1024**2 + 1)
    if len(encoded) > 1024**2:
        raise ValueError("Inspection metadata exceeds its byte budget.")
    value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    if type(value) is not dict:
        raise ValueError("Inspection metadata requires an object.")
    return value


def check_inspection_publication(staging, report, manifest, descriptor, *, checkpoint=None):
    """Bind optional inspection indexes to this publication before exposing them."""
    evidence = report.get("inspection")
    if evidence is None:
        return  # Existing 2.3 snapshots have no inspection-generation claim.
    members = {name: identity for name, identity in manifest["files"].items()
               if name.startswith("inspection/")}
    if evidence == {"status": "unavailable", "reason": "inspection_resource_limit"}:
        if members:
            raise ValueError("Unavailable inspection must not publish partial evidence.")
        return
    from meridian.sensor_contract import contract_sha256
    from meridian.sensor_inspection import validate_summary
    name = "inspection/summary.json"
    if evidence != {"status": "available", "method_version": "sensor-inspection-v1", "summary_path": name}:
        raise ValueError("Unsupported inspection publication claim.")
    if name not in members or sum(item["size_bytes"] for item in members.values()) > 512 * 1024**2:
        raise ValueError("Inspection publication is missing or exceeds its storage budget.")
    for path, identity in members.items():
        if path == name or path.startswith("inspection/streams/") and path.endswith(".json"):
            maximum = 1024**2
        elif path.startswith("inspection/previews/") and path.endswith(".png"):
            maximum = 128 * 1024
        else:
            raise ValueError("Unsupported inspection artifact path.")
        if not 0 < identity["size_bytes"] <= maximum:
            raise ValueError("Inspection artifact exceeds its individual byte budget.")
    summary = _inspection_json(staging, name)
    validate_summary(summary)
    if (type(summary["schema_version"]) is not int
            or summary["acquisition"] != {"id": descriptor["id"], "sha256": contract_sha256(descriptor)}):
        raise ValueError("Inspection summary refers to a different acquisition.")
    indexed = {chunk["path"] for stream in summary["streams"]
               for segment in stream["segments"] for chunk in segment["chunks"]}
    if indexed != {path for path in members if path.startswith("inspection/streams/")}:
        raise ValueError("Inspection chunk inventory differs from its summary index.")
    sources = {item["path"]: item for item in descriptor["files"]}
    camera_sources = {item["id"]: item for item in descriptor["streams"] if item["kind"] == "camera"}
    previews = set()
    for stream in summary["streams"]:
        if stream["kind"] != "camera":
            continue
        camera = camera_sources.get(stream["id"])
        if camera is None or camera["camera"] != stream.get("camera"):
            raise ValueError("Inspection camera differs from its source declaration.")
        for segment in stream["segments"]:
            for index in segment["chunks"]:
                if checkpoint:
                    checkpoint("publishing")
                chunk = _inspection_json(staging, index["path"])
                if (type(chunk.get("schema_version")) is not int or chunk["schema_version"] != 1
                        or chunk.get("method_version") != "sensor-inspection-v1"
                        or chunk.get("stream_id") != stream["id"] or chunk.get("segment_id") != segment["id"]
                        or type(chunk.get("rows")) is not list or len(chunk["rows"]) != index["rows"]):
                    raise ValueError("Camera chunk differs from its inspection index.")
                for row in chunk["rows"]:
                    image = row.get("image") if type(row) is dict else None
                    if type(image) is not dict or type(image.get("preview_path")) is not str:
                        raise ValueError("Camera observation omits its preview identity.")
                    path = image["preview_path"]
                    source = sources.get(image.get("source_path"))
                    if (re.fullmatch(r"inspection/previews/[0-9]{6}\.png", path) is None or path not in members
                            or source is None or source["role"] != "sensor"
                            or source["sha256"] != image.get("source_sha256")
                            or source["size_bytes"] != image.get("source_size_bytes")
                            or [image.get("width"), image.get("height")] != camera["camera"]["resolution"]
                            or any(type(image.get(key)) is not int or not 1 <= image[key] <= 320
                                   for key in ("preview_width", "preview_height"))):
                        raise ValueError("Published preview is absent or differs from its source image.")
                    previews.add(path)
    if previews != {path for path in members if path.startswith("inspection/previews/")}:
        raise ValueError("Inspection publication contains an orphan preview.")


def publish_import(acquisition_id, attempt_id, staging, outcome, *, checkpoint=None):
    """Copy verified child output to durable storage, then atomically rename.

    The caller's live lease guard must stay active through this bounded copy.
    Only finish_import can expose the directory to HTTP readers.
    """
    from .models import Acquisition
    acquisition = Acquisition.objects.get(pk=acquisition_id)
    if acquisition.current_attempt != attempt_id or acquisition.state != "validating":
        return False
    from meridian.sensor_contract import loads_contract, validate_bundle
    total = checked_manifest(outcome["manifest"])
    metadata = {}
    for name, maximum in (("acquisition/acquisition.json", 32 * 1024**2),
                          ("prepared/prepared-input.json", 32 * 1024**2), ("report.json", 256 * 1024)):
        if name not in outcome["manifest"]["files"]:
            raise ValueError("A published acquisition requires its descriptor, preparation and report.")
        with open_relative(Path(staging), name) as stream:
            encoded = stream.read(maximum + 1)
        if len(encoded) > maximum:
            raise ValueError("Publication metadata exceeds its byte budget.")
        metadata[name] = json.loads(encoded) if name == "report.json" else loads_contract(encoded)
    descriptor, prepared = metadata["acquisition/acquisition.json"], metadata["prepared/prepared-input.json"]
    validate_bundle([descriptor, prepared])
    if descriptor != outcome["descriptor"] or metadata["report.json"] != outcome["result"]:
        raise ValueError("Published metadata differs from the worker outcome.")
    check_inspection_publication(staging, metadata["report.json"], outcome["manifest"], descriptor,
                                 checkpoint=checkpoint)
    for branch, contract in (("acquisition", descriptor), ("prepared", prepared)):
        for item in contract["files"]:
            if outcome["manifest"]["files"].get(branch + "/" + item["path"]) != {key: item[key] for key in ("sha256", "size_bytes")}:
                raise ValueError("Published inventory omits or changes declared sensor bytes.")
    digest, count = sha256(), 0
    with open_relative(root(), f"{acquisition.pk}/original") as original:
        before = os.fstat(original.fileno())
        while block := original.read(BLOCK):
            count += len(block)
            if count > acquisition.size_bytes:
                raise ValueError("Uploaded archive grew before publication.")
            digest.update(block)
            if checkpoint:
                checkpoint("publishing")
        after = os.fstat(original.fileno())
    if (count != acquisition.size_bytes or digest.hexdigest() != outcome.get("sha256")
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("Original archive failed its final identity check.")
    maximum = acquisition.budget["unpacked_bytes"] + acquisition.budget["prepared_bytes"]
    if total > maximum:
        raise ValueError("Published inputs exceed their reserved storage.")
    parent = directory(acquisition_id) / "published"
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.resolve(strict=True) != parent:
        raise ValueError("Unsafe publication directory.")
    destination = parent / str(attempt_id)
    temporary = parent / (str(attempt_id) + ".publishing")
    if destination.exists() or temporary.exists():
        raise ValueError("Attempt publication already exists.")
    temporary.mkdir(mode=0o700)
    try:
        for name, identity in sorted(outcome["manifest"]["files"].items()):
            if checkpoint:
                checkpoint("publishing")
            output = temporary / name
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest, size = sha256(), 0
            with open_relative(Path(staging), name) as source, output.open("xb") as target:
                before = os.fstat(source.fileno())
                if before.st_size != identity["size_bytes"]:
                    raise ValueError("Publication file size differs from its manifest.")
                while block := source.read(BLOCK):
                    size += len(block)
                    if size > identity["size_bytes"]:
                        raise ValueError("Publication file grew during verification.")
                    digest.update(block)
                    target.write(block)
                    if checkpoint:
                        checkpoint("publishing")
                target.flush()
                os.fsync(target.fileno())
                after = os.fstat(source.fileno())
            if (size != identity["size_bytes"] or digest.hexdigest() != identity["sha256"]
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("Publication source changed or failed its identity check.")
        with (temporary / "publication-manifest.json").open("xb") as target:
            target.write(canonical(outcome["manifest"]) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        for path, _, _ in os.walk(temporary, topdown=False):
            sync_directory(path)
        if checkpoint:
            checkpoint("publishing")
        os.rename(temporary, destination)
        sync_directory(parent)
        return True
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
