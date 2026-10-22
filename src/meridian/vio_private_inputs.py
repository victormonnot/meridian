"""Bounded sensor-only native adaptation of an immutable prepared revision.

Only CSV indices and deterministic configuration are staged. Camera bytes remain
in verified read-only revision directories; reference and contract files never
belong to the returned estimator mount set. This module needs only the stdlib.
"""
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat

from .sensor_contract import contract_sha256, loads_contract
from .vio_private_specification import (
    MAX_CAMERA_PAIRS, MAX_IMU_SAMPLES, MAX_IMAGE_BYTES, build_private_specification,
    canonical, render_configuration, specification_sha256, timestamp_ns, _path,
)

MAX_REVISION_BYTES = 2 * 1024**3
MAX_METADATA_BYTES = 32 * 1024**2
MAX_CSV_BYTES = 16 * 1024**2
MAX_STAGE_BYTES = 48 * 1024**2
_HEADERS = {
    "camera": ["sample_id", "segment_id", "timestamp", "filename"],
    "imu": ["sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz"],
    "reference": ["sample_id", "segment_id", "timestamp", "px", "py", "pz", "qx", "qy", "qz", "qw"],
}
IMU_HEADER = ["#timestamp [ns]", *[f"w_RS_S_{a} [rad s^-1]" for a in "xyz"], *[f"a_RS_S_{a} [m s^-2]" for a in "xyz"]]
POSE_HEADER = ["timestamp_ns", "px_m", "py_m", "pz_m", "qx", "qy", "qz", "qw"]


def strict_json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("Duplicate JSON field.")
            value[key] = item
        return value
    def finite(value): raise ValueError("Nonfinite JSON value.")
    return json.loads(data, object_pairs_hook=unique, parse_constant=finite)


