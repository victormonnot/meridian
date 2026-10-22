#!/usr/bin/env python3
"""Package the predeclared first 30 seconds of the consumed room1 regression.

Requires an existing Meridian TUM-VI import of the pinned room1 archive. No
download, calibration fit or new evaluation recording is used. The supplied
calibration comes from the frozen OpenVINS reference configuration; this crop is
not its fitting session and does not establish independent calibration accuracy.
"""
import argparse
import ast
import bisect
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import zipfile

from meridian.sensor_contract import contract_sha256, validate_contract
from meridian.vio_specification import build_specification, DATASET_ID


DURATION_NS = 30_000_000_000


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def identity(path):
    with path.open("rb") as stream:
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                "size_bytes": path.stat().st_size}


def reference(document):
    return {"id": document["id"], "sha256": contract_sha256(document)}


def csv_bytes(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode()


def supplied_calibration(acquisition, configuration):
    # Only parse these exact pinned files, never user-provided YAML.
    baseline = build_specification(DATASET_ID, {"num_pts": 200})
    paths = [configuration / name for name in ("kalibr_imucam_chain.yaml", "kalibr_imu_chain.yaml")]
    for path in paths:
        if identity(path) != baseline["configuration"]["baseline_files"][path.name]:
            raise ValueError("The fixture requires the frozen reference calibration files.")
    cameras, transforms = [], []
    text = paths[0].read_text()
    for name, other in (("cam0", "cam1"), ("cam1", None)):
        block = text.split(name + ":", 1)[1]
        if other:
            block = block.split(other + ":", 1)[0]
        fields = {}
        matrix = []
        for line in block.splitlines():
            value = line.split("#", 1)[0].strip()
            if value.startswith("- ["):
                matrix.append(ast.literal_eval(value[2:]))
            elif ":" in value:
                key, value = value.split(":", 1)
                fields[key.strip()] = value.strip()
        cameras.append({"sensor_id": name, "frame_id": name,
                        "resolution": ast.literal_eval(fields["resolution"]),
                        "projection": fields["camera_model"], "distortion": fields["distortion_model"],
                        "intrinsics": ast.literal_eval(fields["intrinsics"]),
                        "coefficients": ast.literal_eval(fields["distortion_coeffs"])})
        if len(matrix) != 4 or matrix[3] != [0, 0, 0, 1]:
            raise ValueError("Unexpected frozen transform layout.")
        transforms.append({"source_frame": "imu", "target_frame": name,
                           "rotation": [row[:3] for row in matrix[:3]],
                           "translation_m": [row[3] for row in matrix[:3]]})
    values = {}
    for line in paths[1].read_text().splitlines():
        parts = line.split("#", 1)[0].strip().split(":", 1)
        if len(parts) == 2 and parts[0] in {"gyroscope_noise_density", "accelerometer_noise_density",
                                          "gyroscope_random_walk", "accelerometer_random_walk"}:
            values[parts[0]] = float(parts[1])
    calibration = {
        "format": "meridian-sensor-contract", "schema_version": 1, "kind": "calibration",
        "id": "room1-supplied-regression-calibration", "acquisition": reference(acquisition),
        "state": "supplied", "method": {"name": "frozen-openvins-tumvi-configuration", "version": "1"},
        "cameras": cameras, "transforms": transforms,
        "imu": {"sensor_id": "imu0", "frame_id": "imu", "gyro_bias_rad_s": [0, 0, 0],
                "accel_bias_m_s2": [0, 0, 0], "gyro_scale": [1, 1, 1], "accel_scale": [1, 1, 1],
                "noise": {"model": "continuous_white_noise_and_bias_diffusion",
                    "gyro_density_rad_s_per_sqrt_hz": values["gyroscope_noise_density"],
                    "accel_density_m_s2_per_sqrt_hz": values["accelerometer_noise_density"],
                    "gyro_bias_diffusion_rad_s_per_sqrt_s": values["gyroscope_random_walk"],
                    "accel_bias_diffusion_m_s2_per_sqrt_s": values["accelerometer_random_walk"],
                    "assumptions": "Isotropic continuous parameters from the frozen OpenVINS TUM-VI configuration; not fitted on this crop or identified by Meridian."}},
        "applicability": {"imu_intrinsics": "already_applied",
            "description": "Publisher calibrated IMU values are preserved. Identity residual bias/scale are not reapplied. The bound replay acquisition carries the supplied parameters; it is not their fitting session."},
        "validation": {"status": "not_provided", "acquisitions": [], "report": None},
    }
    validate_contract(calibration)
    return calibration, {path.name: identity(path) for path in paths}


def build(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output == source or source in output.parents:
        raise ValueError("Choose a new output directory outside the source acquisition.")
    acquisition = json.loads((source / "acquisition.json").read_text())
    validate_contract(acquisition)
    evidence = json.loads((source / "evidence/tumvi-source.json").read_text())
    baseline = build_specification(DATASET_ID, {"num_pts": 200})
    if (acquisition["provenance"]["session_id"] != "tumvi-room1"
            or acquisition["provenance"]["synthetic"] is not False
            or evidence["sequence"] != "room1"
            or evidence["archive_identity"]["sha256"] != baseline["dataset"]["archive_sha256"]
            or identity(source / "originals/source.tar") != evidence["archive_identity"]):
        raise ValueError("The fixture requires the previously imported pinned room1 archive.")
    inventory = {item["path"]: item for item in acquisition["files"]}
    tables = {}
    for stream in acquisition["streams"]:
        path = source / stream["index_path"]
        expected = inventory[stream["index_path"]]
        if identity(path) != {key: expected[key] for key in ("sha256", "size_bytes")}:
            raise ValueError("Source CSV identity changed.")
        with path.open(newline="") as handle:
            rows = list(csv.reader(handle))
        tables[stream["id"]] = rows
    start = int(tables["cam0"][1][2])
    end = start + DURATION_NS
    cameras = [row for row in tables["cam0"][1:] if start <= int(row[2]) < end]
    last = int(cameras[-1][2])
    selections = {}
    for stream in acquisition["streams"]:
        rows = tables[stream["id"]][1:]
        if stream["kind"] == "camera":
            selected = [row for row in rows if start <= int(row[2]) < end]
        else:
            ticks = [int(row[2]) for row in rows]
            first_index = max(0, bisect.bisect_right(ticks, start) - 1)
            stop = min(len(rows), bisect.bisect_right(ticks, last) + 1)
            selected = rows[first_index:stop]
        selections[stream["id"]] = selected
    if [row[2] for row in selections["cam1"]] != [row[2] for row in cameras]:
        raise ValueError("The crop must retain exact stereo pairs.")
    output.mkdir(parents=True, exist_ok=False)
    package = output / "package"
    package.mkdir()
    derived = deepcopy(acquisition)
    derived["id"] = "tumvi-room1-first30s-private-vio-regression"
    derived["provenance"]["source"] += "; fixed first-30-second crop, see evidence/private-vio-crop.json"
    for stream in derived["streams"]:
        rows = selections[stream["id"]]
        path = package / stream["index_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(csv_bytes(tables[stream["id"]][0], rows))
        stream["segments"] = [{"id": "segment0", "first_tick": rows[0][2],
                               "last_tick": rows[-1][2], "sample_count": len(rows)}]
        if stream["kind"] == "camera":
            for row in rows:
                relative = str(Path(stream["index_path"]).parent / row[3])
                original, target = source / relative, package / relative
                expected = inventory[relative]
                if original.is_symlink() or identity(original) != {key: expected[key] for key in ("sha256", "size_bytes")}:
                    raise ValueError("Source image identity changed.")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
    crop = {"schema_version": 1, "source_acquisition": reference(acquisition),
            "source_archive": evidence["archive_identity"], "recording_group": "tumvi-room1",
            "selection": "First 30 seconds from first camera, half-open; adjacent IMU/reference samples bracket retained camera epochs.",
            "start_ns": str(start), "end_exclusive_ns": str(end), "last_camera_ns": str(last),
            "source_sample_ids_preserved": True, "image_bytes_preserved": True,
            "purpose": "Consumed real recording regression; not a held-out or independent physical validation.",
            "license": acquisition["provenance"]["license"], "attribution": acquisition["provenance"]["attribution"]}
    (package / "evidence").mkdir()
    (package / "evidence/private-vio-crop.json").write_bytes(encoded(crop))
    derived["files"] = [{"path": p.relative_to(package).as_posix(), **identity(p),
                          "role": "reference" if p.relative_to(package).parts[0] == "references" else
                                  "evidence" if p.relative_to(package).parts[0] == "evidence" else "sensor"}
                         for p in sorted(package.rglob("*")) if p.is_file()]
    validate_contract(derived)
    (package / "acquisition.json").write_bytes(encoded(derived))
    (output / "acquisition.json").write_bytes(encoded(derived))
    configuration = Path(__file__).resolve().parents[1] / "integrations/openvins/config"
    calibration, pins = supplied_calibration(derived, configuration)
    (output / "calibration.json").write_bytes(encoded(calibration))
    with zipfile.ZipFile(output / "room1-first30s.zip", "x", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(package).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100600 << 16
                info.file_size = path.stat().st_size
                with archive.open(info, "w") as target, path.open("rb") as original:
                    shutil.copyfileobj(original, target, length=1024 * 1024)
    manifest = {"schema_version": 1, "synthetic": False, "recording_group": "tumvi-room1",
                "package": {"file": "room1-first30s.zip", **identity(output / "room1-first30s.zip"), "acquisition": reference(derived)},
                "calibration": {"file": "calibration.json", **identity(output / "calibration.json"), "contract": reference(calibration)},
                "configuration_sources": pins, "interval": crop,
                "counts": {name: len(rows) for name, rows in selections.items()},
                "preparation": {"apply_imu_intrinsics": False, "imu_target_frame": None, "imu_stream": "imu0",
                                "clock_mappings": [], "image_encoding": "preserve"},
                "reference_variants": ["included", "excluded_by_preparation"],
                "limitations": "Same consumed recording, supplied parameters and fixed predeclared crop; no fitting, untouched evaluation, hardware or flight qualification."}
    (output / "manifest.json").write_bytes(encoded(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing imported room1 acquisition directory")
    parser.add_argument("output", type=Path, help="New output directory")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))


if __name__ == "__main__":
    main()
