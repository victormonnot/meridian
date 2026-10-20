"""Generate tiny, deterministic contract fixtures; never import a recording.

Run with a new output directory. These synthetic images and measurements check
metadata conventions, not calibration observability or estimator performance.
"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import zlib


EPOCH = 1_520_530_308_199_447_626


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def reference(value):
    return {"id": value["id"], "sha256": digest(value)}


def envelope(kind, identifier):
    return {"format": "meridian-sensor-contract", "schema_version": 1,
            "kind": kind, "id": identifier}


def identity(root, path, role=None):
    content = (root / path).read_bytes()
    record = {"path": path, "sha256": hashlib.sha256(content).hexdigest(),
              "size_bytes": len(content)}
    if role is not None:
        record["role"] = role
    return record


def write(root, path, content):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def image_bytes():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))
    # One unfiltered row per scanline, eight-bit grayscale, 8 by 6 pixels.
    pixels = b"".join(b"\0" + bytes((x * 17 + y * 11) % 256 for x in range(8))
                      for y in range(6))
    # Explicit uncompressed DEFLATE block: stable across zlib compressor versions.
    compressed = (b"\x78\x01\x01" + struct.pack("<HH", len(pixels), len(pixels) ^ 0xffff)
                  + pixels + struct.pack(">I", zlib.adler32(pixels)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 6, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


def processing():
    return {"calibration": "not_applied", "filtering": "none",
            "clock_correction": "not_applied",
            "description": "Analytic synthetic fixture; no physical acquisition."}


def segment(first, last, count, identifier="segment0"):
    return {"id": identifier, "first_tick": str(first), "last_tick": str(last),
            "sample_count": count}


def generate(root):
    root.mkdir(parents=True, exist_ok=False)
    files = []
    camera_rows = "sample_id,segment_id,timestamp,filename\n"
    for index, tick in enumerate((EPOCH, EPOCH + 50_000_000)):
        camera_rows += f"{index},segment0,{tick},images/{index}.png\n"
    for camera in ("cam0", "cam1"):
        index_path = f"sensors/{camera}/data.csv"
        write(root, index_path, camera_rows.encode("ascii"))
        files.append(identity(root, index_path, "sensor"))
        for index in (0, 1):
            path = f"sensors/{camera}/images/{index}.png"
            write(root, path, image_bytes())
            files.append(identity(root, path, "sensor"))
    imu_header = "sample_id,segment_id,timestamp,wx,wy,wz,fx,fy,fz\n"
    imu_ticks = (EPOCH - 5_000_000, EPOCH + 25_000_000, EPOCH + 55_000_000)
    imu_rows = imu_header + "".join(
        f"{i},segment0,{tick},0,0,0,0,0,-9.81\n" for i, tick in enumerate(imu_ticks))
    write(root, "sensors/imu0/data.csv", imu_rows.encode("ascii"))
    files.append(identity(root, "sensors/imu0/data.csv", "sensor"))
    frames = [
        {"id": "imu", "axes": ["forward", "right", "down"]},
        {"id": "cam0", "axes": ["right", "down", "forward"]},
        {"id": "cam1", "axes": ["right", "down", "forward"]},
        {"id": "world", "axes": ["east", "north", "up"]},
    ]
    streams = []
    for camera in ("cam0", "cam1"):
        streams.append({
            "id": camera, "kind": "camera", "sensor_instance": camera,
            "frame_id": camera, "clock_id": "camera-clock",
            "timestamp_semantics": "exposure_midpoint", "measurement_semantics": "image",
            "index_path": f"sensors/{camera}/data.csv",
            "segments": [segment(EPOCH, EPOCH + 50_000_000, 2)],
            "processing": processing(),
            "camera": {"resolution": [8, 6], "encoding": "mono8", "shutter": "global"},
        })
    streams.append({
        "id": "imu0", "kind": "imu", "sensor_instance": "imu0",
        "frame_id": "imu", "clock_id": "imu-clock",
        "timestamp_semantics": "sample", "measurement_semantics": "instantaneous_imu",
        "index_path": "sensors/imu0/data.csv",
        "segments": [segment(imu_ticks[0], imu_ticks[-1], 3)],
        "processing": processing(),
        "imu": {"angular_velocity_unit": "rad/s", "specific_force_unit": "m/s^2",
                "local_gravity_m_s2": None},
    })
    acquisition = {
        **envelope("acquisition", "synthetic-stereo"),
        "purpose": "fixture",
        "rig": {"id": "synthetic-rig", "revision": "1"},
        "provenance": {"source": "generate.py analytic fixture", "license": "unspecified",
                       "attribution": "Meridian synthetic contract fixtures",
                       "session_id": "synthetic-session", "synthetic": True},
        "frames": frames, "clocks": [
            {"id": "camera-clock", "unit": "ns", "epoch": "unix"},
            {"id": "imu-clock", "unit": "ns", "epoch": "device"},
        ],
        "files": files, "streams": streams,
        "stereo_pairing": {"camera_ids": ["cam0", "cam1"], "policy": "exact_timestamp_pairs"},
    }

    partial = deepcopy(acquisition)
    partial["id"] = "synthetic-partial-reference"
    pose_path = "references/pose0/data.csv"
    write(root, pose_path, (
        "sample_id,segment_id,timestamp,px,py,pz,qx,qy,qz,qw\n"
        f"0,reference0,{EPOCH},0,0,0,1,0,0,0\n"
    ).encode("ascii"))
    partial["files"].append(identity(root, pose_path, "reference"))
    partial["streams"].append({
        "id": "pose0", "kind": "reference_pose", "sensor_instance": "synthetic-truth",
        "frame_id": "imu", "clock_id": "camera-clock",
        "timestamp_semantics": "sample", "measurement_semantics": "pose",
        "index_path": pose_path, "segments": [segment(EPOCH, EPOCH, 1, "reference0")],
        "processing": processing(),
        "reference": {"parent_frame_id": "world", "position_unit": "m",
                      "orientation": "Hamilton_xyzw_frame_to_parent",
                      "use": "independent_evaluation_only", "coverage": "partial",
                      "position_uncertainty_m": 0.0, "orientation_uncertainty_rad": 0.0},
    })

    snapshots = deepcopy(acquisition)
    snapshots["id"] = "synthetic-imu-snapshots"
    snapshots["frames"] = [frames[0]]
    snapshots["clocks"] = [{"id": "imu-clock", "unit": "us", "epoch": "boot"}]
    snapshots["stereo_pairing"] = None
    snapshots["streams"] = [deepcopy(streams[-1])]
    stream = snapshots["streams"][0]
    stream["timestamp_semantics"] = "message_write"
    stream["measurement_semantics"] = "filtered_snapshot"
    stream["index_path"] = "sensors/snapshots/data.csv"
    # A reset is explicit; file order is retained although ticks decrease.
    stream["segments"] = [segment(1_000_000, 1_040_000, 2),
                          segment(100, 40_100, 2, "segment1")]
    stream["processing"] = {
        "calibration": "applied", "filtering": "applied", "clock_correction": "unknown",
        "description": "Synthetic representation of processed logger snapshots; not a DataFlash acquisition.",
    }
    snapshot_rows = (imu_header + "0,segment0,1000000,0.01,0,0,0,0,-9.81\n"
                     "1,segment0,1040000,0.01,0,0,0,0,-9.81\n"
                     "2,segment1,100,0.01,0,0,0,0,-9.81\n"
                     "3,segment1,40100,0.01,0,0,0,0,-9.81\n")
    write(root, stream["index_path"], snapshot_rows.encode("ascii"))
    snapshots["files"] = [identity(root, stream["index_path"], "sensor")]

    unknown = deepcopy(acquisition)
    unknown["id"] = "synthetic-unknown-metadata"
    unknown["frames"][0]["axes"] = None
    unknown["streams"][-1]["timestamp_semantics"] = "unknown"
    unknown["streams"][-1]["processing"]["calibration"] = "unknown"

    calibration = {
        **envelope("calibration", "synthetic-calibration"),
        "acquisition": reference(acquisition), "state": "supplied",
        "method": {"name": "analytic-fixture", "version": "1"},
        "cameras": [
            {"sensor_id": c, "frame_id": c, "resolution": [8, 6], "projection": "pinhole",
             "distortion": "radtan", "intrinsics": [4.0, 4.0, 3.5, 2.5],
             "coefficients": [0.0, 0.0, 0.0, 0.0]} for c in ("cam0", "cam1")
        ],
        "imu": None,
        "transforms": [
            {"source_frame": "imu", "target_frame": "cam0",
             "rotation": [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
             "translation_m": [0.0, 0.0, 0.0]},
            {"source_frame": "imu", "target_frame": "cam1",
             "rotation": [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
             "translation_m": [-0.1, 0.0, 0.0]},
        ],
        "applicability": {"description": "Synthetic convention example, not a fitted calibration.",
                          "imu_intrinsics": "not_applicable"},
        "validation": {"status": "not_provided", "acquisitions": [], "report": None},
    }
    replay = deepcopy(acquisition)
    replay["id"] = "synthetic-replay"
    replay["purpose"] = "replay"
    replay["provenance"]["session_id"] = "synthetic-replay-session"
    clock = {
        **envelope("clock_mapping", "synthetic-clock-offset"),
        "acquisition": reference(replay), "source_stream_id": "imu0",
        "source_segment_id": "segment0", "target_clock_id": "camera-clock",
        "purpose": "clock_domain_mapping", "model": "constant_offset_ns",
        "source_origin_ns": str(EPOCH), "target_origin_ns": str(EPOCH),
        "offset_ns": "2", "first_source_ns": str(imu_ticks[0]),
        "last_source_ns": str(imu_ticks[-1]),
        "evidence": {"status": "declared", "description": "Analytic +2 ns example only.",
                     "report": None},
    }
    prepared_path = "sensors/prepared-imu0/data.csv"
    write(root, prepared_path, (imu_header + "".join(
        f"{i},segment0,{tick + 2},0,0,0,0,0,-9.81\n"
        for i, tick in enumerate(imu_ticks))).encode("ascii"))
    prepared = {
        **envelope("prepared_input", "synthetic-prepared"),
        "acquisition": reference(replay), "calibration": reference(calibration),
        "clock_mappings": [reference(clock)],
        "method": {"name": "analytic-fixture-generator", "version": "1"},
        "selections": [{"stream_id": s["id"], "segment_id": "segment0",
                        "first_tick": s["segments"][0]["first_tick"],
                        "last_tick": s["segments"][0]["last_tick"]} for s in streams],
        "operations": [
            {"kind": "clock_mapping", "stream_id": "imu0", "artifact": reference(clock),
             "description": "Add exactly 2 ns while preserving row identity and measurement values.",
             "parameters": {}},
        ],
        "outputs": [
            {"stream_id": "cam0", "index_path": "sensors/cam0/data.csv",
             "frame_id": "cam0", "clock_id": "camera-clock", "timestamp_unit": "ns"},
            {"stream_id": "cam1", "index_path": "sensors/cam1/data.csv",
             "frame_id": "cam1", "clock_id": "camera-clock", "timestamp_unit": "ns"},
            {"stream_id": "imu0", "index_path": prepared_path,
             "frame_id": "imu", "clock_id": "camera-clock", "timestamp_unit": "ns"},
        ],
        "files": [f for f in files if f["path"] != "sensors/imu0/data.csv"]
                 + [identity(root, prepared_path, "sensor")],
        "target_profile": "stereo_imu_candidate_v1",
    }
    documents = {
        "stereo-no-reference.json": acquisition,
        "stereo-partial-reference.json": partial,
        "imu-snapshots.json": snapshots,
        "unknown-metadata.json": unknown,
        "replay-acquisition.json": replay,
        "calibration.json": calibration,
        "clock-mapping.json": clock,
        "prepared-input.json": prepared,
    }
    for name, document in documents.items():
        write(root, name, (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("ascii"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