@contextmanager
def _regular(path, maximum):
    """Open every component without following symbolic links."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                raise ValueError("Source is not a bounded independent regular file.")
            with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                yield handle, before
            after = os.fstat(file_descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Input changed during verification.")
        finally: os.close(file_descriptor)
    finally: os.close(descriptor)


def _read(path, maximum, *, expected=None, checkpoint=lambda: None, keep=True):
    digest, parts, total = hashlib.sha256(), [], 0
    with _regular(path, maximum) as (handle, before):
        while chunk := handle.read(1024**2):
            checkpoint(); total += len(chunk)
            if total > maximum: raise ValueError("Input exceeds its byte limit.")
            digest.update(chunk)
            if keep: parts.append(chunk)
    identity = {"sha256": digest.hexdigest(), "size_bytes": total}
    if expected is not None and identity != expected:
        raise ValueError("Immutable prepared file differs from its manifest.")
    return b"".join(parts) if keep else identity


def _csv(root, path, kind, inventory, checkpoint):
    raw = _read(root / path, MAX_CSV_BYTES, expected=inventory[path], checkpoint=checkpoint)
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""))
    if next(reader, None) != _HEADERS[kind]: raise ValueError("Prepared CSV header differs.")
    result, previous, ids = [], None, set()
    maximum = MAX_CAMERA_PAIRS if kind == "camera" else MAX_IMU_SAMPLES
    for index, row in enumerate(reader):
        if index % 256 == 0: checkpoint()
        if index >= maximum or len(row) != len(_HEADERS[kind]): raise ValueError("Prepared rows exceed their bounded schema.")
        if row[0] in ids: raise ValueError("Prepared sample identities must be unique.")
        ids.add(row[0])
        stamp = timestamp_ns(row[2])
        if stamp < 0 or (previous is not None and stamp <= previous): raise ValueError("Native timestamps require strictly increasing nonnegative nanoseconds.")
        previous = stamp
        if kind != "camera":
            values = [float(v) for v in row[3:]]
            if not all(math.isfinite(v) for v in values): raise ValueError("Prepared sensor values must be finite.")
            if kind == "reference" and abs(math.sqrt(sum(v*v for v in values[3:])) - 1) > 1e-6:
                raise ValueError("Reference quaternions must be unit Hamilton xyzw.")
        result.append(row)
    if not result: raise ValueError("Prepared sensor stream is empty.")
    return result


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n"); writer.writerow(header); writer.writerows(rows)


def _timeline(spec, root, inventory, checkpoint):
    segment = spec["interval"]["segment_id"]
    first, last = (timestamp_ns(spec["interval"][k]) for k in ("first_ns", "last_ns"))
    cameras = []
    for camera in spec["cameras"]:
        rows = _csv(root, camera["index_path"], "camera", inventory, checkpoint)
        if any(row[1] != segment for row in rows): raise ValueError("Camera reset segment differs.")
        rows = [r for r in rows if first <= int(r[2]) <= last]
        if len(rows) < 2 or int(rows[0][2]) != first or int(rows[-1][2]) != last:
            raise ValueError("Interval endpoints must be exact camera sample epochs.")
        cameras.append(rows)
    if [r[2] for r in cameras[0]] != [r[2] for r in cameras[1]]:
        raise ValueError("Prepared stereo pairs are not exact.")
    imus = _csv(root, spec["imu"]["index_path"], "imu", inventory, checkpoint)
    if any(row[1] != segment for row in imus): raise ValueError("IMU reset segment differs.")
    before = [n for n,r in enumerate(imus) if int(r[2]) <= first]
    after = next((n for n,r in enumerate(imus) if int(r[2]) > last), None)
    if not before or after is None:
        raise ValueError("IMU must bracket every selected camera epoch through a strictly later sample.")
    return cameras, imus[before[-1]:after+1]


def inspect_private_timeline(spec, revision_root, *, checkpoint=None):
    """Read bounded hash-verified indices to establish exact schedule admission.

    This complements metadata assessment. It never reads camera pixels, stages
    files, launches numerical code or decides whether static initialization works.
    """
    specification_sha256(spec)
    root = Path(revision_root)
    checkpoint = checkpoint or (lambda: None)
    marker = _read(root/"publication-manifest.json", MAX_METADATA_BYTES, checkpoint=checkpoint)
    if hashlib.sha256(marker).hexdigest() != spec["input"]["publication_manifest_sha256"]:
        raise ValueError("Revision publication identity differs.")
    publication = strict_json(marker)
    if type(publication) is not dict or set(publication) != {"schema_version", "files"} or type(publication["schema_version"]) is not int or publication["schema_version"] != 1:
        raise ValueError("Unsupported revision publication.")
    cameras, imus = _timeline(spec, root, publication["files"], checkpoint)
    return {"camera_pairs":len(cameras[0]), "imu_samples":len(imus),
        "first_ns":cameras[0][0][2], "last_ns":cameras[0][-1][2]}


def prepare_private_inputs(spec, revision_root, output, *, checkpoint=None):
    """Verify the exact revision, stage small inputs, and return mount/evaluation data.

    The caller supplies an owned immutable revision and runs only after this
    function completes. Local source mutation during/after preparation is not an
    execution guarantee; hosted ownership and read-only mounts enforce that fence.
    """
    checkpoint = checkpoint or (lambda: None)
    digest = specification_sha256(spec)
    root, output = Path(revision_root), Path(output)
    if output.resolve().is_relative_to(root.resolve()): raise ValueError("Staging must be outside the immutable revision.")
    marker = _read(root / "publication-manifest.json", MAX_METADATA_BYTES, checkpoint=checkpoint)
    if hashlib.sha256(marker).hexdigest() != spec["input"]["publication_manifest_sha256"]:
        raise ValueError("Revision publication identity differs.")
    publication = strict_json(marker)
    if type(publication) is not dict or set(publication) != {"schema_version", "files"} or type(publication["schema_version"]) is not int or publication["schema_version"] != 1 or type(publication["files"]) is not dict:
        raise ValueError("Unsupported revision manifest.")
    inventory = publication["files"]
    if len(inventory) > 29999: raise ValueError("Revision file count exceeds the supported bound.")
    total = len(marker)
    for path, identity in inventory.items():
        _path(path)
        if type(identity) is not dict or set(identity) != {"size_bytes", "sha256"} or type(identity["size_bytes"]) is not int or identity["size_bytes"] < 0:
            raise ValueError("Invalid revision inventory.")
        total += identity["size_bytes"]
    if total > MAX_REVISION_BYTES: raise ValueError("Revision bytes exceed the supported bound.")
    def document(name):
        return loads_contract(_read(root/name, MAX_METADATA_BYTES, expected=inventory[name], checkpoint=checkpoint))
    prepared, source = document("prepared-input.json"), document("source-acquisition.json")
    calibration = document("evidence/calibration.json")
    mappings = [document(f"evidence/clock-{n:04d}.json") for n in range(len(prepared["clock_mappings"]))]
    expected_files = {f["path"]: {k:f[k] for k in ("size_bytes", "sha256")} for f in prepared["files"]}
    if set(inventory) != set(expected_files) | {"prepared-input.json", "source-acquisition.json", "preparation-report.json"} or any(inventory[name] != identity for name, identity in expected_files.items()):
        raise ValueError("Publication is not the exact prepared artifact closure.")
    binding = {key:spec["input"][key] for key in ("input_revision_id", "acquisition_id", "published_attempt", "acquisition_sha256", "prepared_input_sha256", "publication_manifest_sha256")}
    rebuilt = build_private_specification(binding, prepared, source, calibration, parameters=spec["parameters"], interval=spec["interval"], clock_mappings=mappings)
    if canonical(rebuilt) != canonical(spec): raise ValueError("Execution specification differs from retained input contracts.")
    for name, identity in inventory.items():
        _read(root/name, MAX_REVISION_BYTES, expected=identity, checkpoint=checkpoint, keep=False)
    selected_cameras, selected_imus = _timeline(spec, root, inventory, checkpoint)
    output.mkdir(parents=True, exist_ok=False)
    try:
        segment = spec["interval"]["segment_id"]
        first, last = (timestamp_ns(spec["interval"][k]) for k in ("first_ns", "last_ns"))
        camera_rows, mounts, indices = [], [], {}
        for camera, rows in zip(spec["cameras"], selected_cameras, strict=True):
            parent, image_rows = None, []
            for index, row in enumerate(rows):
                relative = str(PurePosixPath(camera["index_path"]).parent / _path(row[3]))
                image = inventory.get(relative)
                if image is None or image["size_bytes"] > MAX_IMAGE_BYTES or not relative.endswith(".png"):
                    raise ValueError("Image is missing or exceeds the supported encoded size.")
                current_parent = str(PurePosixPath(relative).parent)
                if parent is not None and current_parent != parent: raise ValueError("Camera images require a single directory.")
                parent = current_parent
                basename = PurePosixPath(relative).name
                if "/" in basename or "," in basename or "\n" in basename or not basename.endswith(".png"):
                    raise ValueError("Unsupported native image filename.")
                image_rows.append({"index": index, "timestamp_ns": row[2], "sample_id": row[0], "relative_path": relative, **image})
            # Every exposed regular file must be a declared sensor image.
            roles = {f["path"]: f["role"] for f in prepared["files"]}
            for entry in (root/parent).iterdir():
                name = str(PurePosixPath(parent)/entry.name)
                if entry.is_symlink() or not entry.is_file() or roles.get(name) != "sensor" or not name.endswith(".png"):
                    raise ValueError("Native image mount contains a non-sensor file.")
            slot = camera["slot"]
            # Nested read-only image binds need existing destinations because
            # Docker cannot create mountpoints inside the read-only CSV tree.
            (output/f"sensors/mav0/{slot}/data").mkdir(parents=True, exist_ok=False)
            _write_csv(output/f"sensors/mav0/{slot}/data.csv", ["#timestamp [ns]", "filename"],
                       ([row[2], PurePosixPath(row[3]).name] for row in rows))
            mounts.append({"slot": slot, "source_path": str((root/parent).absolute()), "target": f"/sensors/mav0/{slot}/data"})
            indices[slot] = image_rows
            camera_rows.append(rows)
        expected = [r[2] for r in camera_rows[0]]
        if expected != [r[2] for r in camera_rows[1]]: raise ValueError("Prepared stereo pairs are not exact.")
        imus = selected_imus
        imu_path = output/"sensors/mav0/imu0/data.csv"
        _write_csv(imu_path, IMU_HEADER, (row[2:] for row in imus))
        reference = spec["reference"]["stream"]
        references = []
        if reference is not None:
            references = _csv(root, reference["index_path"], "reference", inventory, checkpoint)
            if any(row[1] != reference["segment_id"] for row in references): raise ValueError("Reference reset segment differs.")
            # Keep one bracket on either side, never interpolate or change values.
            before = [n for n,r in enumerate(references) if int(r[2]) <= first]
            after = next((n for n,r in enumerate(references) if int(r[2]) >= last), len(references)-1)
            references = references[before[-1] if before else 0:after+1]
        reference_path = output/"evaluation/reference.csv"
        _write_csv(reference_path, POSE_HEADER, (row[2:] for row in references))
        config = output/"config"; config.mkdir()
        for name, payload in render_configuration(spec).items(): (config/name).write_text(payload, encoding="utf-8")
        image_index = {"schema_version": 2, "specification_sha256": digest,
            "input": deepcopy_binding(spec), "image": {"served_encoding": "original_png", "transformation": "none"},
            "camera_metadata": spec["cameras"], "cameras": indices}
        if len(canonical(image_index).encode()) > 2*1024**2: raise ValueError("Private image index exceeds its publication limit.")
        manifest = {"schema_version": 1, "profile_id": spec["profile_id"], "specification_sha256": digest,
            "input": deepcopy_binding(spec), "interval": spec["interval"], "camera_pairs": len(expected), "imu_samples": len(imus),
            "camera_timestamps_ns": expected, "reference_available": reference is not None,
            "reference_unavailable_reason": spec["reference"]["unavailable_reason"],
            "imu_sha256": hashlib.sha256(imu_path.read_bytes()).hexdigest(),
            "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            "configuration": spec["configuration"], "sensor_values": "prepared_unchanged_no_second_calibration"}
        (output/"input-manifest.json").write_text(canonical(manifest)+"\n")
        if sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) > MAX_STAGE_BYTES:
            raise ValueError("Private staging exceeds its scratch allocation.")
        checkpoint()
        return {"camera_timestamps_ns": expected, "camera_mounts": mounts,
            "evaluation": {"reference_available": reference is not None, "reason": spec["reference"]["unavailable_reason"],
                "reference_path": str(reference_path), "imu_path": str(imu_path)},
            "image_index": image_index, "input_manifest": manifest}
    except BaseException:
        shutil.rmtree(output)
        raise


def deepcopy_binding(spec):
    return json.loads(canonical(spec["input"]))
