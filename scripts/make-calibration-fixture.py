#!/usr/bin/env python3
"""Generate analytic supplied-calibration packages and independent expected rows.

All observations and validation claims are synthetic. Distinct descriptor/session
identities exercise ownership and lineage; they are not independent physical
calibration or evaluation sessions. No network, fitting or estimator is used.
"""
import argparse
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile


EPOCH = 1_520_530_308_199_447_626
IMU_ROWS = [(1, 2, 3, 4, 5, 6), (-1, 0, 2, 1, 2, 3), (0, 1, -1, -2, 4, 0)]
IMU_TICKS = [EPOCH - 5_000_000, EPOCH + 25_000_000, EPOCH + 55_000_000]
HEADER = ["sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz"]


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def identity(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def reference(document):
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"id": document["id"], "sha256": hashlib.sha256(canonical).hexdigest()}


def csv_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return output.getvalue().encode()


def expected(calibration):
    # A positive quarter turn about source Z means target coordinates (-y,x,z).
    # This calculation is independent of Meridian's preparation/rotation helpers.
    result = []
    info = calibration["imu"]
    for index, values in enumerate(IMU_ROWS):
        gyro = [(values[i] - info["gyro_bias_rad_s"][i]) * info["gyro_scale"][i] for i in range(3)]
        force = [(values[3 + i] - info["accel_bias_m_s2"][i]) * info["accel_scale"][i] for i in range(3)]
        rotated = [-gyro[1], gyro[0], gyro[2], -force[1], force[0], force[2]]
        result.append([str(index), "segment0", str(IMU_TICKS[index] + 2)] +
                      [repr(float(value)) if value != 0 else "0.0" for value in rotated])
    return {"rows": result, "imu_csv": identity(csv_bytes(result)), "output_clock_id": "camera-clock",
            "output_frame_id": "imu-corrected", "rotation": "positive quarter turn about source Z"}


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    examples = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"
    template = json.loads((examples / "stereo-partial-reference.json").read_bytes())
    template["rig"] = {"id": "analytic-calibration-rig", "revision": "1"}
    template["frames"].append({"id": "imu-corrected", "axes": ["left", "forward", "down"]})
    template["provenance"].update(source="Meridian analytic supplied-calibration fixture",
        attribution="Generated analytic measurements; no physical validation.")
    files = {item["path"]: (examples / item["path"]).read_bytes() for item in template["files"]}
    files["sensors/imu0/data.csv"] = csv_bytes([[str(i), "segment0", str(IMU_TICKS[i]), *row]
                                               for i, row in enumerate(IMU_ROWS)])
    report = encoded({"schema_version": 1, "synthetic": True,
        "claim": "Analytic fixture report supplied by the caller; physical independence and accuracy are not verified."})
    files["evidence/analytic-validation.json"] = report
    roles = {item["path"]: item["role"] for item in template["files"]}
    roles["evidence/analytic-validation.json"] = "evidence"
    template["files"] = [{"path": name, **identity(data), "role": roles[name]} for name, data in sorted(files.items())]
    acquisitions, packages = {}, {}
    for name in ("source", "replay", "validation"):
        acquisition = deepcopy(template)
        acquisition["id"] = "analytic-calibration-" + name
        acquisition["provenance"]["session_id"] = "synthetic-" + name
        acquisitions[name] = acquisition
        members = {**files, "acquisition.json": encoded(acquisition)}
        path = output / (name + ".zip")
        with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
            for member, data in sorted(members.items()):
                info = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100600 << 16
                archive.writestr(info, data)
        (output / (name + "-acquisition.json")).write_bytes(encoded(acquisition))
        packages[name] = {"file": path.name, **identity(path.read_bytes()), "acquisition": reference(acquisition)}
    calibration = json.loads((examples / "calibration.json").read_bytes())
    calibration.update(id="analytic-supplied-calibration", acquisition=reference(acquisitions["source"]))
    calibration["method"] = {"name": "supplied-analytic-parameters", "version": "1"}
    calibration["imu"] = {"sensor_id": "imu0", "frame_id": "imu",
        "gyro_bias_rad_s": [.1, .2, .3], "accel_bias_m_s2": [1, 2, 3],
        "gyro_scale": [2, 3, 4], "accel_scale": [.5, 2, 1],
        "noise": {"model": "continuous_white_noise_and_bias_diffusion",
            "gyro_density_rad_s_per_sqrt_hz": .01, "accel_density_m_s2_per_sqrt_hz": .02,
            "gyro_bias_diffusion_rad_s_per_sqrt_s": .001, "accel_bias_diffusion_m_s2_per_sqrt_s": .002,
            "assumptions": "Supplied corrected-frame illustrative isotropic model; not fitted, propagated or consumed."}}
    calibration["transforms"].append({"source_frame": "imu", "target_frame": "imu-corrected",
        "rotation": [[0, -1, 0], [1, 0, 0], [0, 0, 1]], "translation_m": [0, 0, 0]})
    calibration["applicability"] = {"description": "Synthetic constant SI bias and positive diagonal gains, before one explicit frame rotation.",
                                     "imu_intrinsics": "candidate_not_applied"}
    calibration["validation"] = {"status": "reported", "acquisitions": [reference(acquisitions["validation"])],
                                  "report": {"path": "evidence/analytic-validation.json", **identity(report)}}
    alternative = deepcopy(calibration)
    alternative["id"] = "analytic-supplied-calibration-alternative"
    alternative["imu"]["gyro_bias_rad_s"][0] = .6
    clock = {"format": "meridian-sensor-contract", "schema_version": 1, "kind": "clock_mapping",
        "id": "analytic-domain-plus2ns", "acquisition": reference(acquisitions["replay"]),
        "source_stream_id": "imu0", "source_segment_id": "segment0", "target_clock_id": "camera-clock",
        "purpose": "clock_domain_mapping", "model": "constant_offset_ns", "source_origin_ns": str(EPOCH),
        "target_origin_ns": str(EPOCH), "offset_ns": "2", "first_source_ns": str(IMU_TICKS[0]),
        "last_source_ns": str(IMU_TICKS[-1]), "evidence": {"status": "declared", "description": "Analytic +2ns after exact ns conversion; no physical clock claim.", "report": None}}
    offset = deepcopy(clock)
    offset.update(id="analytic-sensor-minus2ns", target_clock_id="imu-clock", purpose="sensor_time_offset", offset_ns="-2")
    documents = {"calibration.json": calibration, "calibration-alternative.json": alternative,
                 "clock-domain.json": clock, "sensor-offset.json": offset}
    for name, change in (("invalid-rotation.json", "rotation"), ("invalid-resolution.json", "resolution"),
                         ("invalid-noise-unit.json", "noise")):
        value = deepcopy(calibration)
        if change == "rotation": value["transforms"][-1]["rotation"][0][1] = 1
        if change == "resolution": value["cameras"][0]["resolution"][0] += 1
        if change == "noise": value["imu"]["noise"]["gyro_variance"] = .01
        documents[name] = value
    for name, document in documents.items():
        (output / name).write_bytes(encoded(document))
    (output / "invalid-duplicate-key.json").write_bytes(b'{"id":"duplicate",' + encoded(calibration).lstrip()[1:])
    manifest = {"schema_version": 1, "synthetic": True, "packages": packages,
        "artifacts": {name: {**identity((output / name).read_bytes()), "contract": reference(document)}
                      for name, document in documents.items() if not name.startswith("invalid-")},
        "selection": {"calibration_file": "calibration.json", "clock_mapping_files": ["clock-domain.json"],
            "dependency_acquisition_files": ["source-acquisition.json", "validation-acquisition.json"],
            "apply_imu_intrinsics": True, "imu_target_frame": "imu-corrected", "imu_stream": "imu0"},
        "expected_revision_a": expected(calibration), "expected_revision_b": expected(alternative),
        "sensor_offset_only_expected_ns": [str(t - 2) for t in IMU_TICKS],
        "limitations": "Synthetic observations and reported-validation metadata only; no fitting, physical independence, estimator or flight result."}
    (output / "manifest.json").write_bytes(encoded(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))


if __name__ == "__main__":
    main()
