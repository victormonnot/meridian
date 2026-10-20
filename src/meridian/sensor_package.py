"""Bounded local sensor-package materialization and payload validation.

No estimator, calibration solver, network access or implicit data repair occurs
here. Source bytes survive unchanged; validated CSV rows retain their order and
original decimal representations.
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tarfile
import warnings
import zipfile
import zlib

from PIL import Image

from .sensor_contract import assess_acquisition, contract_sha256, loads_contract
from .sensor_conventions import timestamp_ns, timestamp_to_ns, validate_quaternion_xyzw


@dataclass(frozen=True)
class PackageLimits:
    max_total_bytes: int = 8 * 1024**3
    max_file_bytes: int = 4 * 1024**3
    max_members: int = 100_000
    max_csv_rows: int = 2_000_000
    max_image_pixels: int = 16_000_000
    max_descriptor_bytes: int = 32 * 1024**2
    max_csv_bytes: int = 512 * 1024**2
    max_png_bytes: int = 64 * 1024**2
    max_archive_metadata_bytes: int = 64 * 1024**2

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("Package limits must be positive integers.")


DEFAULT_LIMITS = PackageLimits()
HEADERS = {
    "camera": ["sample_id", "segment_id", "timestamp", "filename"],
    "imu": ["sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz"],
    "reference_pose": ["sample_id", "segment_id", "timestamp", "px", "py", "pz", "qx", "qy", "qz", "qw"],
}
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_SAMPLE_ID = re.compile(r"0|[1-9][0-9]*\Z")
_CHUNK = 1024 * 1024


class PackageValidationError(ValueError):
    """One actionable deterministic finding, without absolute host paths."""

    def __init__(self, code, message, *, path=None, stream_id=None, row=None):
        self.finding = {"code": code, "message": message}
        for key, value in (("path", path), ("stream_id", stream_id), ("row", row)):
            if value is not None:
                self.finding[key] = Path(value).name if key == "path" and str(value).startswith("/") else value
        super().__init__(message)


def _error(code, message, **context):
    raise PackageValidationError(code, message, **context)


def _safe_name(name, *, directory=False):
    if type(name) is not str or not name or len(name) > 512:
        _error("unsafe_path", "Member paths must be bounded nonempty relative POSIX paths.")
    if directory and name.endswith("/"):
        name = name[:-1]
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name)
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        _error("unsafe_path", "Absolute paths, control characters and traversal components are unsupported.")
    try:
        name.encode("utf-8")
    except UnicodeError:
        _error("unsafe_path", "Member names must be valid Unicode.")
    return name


def _open_path(path, *, directory=False):
    """Open every absolute component without following a symbolic link."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(absolute.parts[1:]):
            last = index == len(absolute.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last or directory:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        os.close(descriptor)
        _error("unsafe_source", "Source is unavailable or contains a symbolic link.")


def _fingerprint(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def _regular(path, *, maximum):
    descriptor = _open_path(path)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _error("special_file", "Only regular files without hard links are supported.")
        if before.st_size > maximum:
            _error("file_limit", "File exceeds its allowed byte limit.")
        yield source
        if _fingerprint(before) != _fingerprint(os.fstat(source.fileno())):
            _error("source_changed", "Source changed while it was being read.")


def file_identity(path, *, limits=DEFAULT_LIMITS):
    digest, size = hashlib.sha256(), 0
    with _regular(path, maximum=limits.max_file_bytes) as source:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > limits.max_file_bytes:
                _error("file_limit", "File grew beyond its allowed byte limit.")
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def copy_regular(source, destination, *, limits=DEFAULT_LIMITS):
    """Copy a regular unlinked source to a new file, preserving exact bytes."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    ancestor = destination.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    descriptor = _open_path(ancestor, directory=True)
    os.close(descriptor)
    created = False
    try:
        with _regular(source, maximum=limits.max_file_bytes) as content:
            size = os.fstat(content.fileno()).st_size
            if size > limits.max_total_bytes:
                _error("total_limit", "Source exceeds its aggregate byte limit.")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created, copied, digest = True, 0, hashlib.sha256()
            with os.fdopen(descriptor, "wb") as target:
                while chunk := content.read(min(_CHUNK, size - copied + 1)):
                    copied += len(chunk)
                    if copied > size:
                        _error("source_changed", "Source grew while copying.")
                    digest.update(chunk)
                    target.write(chunk)
            if copied != size:
                _error("source_changed", "Source shrank while copying.")
        return {"sha256": digest.hexdigest(), "size_bytes": copied}
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _tree(root, limits):
    """Inventory a directory through no-follow directory descriptors."""
    files, directories = {}, set()
    root_fd = _open_path(root, directory=True)
    def visit(descriptor, prefix=""):
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            relative = _safe_name(prefix + name)
            if len(files) + len(directories) >= limits.max_members:
                _error("member_limit", "Source exceeds its member-count limit.")
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    visit(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                if metadata.st_size > limits.max_file_bytes:
                    _error("file_limit", "File exceeds its allowed byte limit.", path=relative)
                files[relative] = _fingerprint(metadata)
            else:
                _error("special_file", "Links and special filesystem entries are unsupported.", path=relative)
    try:
        visit(root_fd)
    except OSError:
        _error("source_changed", "Directory changed or became unavailable during inspection.")
    finally:
        os.close(root_fd)
    if sum(value[2] for value in files.values()) > limits.max_total_bytes:
        _error("total_limit", "Source exceeds its aggregate byte limit.")
    return files, directories


def _copy_member(source, destination, name, size, budget, limits):
    if type(size) is not int or not 0 <= size <= limits.max_file_bytes:
        _error("file_limit", "Member exceeds its allowed byte limit.", path=name)
    budget[0] += size
    if budget[0] > limits.max_total_bytes:
        _error("total_limit", "Source exceeds its aggregate byte limit.")
    path = destination / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest, written = hashlib.sha256(), 0
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            while chunk := source.read(min(_CHUNK, size - written + 1)):
                written += len(chunk)
                if written > size:
                    _error("member_size", "Member contains more bytes than declared.", path=name)
                digest.update(chunk)
                target.write(chunk)
    except OSError:
        _error("destination_error", "Cannot safely create a materialized member.", path=name)
    if written != size:
        _error("member_size", "Member is truncated.", path=name)
    return {"sha256": digest.hexdigest(), "size_bytes": written}


def _zip_preflight(source, limits):
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        _error("archive_format", "ZIP end-of-directory record is missing.")
    _, disk, central_disk, local_count, count, central_size, central_offset, comment = struct.unpack(
        "<4s4H2LH", tail[offset:offset + 22])
    if (disk or central_disk or local_count != count or count == 65535
            or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF):
        _error("archive_format", "Multipart and ZIP64 archives are unsupported by this bounded profile.")
    if (count > limits.max_members or central_size > limits.max_archive_metadata_bytes
            or offset + 22 + comment != len(tail)
            or central_offset + central_size != size - len(tail) + offset):
        _error("archive_limit", "ZIP directory is inconsistent or exceeds its limits.")
    source.seek(0)


def materialize_source(source, destination, *, limits=DEFAULT_LIMITS, ignored_aliases=None):
    """Copy a raw directory/tar/zip to a new directory with verified inventories.

    Exact upstream symlink aliases may be explicitly ignored by a maintained
    adapter. They are never extracted. General packages pass no such exception.
    """
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    source_absolute, destination_absolute = Path(os.path.abspath(source)), Path(os.path.abspath(destination))
    if destination_absolute == source_absolute or destination_absolute.is_relative_to(source_absolute):
        _error("destination_error", "Destination must be outside its source.")
    aliases = ignored_aliases or {}
    if type(aliases) is not dict:
        _error("archive_format", "Ignored aliases must be an exact name-to-target mapping.")
    for name, target in aliases.items():
        _safe_name(name)
        if type(target) is not str:
            _error("archive_format", "Ignored alias targets must be exact strings.")
    inventory, budget, names = {}, [0], set()
    try:
        metadata = source.lstat()
    except OSError:
        _error("unsafe_source", "Source is unavailable.")
    source_descriptor = _open_path(source, directory=stat.S_ISDIR(metadata.st_mode))
    os.close(source_descriptor)
    parent = destination.parent
    while not parent.exists() and not parent.is_symlink():
        parent = parent.parent
    parent_descriptor = _open_path(parent, directory=True)
    os.close(parent_descriptor)
    destination.mkdir(parents=True, mode=0o700)
    try:
        if stat.S_ISDIR(metadata.st_mode):
            files, directories = _tree(source, limits)
            for name in sorted(directories):
                (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, fingerprint in sorted(files.items()):
                with _regular(source / name, maximum=limits.max_file_bytes) as content:
                    if _fingerprint(os.fstat(content.fileno())) != fingerprint:
                        _error("source_changed", "Source member changed before copying.", path=name)
                    inventory[name] = _copy_member(content, destination, name, fingerprint[2], budget, limits)
            if _tree(source, limits) != (files, directories):
                _error("source_changed", "Source directory changed during materialization.")
        else:
            with _regular(source, maximum=limits.max_file_bytes) as content:
                magic = content.read(4)
                content.seek(0)
                if magic in {b"PK\x03\x04", b"PK\x05\x06"}:
                    _zip_preflight(content, limits)
                    with zipfile.ZipFile(content) as archive:
                        for member in archive.infolist():
                            name = _safe_name(member.orig_filename, directory=member.is_dir())
                            if name in names or len(names) >= limits.max_members:
                                _error("duplicate_member", "Duplicate member or exceeded member limit.", path=name)
                            names.add(name)
                            mode = member.external_attr >> 16
                            if (member.filename != member.orig_filename or member.flag_bits & 1
                                    or member.extract_version > 20
                                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                                    or (stat.S_IFMT(mode) and member.is_dir() != stat.S_ISDIR(mode))):
                                _error("archive_member", "ZIP links, special files, encryption or encoding are unsupported.", path=name)
                            if member.is_dir():
                                if member.file_size:
                                    _error("archive_member", "ZIP directories cannot contain payload bytes.", path=name)
                                (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
                                continue
                            with archive.open(member) as item:
                                inventory[name] = _copy_member(item, destination, name, member.file_size, budget, limits)
                else:
                    metadata_budget = [0, 0]
                    class BoundedTarInfo(tarfile.TarInfo):
                        def _proc_member(self, archive):
                            metadata_budget[1] += 1
                            if metadata_budget[1] > limits.max_members:
                                _error("member_limit", "Archive exceeds its header-count limit.")
                            if self.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}:
                                metadata_budget[0] += self.size
                                if metadata_budget[0] > limits.max_archive_metadata_bytes:
                                    _error("archive_limit", "Tar extended metadata exceeds its byte limit.")
                            if self.type == tarfile.GNUTYPE_SPARSE:
                                _error("archive_member", "Sparse tar files are unsupported.")
                            return super()._proc_member(archive)
                    with tarfile.open(fileobj=content, mode="r|*", tarinfo=BoundedTarInfo) as archive:
                        for member in archive:
                            name = _safe_name(member.name, directory=member.isdir())
                            if name in names or len(names) >= limits.max_members:
                                _error("duplicate_member", "Duplicate member or exceeded member limit.", path=name)
                            names.add(name)
                            if member.issym() and not member.size and aliases.get(name) == member.linkname:
                                continue
                            if member.isdir():
                                if member.size:
                                    _error("archive_member", "Tar directories cannot contain payload bytes.", path=name)
                                (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
                                continue
                            if not member.isfile() or member.sparse is not None:
                                _error("archive_member", "Archive links, sparse and special files are unsupported.", path=name)
                            item = archive.extractfile(member)
                            if item is None:
                                _error("archive_member", "Archive member cannot be read.", path=name)
                            with item:
                                inventory[name] = _copy_member(item, destination, name, member.size, budget, limits)
        return dict(sorted(inventory.items()))
    except PackageValidationError:
        shutil.rmtree(destination)
        raise
    except (OSError, ValueError, EOFError, RecursionError, tarfile.TarError, zipfile.BadZipFile, zlib.error) as error:
        shutil.rmtree(destination)
        _error("source_invalid", "Source cannot be safely read: " + type(error).__name__ + ".")


def _read_bytes(path, maximum):
    with _regular(path, maximum=maximum) as source:
        content = source.read(maximum + 1)
        if len(content) > maximum:
            _error("file_limit", "Payload exceeds its allowed byte limit.")
        return content


def _decimal(value, context):
    if len(value) > 128 or _NUMBER.fullmatch(value) is None:
        _error("numeric_value", "Expected a finite decimal measurement.", **context)
    number = float(value)
    if not math.isfinite(number):
        _error("numeric_value", "Measurement must be finite.", **context)
    return number


def _csv_lines(text, context):
    while line := text.readline(32 * 1024 + 1):
        if len(line) > 32 * 1024:
            _error("csv_columns", "CSV physical lines must not exceed 32768 characters.", **context)
        yield line


def iter_stream_rows(root, stream, *, limits=DEFAULT_LIMITS):
    """Yield checked original string columns, excluding the exact header."""
    path = stream["index_path"]
    context = {"path": path, "stream_id": stream["id"]}
    segments = stream["segments"]
    segment_index, segment_rows, previous_id, previous_tick, first_tick = 0, 0, -1, None, None
    try:
        with _regular(Path(root) / path, maximum=min(limits.max_csv_bytes, limits.max_file_bytes)) as source:
            text = io.TextIOWrapper(source, encoding="utf-8", newline="")
            try:
                reader = csv.reader(_csv_lines(text, context), strict=True)
                if next(reader, None) != HEADERS[stream["kind"]]:
                    _error("csv_header", "CSV header does not match the declared stream kind.", **context)
                count = 0
                for line, row in enumerate(reader, 2):
                    row_context = {**context, "row": line}
                    count += 1
                    if count > limits.max_csv_rows:
                        _error("row_limit", "CSV exceeds its sample-count limit.", **row_context)
                    if len(row) != len(HEADERS[stream["kind"]]) or any(len(field) > 4096 for field in row):
                        _error("csv_columns", "CSV row has an invalid field count or oversized field.", **row_context)
                    if _SAMPLE_ID.fullmatch(row[0]) is None or len(row[0]) > 19 or int(row[0]) <= previous_id:
                        _error("sample_identity", "Sample IDs must be canonical nonnegative integers increasing in file order.", **row_context)
                    previous_id = int(row[0])
                    try:
                        tick = timestamp_ns(row[2])
                    except ValueError:
                        _error("timestamp", "Timestamp must be a canonical signed-int64 tick string.", **row_context)
                    if row[1] != segments[segment_index]["id"]:
                        _check_segment(segments[segment_index], segment_rows, first_tick, previous_tick, context)
                        segment_index += 1
                        if segment_index >= len(segments) or row[1] != segments[segment_index]["id"]:
                            _error("segment_order", "Rows must follow the declared contiguous segment order.", **row_context)
                        segment_rows, previous_tick, first_tick = 0, None, None
                    if previous_tick is not None and tick <= previous_tick:
                        _error("timestamp_order", "Timestamps must strictly increase within a segment; declare discontinuities explicitly.", **row_context)
                    first_tick = tick if first_tick is None else first_tick
                    previous_tick, segment_rows = tick, segment_rows + 1
                    if stream["kind"] == "camera":
                        _safe_name(row[3])
                    else:
                        values = [_decimal(field, row_context) for field in row[3:]]
                        if stream["kind"] == "reference_pose":
                            try:
                                validate_quaternion_xyzw(values[3:])
                            except ValueError:
                                _error("quaternion", "Reference orientation must be a unit Hamilton xyzw quaternion.", **row_context)
                    yield row
                _check_segment(segments[segment_index], segment_rows, first_tick, previous_tick, context)
                if segment_index != len(segments) - 1:
                    _error("segment_missing", "CSV omits a declared segment.", **context)
            finally:
                text.detach()
    except PackageValidationError:
        raise
    except (UnicodeError, csv.Error, OSError) as error:
        _error("csv_invalid", "CSV cannot be decoded as strict UTF-8 rows: " + type(error).__name__ + ".", **context)


def _check_segment(segment, rows, first, last, context):
    if (rows != segment["sample_count"] or first != int(segment["first_tick"])
            or last != int(segment["last_tick"])):
        _error("segment_summary", "Actual segment count or endpoint timestamps differ from the descriptor.", **context)


def _validate_png(content, expected, *, limits, path):
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        _error("image_format", "Expected a PNG image.", path=path)
    position, seen_header, seen_data, ended_data, ended, inflater, inflated = 8, False, False, False, False, None, 0
    expected_bytes = None
    try:
        while position < len(content):
            if len(content) - position < 12:
                _error("image_truncated", "PNG chunk is truncated.", path=path)
            length, kind = struct.unpack(">I4s", content[position:position + 8])
            if not all(65 <= letter <= 90 or 97 <= letter <= 122 for letter in kind) or not 65 <= kind[2] <= 90:
                _error("image_structure", "PNG chunk types require alphabetic names and the reserved bit clear.", path=path)
            end = position + 12 + length
            if end > len(content):
                _error("image_truncated", "PNG chunk exceeds the file boundary.", path=path)
            data = content[position + 8:position + 8 + length]
            crc, = struct.unpack(">I", content[position + 8 + length:end])
            if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
                _error("image_crc", "PNG chunk CRC does not match.", path=path)
            if kind in {b"acTL", b"fcTL", b"fdAT"}:
                _error("image_encoding", "Animated PNG chunks are unsupported; each observation requires a static image.", path=path)
            if not seen_header and kind != b"IHDR":
                _error("image_structure", "PNG must start with IHDR.", path=path)
            if kind == b"IHDR":
                if seen_header or length != 13:
                    _error("image_structure", "PNG requires one complete IHDR.", path=path)
                width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if (not width or not height or width * height > limits.max_image_pixels
                        or [width, height] != expected["resolution"]):
                    _error("image_dimensions", "Image dimensions differ from the descriptor or exceed the pixel limit.", path=path)
                if (bits != {"mono8": 8, "mono16": 16}[expected["encoding"]]
                        or color != 0 or compression or filtering or interlace):
                    _error("image_encoding", "Only declared noninterlaced grayscale PNG8/PNG16 images are supported.", path=path)
                expected_bytes, inflater, seen_header = height * (1 + width * (bits // 8)), zlib.decompressobj(), True
            elif kind == b"IDAT":
                if ended_data:
                    _error("image_structure", "PNG IDAT chunks must be contiguous.", path=path)
                seen_data = True
                remaining = data
                while remaining:
                    output = inflater.decompress(remaining, min(_CHUNK, expected_bytes - inflated + 1))
                    inflated += len(output)
                    if inflated > expected_bytes or inflater.unused_data:
                        _error("image_expansion", "PNG decompression exceeds its declared image shape.", path=path)
                    remaining = inflater.unconsumed_tail
            elif kind == b"IEND":
                if length or not seen_data or end != len(content):
                    _error("image_structure", "PNG IEND must terminate a complete image.", path=path)
                ended = True
            elif kind[:1].isupper():
                _error("image_encoding", "Unsupported critical PNG chunk.", path=path)
            if kind != b"IDAT" and seen_data:
                ended_data = True
            position = end
        if not ended or not inflater.eof or inflated != expected_bytes:
            _error("image_truncated", "PNG compressed image data is incomplete.", path=path)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.mode not in {"L", "I;16", "I;16B", "I"}:
                    _error("image_encoding", "Decoded PNG is not a supported grayscale image.", path=path)
    except PackageValidationError:
        raise
    except (OSError, ValueError, SyntaxError, zlib.error, Image.DecompressionBombWarning, Image.DecompressionBombError):
        _error("image_decode", "PNG cannot be fully decoded.", path=path)


def validate_package(root, *, limits=DEFAULT_LIMITS):
    """Verify all inventoried payload bytes and supported stream semantics."""
    root = Path(root)
    files, directories = _tree(root, limits)
    if "acquisition.json" not in files:
        _error("descriptor_missing", "Package requires acquisition.json.")
    try:
        acquisition = loads_contract(_read_bytes(root / "acquisition.json", limits.max_descriptor_bytes))
    except ValueError as error:
        if isinstance(error, PackageValidationError):
            raise
        _error("descriptor_invalid", str(error), path="acquisition.json")
    if acquisition["kind"] != "acquisition":
        _error("descriptor_kind", "Package descriptor must describe an acquisition.")
    declared = {item["path"]: item for item in acquisition["files"]}
    if "acquisition.json" in declared:
        _error("reserved_path", "The descriptor cannot inventory or replace itself.")
    if set(files) != set(declared) | {"acquisition.json"}:
        missing, extra = sorted(set(declared) - set(files)), sorted(set(files) - set(declared) - {"acquisition.json"})
        _error("inventory_mismatch", "Package has a missing or unlisted file.",
               path=missing[0] if missing else extra[0])
    parents = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}
    if directories - parents:
        _error("inventory_mismatch", "Package has an unlisted empty directory.", path=sorted(directories - parents)[0])
    for name, identity in sorted(declared.items()):
        if file_identity(root / name, limits=limits) != {key: identity[key] for key in ("sha256", "size_bytes")}:
            _error("file_identity", "Payload bytes differ from their declared size or SHA-256.", path=name)
    clocks = {item["id"]: item for item in acquisition["clocks"]}
    consumed, images, summaries, total_rows = set(), set(), {}, 0
    for stream in acquisition["streams"]:
        consumed.add(stream["index_path"])
        count = 0
        for line, row in enumerate(iter_stream_rows(root, stream, limits=limits), 2):
            total_rows, count = total_rows + 1, count + 1
            if total_rows > limits.max_csv_rows:
                _error("row_limit", "Package exceeds its aggregate sample-count limit.")
            try:
                timestamp_to_ns(row[2], clocks[stream["clock_id"]]["unit"])
            except ValueError:
                _error("timestamp", "Timestamp cannot be converted exactly to signed-int64 nanoseconds.",
                       stream_id=stream["id"], row=line)
            if stream["kind"] == "camera":
                name = (PurePosixPath(stream["index_path"]).parent / _safe_name(row[3])).as_posix()
                if name not in declared or declared[name]["role"] != "sensor":
                    _error("image_inventory", "Camera image must resolve to an inventoried sensor file.", path=name,
                           stream_id=stream["id"], row=line)
                if name in images:
                    _error("image_identity", "Each camera observation requires its own inventoried image path.", path=name)
                images.add(name)
                consumed.add(name)
                _validate_png(_read_bytes(root / name, min(limits.max_png_bytes, limits.max_file_bytes)),
                              stream["camera"], limits=limits, path=name)
        summaries[stream["id"]] = {"kind": stream["kind"], "samples": count,
                                   "segments": [dict(item) for item in stream["segments"]],
                                   "measurement_semantics": stream["measurement_semantics"]}
    for name, item in declared.items():
        if item["role"] in {"sensor", "reference"} and name not in consumed:
            _error("unreferenced_payload", "Sensor/reference payload is not used by a declared stream.", path=name)
    pairing = acquisition["stereo_pairing"]
    if pairing and pairing["policy"] == "exact_timestamp_pairs":
        streams = {item["id"]: item for item in acquisition["streams"]}
        left, right = (streams[name] for name in pairing["camera_ids"])
        for a, b in zip(iter_stream_rows(root, left, limits=limits), iter_stream_rows(root, right, limits=limits), strict=True):
            if a[2] != b[2]:
                _error("stereo_pairing", "Declared stereo pair has different actual timestamps.")
    if _tree(root, limits) != (files, directories):
        _error("source_changed", "Package changed during validation.")
    report = {
        "schema_version": 1, "status": "validated", "scope": "supported_payload_syntax_and_declared_identity",
        "acquisition_sha256": contract_sha256(acquisition), "payload_verification": "verified",
        "execution_readiness": "not_assessed", "calibration_validity": "not_assessed",
        "files": len(declared), "payload_bytes": sum(item["size_bytes"] for item in declared.values()),
        "rows": total_rows, "images": len(images), "streams": summaries,
        "reference_availability": "present" if any(s["kind"] == "reference_pose" for s in acquisition["streams"]) else "absent",
        "metadata_assessment": assess_acquisition(acquisition),
        "limits": dict(vars(limits)),
    }
    return {"acquisition": acquisition, "report": report}


def materialize_package(source, destination, *, limits=DEFAULT_LIMITS):
    """Materialize an exact package, validate it, and remove failed new outputs."""
    materialize_source(source, destination, limits=limits)
    try:
        return validate_package(destination, limits=limits)
    except BaseException:
        shutil.rmtree(destination)
        raise
