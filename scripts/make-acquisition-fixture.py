#!/usr/bin/env python3
"""Build a deterministic synthetic package for the hosted upload acceptance.

The uncompressed padding crosses the 4 MiB transfer boundary. It represents
generated storage-test bytes, not an additional sensor observation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output, *, corrupt=False, snapshots=False):
    examples = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"
    document = json.loads((examples / ("imu-snapshots.json" if snapshots else
                                       "stereo-no-reference.json")).read_text())
    files = {item["path"]: (examples / item["path"]).read_bytes() for item in document["files"]}
    padding_name = "originals/synthetic-transfer-padding.bin"
    padding = bytes(range(256)) * (5 * 1024 * 1024 // 256)
    files[padding_name] = padding
    document["files"].append({"path": padding_name, "size_bytes": len(padding),
                              "sha256": hashlib.sha256(padding).hexdigest(), "role": "original"})
    document["provenance"]["source"] = "Meridian analytic fixtures and synthetic transfer padding"
    files["acquisition.json"] = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if corrupt:
        name = document["streams"][0]["index_path"]
        files[name] += b"corrupt row deliberately absent from the descriptor identity\n"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            member.external_attr = 0o100600 << 16
            member.compress_type = zipfile.ZIP_STORED
            archive.writestr(member, data)
    digest = hashlib.sha256()
    with output.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"synthetic": True, "deliberately_corrupt": corrupt,
            "size_bytes": output.stat().st_size, "sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New ZIP path; parent must exist.")
    parser.add_argument("--corrupt", action="store_true", help="Create a deliberate payload/hash mismatch.")
    parser.add_argument("--snapshots", action="store_true", help="Use the IMU-only snapshot example.")
    args = parser.parse_args()
    print(json.dumps(build(args.output, corrupt=args.corrupt, snapshots=args.snapshots), indent=2))


if __name__ == "__main__":
    main()
