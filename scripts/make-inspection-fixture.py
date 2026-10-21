#!/usr/bin/env python3
"""Build analytic stereo/IMU packages with known timing and reference gaps.

These are generated observations, never physical sensor evidence. The original
archive and every member have deterministic identities for browser acceptance.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile
import zlib


ORIGIN = 1_520_530_308_199_447_626


def png(frame):
    width, height = 64, 48
    def pixel(x, y):
        if frame == 0:
            return 0
        if frame == 1:
            return 255
        if frame == 2:
            return 127
        return (x * 7 + y * 11 + frame * 13) % 256
    raw = b"".join(b"\0" + bytes(pixel(x, y) for x in range(width)) for y in range(height))
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def csv_bytes(header, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode()


def build(output, *, no_reference=False, reset=False, foreign_clock=False):
    examples = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"
    descriptor = json.loads((examples / "stereo-partial-reference.json").read_text())
    descriptor["id"] = "synthetic-inspection" + ("-no-reference" if no_reference else "") + ("-reset" if reset else "") + ("-foreign-clock" if foreign_clock else "")
    descriptor["provenance"]["source"] = "Meridian analytic inspection fixture with declared gaps"
    descriptor["clocks"] = [{"id": "shared-clock", "unit": "ns", "epoch": "unix"}]
    if foreign_clock:
        descriptor["clocks"].append({"id": "independent-clock", "unit": "ns", "epoch": "device"})
    files, roles = {}, {}
    for stream in descriptor["streams"]:
        stream["clock_id"] = "independent-clock" if foreign_clock and stream["kind"] == "imu" else "shared-clock"
        kind = stream["kind"]
        if kind == "camera":
            stream["camera"]["resolution"] = [64, 48]
            ticks = [ORIGIN + i * 50_000_000 for i in range(20) if i not in (8, 9, 10)]
            rows = []
            for i, tick in enumerate(ticks):
                name = f"sensors/{stream['id']}/images/{i}.png"
                files[name], roles[name] = png(i), "sensor"
                rows.append([str(i), "segment0", str(tick), f"images/{i}.png"])
            header = ["sample_id", "segment_id", "timestamp", "filename"]
        elif kind == "imu":
            samples = [i for i in range(401) if not 256 <= i < 296]
            rows = [[str(index), "segment0", str(ORIGIN + i * 2_500_000),
                     str(i / 1000), str(-i / 2000), str(i / 4000),
                     str(8 if i == 120 else i / 400), str(-i / 800), str(-9.81 + i / 1600)]
                    for index, i in enumerate(samples)]
            # Use the source contract's exact supported CSV header.
            header = (examples / stream["index_path"]).read_text().splitlines()[0].split(",")
        else:
            rows = [[str(index), "segment0", str(ORIGIN + i * 10_000_000), "0", "0", "0", "0", "0", "0", "1"]
                    for index, i in enumerate(i for i in range(101) if not 30 <= i <= 60)]
            header = (examples / stream["index_path"]).read_text().splitlines()[0].split(",")
            stream["reference"]["coverage"] = "partial"
        stream["segments"] = [{"id": "segment0", "first_tick": rows[0][2], "last_tick": rows[-1][2], "sample_count": len(rows)}]
        if reset and kind == "imu":
            first_id = len(rows)
            extra = [[str(first_id + i), "reset1", str(i * 2_500_000), "0", "0", "0", "0", "0", "-9.81"] for i in range(41)]
            stream["segments"].append({"id": "reset1", "first_tick": extra[0][2], "last_tick": extra[-1][2], "sample_count": len(extra)})
            rows.extend(extra)
        files[stream["index_path"]] = csv_bytes(header, rows)
        roles[stream["index_path"]] = "reference" if kind == "reference_pose" else "sensor"
    if no_reference:
        for stream in descriptor["streams"]:
            if stream["kind"] == "reference_pose":
                files.pop(stream["index_path"])
                roles.pop(stream["index_path"])
        descriptor["streams"] = [s for s in descriptor["streams"] if s["kind"] != "reference_pose"]
    descriptor["files"] = [{"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "role": roles[name]}
                           for name, data in sorted(files.items())]
    files["acquisition.json"] = (json.dumps(descriptor, sort_keys=True, indent=2) + "\n").encode()
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            member.external_attr = 0o100600 << 16
            archive.writestr(member, data)
    return {"synthetic": True, "size_bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "camera_samples_per_stream": 17, "imu_samples": 402 if reset else 361,
            "reference_samples": 0 if no_reference else 70,
            "reference_supported_camera_samples": 0 if no_reference else 13,
            "reference_max_bracket_ns": "20000000",
            "reset": reset, "foreign_clock": foreign_clock,
            "limitations": "Analytic measurements and grayscale patterns; no physical calibration, saturation range or estimator output."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--no-reference", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--foreign-clock", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.output, no_reference=args.no_reference, reset=args.reset, foreign_clock=args.foreign_clock), indent=2))


if __name__ == "__main__":
    main()
