"""Local sensor import and deterministic preparation, without estimation.

The acquisition remains the immutable source declaration. Preparation records
SI and integer-nanosecond conversions separately, preserves row identities and
keeps evaluation references outside the sensor tree. Supplied correction plans
create a separate method-v2 revision; unselected preparation retains method v1.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import json
from pathlib import Path
import re
import shutil
import struct
import sys
import zlib

from .sensor_contract import contract_sha256, loads_contract, validate_bundle
from .sensor_calibration import (
    apply_clock, apply_imu, selected_streams, validate_application_request,
)
from .sensor_conventions import accel_to_m_s2, gyro_to_rad_s, timestamp_to_ns
from .sensor_package import (
    DEFAULT_LIMITS, copy_regular, file_identity, iter_stream_rows,
    materialize_package, validate_package,
)


METHOD = {"name": "meridian-sensor-preparation", "version": "1"}
HEADERS = {
    "camera": ["sample_id", "segment_id", "timestamp", "filename"],
    "imu": ["sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz"],
    "reference_pose": ["sample_id", "segment_id", "timestamp", "px", "py", "pz",
                       "qx", "qy", "qz", "qw"],
}


def _json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        handle.write("\n")


def _identity(document):
    return {"id": document["id"], "sha256": contract_sha256(document)}


def _copy(source, target, expected=None, *, limits=DEFAULT_LIMITS):
    target.parent.mkdir(parents=True, exist_ok=True)
    actual = copy_regular(source, target, limits=limits)
    if expected is not None and any(actual[key] != expected[key] for key in actual):
        raise ValueError("Source identity changed during preparation.")
    return actual


def _check_destination(source, output):
    if source.is_dir() and output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output must be outside the source recording.")


def import_package(source: Path, output: Path, *, limits=DEFAULT_LIMITS) -> dict:
    """Import into a new result directory, retaining a transfer archive if used.

    ``output/acquisition`` is an exact, validated package. The outer report and
    optional original transfer container do not pollute its exact inventory.
    No successful result is overwritten, and failed new outputs are removed.
    """
    source, output = Path(source), Path(output)
    _check_destination(source, output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        transfer = None
        if source.is_file() and not source.is_symlink():
            # Read and preserve the same bytes that are subsequently decoded.
            if source.stat().st_size > limits.max_file_bytes:
                raise ValueError("Transfer container exceeds the file-size limit.")
            original = output / "original-transfer"
            transfer = {"path": original.name, **_copy(source, original, limits=limits)}
            source = original
        result = materialize_package(source, output / "acquisition", limits=limits)
        report = {
            "format": "meridian-sensor-import-report", "schema_version": 1,
            "status": "accepted", "acquisition": _identity(result["acquisition"]),
            "validation": result["report"], "original_transfer": transfer,
            "execution_readiness": "not_assessed",
        }
        _json(output / "import-report.json", report)
        return report
    except BaseException:
        shutil.rmtree(output)
        raise


def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def _write_mono8_png(path, pixels):
    """Write deterministic PNG with explicit stored DEFLATE blocks and filter 0."""
    height, width = pixels.shape
    scanlines = b"".join(b"\0" + row.tobytes() for row in pixels)
    compressed = bytearray(b"\x78\x01")
    for start in range(0, len(scanlines), 65535):
        block = scanlines[start:start + 65535]
        final = int(start + len(block) == len(scanlines))
        compressed.extend(struct.pack("<BHH", final, len(block), 65535 - len(block)))
        compressed.extend(block)
    compressed.extend(struct.pack(">I", zlib.adler32(scanlines)))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    with path.open("xb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(_png_chunk(b"IHDR", header))
        handle.write(_png_chunk(b"IDAT", bytes(compressed)))
        handle.write(_png_chunk(b"IEND", b""))


def convert_mono16(source, target):
    """Apply nearest-integer uint16/257, equivalent to the pinned OpenCV scale.

    For integer input there are no half-integer ties: 257 is odd. The integer
    expression avoids float rounding differences and has no saturation cases
    outside the uint8 range. Full source validation precedes this operation.
    """
    import numpy as np
    from PIL import Image

    with Image.open(source, formats=["PNG"]) as image:
        if image.mode not in {"I;16", "I;16B", "I"}:
            raise ValueError("Expected a mono16 grayscale image.")
        image.load()
        pixels = np.asarray(image)
        if pixels.ndim != 2 or pixels.dtype.kind not in "iu":
            raise ValueError("Expected grayscale integer pixels.")
        if np.any(pixels < 0) or np.any(pixels > 65535):
            raise ValueError("Image values are outside mono16.")
        converted = ((pixels.astype(np.uint32) + 128) // 257).astype(np.uint8)
    _write_mono8_png(target, converted)


def _operation(kind, stream, parameters, description):
    return {"kind": kind, "stream_id": stream["id"], "artifact": None,
            "parameters": parameters, "description": description}


def _selected_streams(acquisition, imu_stream, include_reference):
    return selected_streams(acquisition, imu_stream, include_reference)


def prepare_package(source: Path, output: Path, *, prepared_id=None, imu_stream=None,
                    include_reference=True, image_encoding="preserve",
                    limits=DEFAULT_LIMITS, output_limits=None, calibration=None,
                    clock_mappings=(), dependency_acquisitions=(),
                    apply_imu_intrinsics=False, imu_target_frame=None) -> dict:
    """Prepare all segments of a validated acquisition into a new directory.

    Explicitly choose an IMU when several are present. Reference copying can be
    disabled without changing the source acquisition. Unknown clocks/axes remain
    unknown; the result is never promoted to estimator readiness.
    ``output_limits`` may impose a smaller derived-payload byte budget while
    keeping the source acquisition's independent validation limits.
    """
    if type(include_reference) is not bool or image_encoding not in {"preserve", "mono8"}:
        raise ValueError("Unsupported preparation option.")
    if prepared_id is not None and (type(prepared_id) is not str
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,79}", prepared_id) is None):
        raise ValueError("Prepared identity must be an ASCII slug of at most 80 characters.")
    source, output = Path(source), Path(output)
    output_limits = limits if output_limits is None else output_limits
    _check_destination(source, output)
    result = validate_package(source, limits=limits)
    acquisition = result["acquisition"]
    streams, profile = _selected_streams(acquisition, imu_stream, include_reference)
    clocks = {clock["id"]: clock for clock in acquisition["clocks"]}
    inventory = {entry["path"]: entry for entry in acquisition["files"]}
    application = None
    if (calibration is not None or clock_mappings or dependency_acquisitions
            or apply_imu_intrinsics is not False or imu_target_frame is not None):
        application = validate_application_request(acquisition, calibration=calibration,
            clock_mappings=clock_mappings, dependency_acquisitions=dependency_acquisitions,
            apply_imu_intrinsics=apply_imu_intrinsics, imu_target_frame=imu_target_frame,
            imu_stream=imu_stream, include_reference=include_reference, image_encoding=image_encoding)
        request = application["request"]
        calibration, clock_mappings, dependency_acquisitions = (
            request["calibration"], request["clock_mappings"], request["dependency_acquisitions"])
    method = application["method"] if application else METHOD
    mapping_index = {(m["source_stream_id"], m["source_segment_id"]): m for m in clock_mappings}
    rotation = next((t["rotation"] for t in calibration["transforms"] if t["target_frame"] == imu_target_frame
                     and t["source_frame"] == next(s["frame_id"] for s in streams if s["kind"] == "imu")), None) if calibration and imu_target_frame else None
    prepared = {
        "format": "meridian-sensor-contract", "schema_version": 1,
        "kind": "prepared_input", "id": prepared_id or (application["prepared_id"] if application else f"prepared-{contract_sha256(acquisition)[:24]}"),
        "acquisition": _identity(acquisition), "calibration": _identity(calibration) if calibration else None,
        "clock_mappings": [_identity(m) for m in clock_mappings],
        "method": dict(method),
        "selections": [{"stream_id": stream["id"], "segment_id": segment["id"],
                        "first_tick": segment["first_tick"], "last_tick": segment["last_tick"]}
                       for stream in streams for segment in stream["segments"]],
        "operations": [], "outputs": [], "files": [], "target_profile": profile,
    }
    output.mkdir(parents=True, exist_ok=False)
    try:
        conversion_evidence, summaries, payload_bytes = [], [], 0
        def account(amount, *, csv_file=False):
            nonlocal payload_bytes
            ceiling = min(output_limits.max_file_bytes, output_limits.max_csv_bytes) if csv_file else output_limits.max_file_bytes
            if amount > ceiling or payload_bytes + amount > output_limits.max_total_bytes:
                raise ValueError("Prepared payload exceeds the configured byte limits.")
            payload_bytes += amount
        for stream in streams:
            kind = stream["kind"]
            branch = "references" if kind == "reference_pose" else "sensors"
            index_path = f"{branch}/{stream['id']}/data.csv"
            index = output / index_path
            index.parent.mkdir(parents=True)
            source_unit = clocks[stream["clock_id"]]["unit"]
            convert_image = (kind == "camera" and image_encoding == "mono8"
                             and stream["camera"]["encoding"] == "mono16")
            if convert_image:
                prepared["operations"].append(_operation(
                    "image_conversion", stream,
                    {"source_encoding": "mono16", "target_encoding": "mono8",
                     "scale": 255 / 65535, "offset": 0, "rounding": "opencv_convertTo"},
                    "Integer nearest uint16/257; deterministic grayscale PNG encoding."))
            if kind == "imu":
                imu = stream["imu"]
                for quantity, field, target in (
                    ("angular_velocity", "angular_velocity_unit", "rad/s"),
                    ("specific_force", "specific_force_unit", "m/s^2"),
                ):
                    if imu[field] != target:
                        prepared["operations"].append(_operation(
                            "unit_conversion", stream,
                            {"quantity": quantity, "source_unit": imu[field], "target_unit": target,
                             "local_gravity_m_s2": (imu["local_gravity_m_s2"]
                                                   if quantity == "specific_force" else None)},
                            "Explicit source-unit conversion; no bias, scale calibration or frame change."))
            count, first, last = 0, None, None
            with index.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                csv_bytes = writer.writerow(HEADERS[kind])
                for raw in iter_stream_rows(source, stream, limits=limits):
                    row = list(raw)
                    row[2] = str(timestamp_to_ns(raw[2], source_unit))
                    mapping = mapping_index.get((stream["id"], raw[1]))
                    if mapping is not None:
                        row[2] = str(apply_clock(int(row[2]), mapping))
                    if kind == "imu":
                        if imu["angular_velocity_unit"] != "rad/s":
                            row[3:6] = [repr(gyro_to_rad_s(float(v), imu["angular_velocity_unit"]))
                                        for v in raw[3:6]]
                        if imu["specific_force_unit"] != "m/s^2":
                            row[6:9] = [repr(accel_to_m_s2(
                                float(v), imu["specific_force_unit"],
                                local_gravity_m_s2=imu["local_gravity_m_s2"])) for v in raw[6:9]]
                        if apply_imu_intrinsics or rotation is not None:
                            row[3:9] = [repr(v) for v in apply_imu(list(map(float, row[3:9])),
                                intrinsics=calibration["imu"] if apply_imu_intrinsics else None,
                                rotation=rotation)]
                    elif kind == "camera":
                        relative = (Path(stream["index_path"]).parent / raw[3]).as_posix()
                        target_relative = f"{branch}/{stream['id']}/images/{raw[0]}.png"
                        target = output / target_relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if convert_image:
                            width, height = stream["camera"]["resolution"]
                            raw_size = height * (width + 1)
                            png_size = raw_size + 5 * ((raw_size + 65534) // 65535) + 63
                            if png_size > output_limits.max_png_bytes:
                                raise ValueError("Prepared PNG exceeds the configured byte limit.")
                            account(png_size)
                            snapshot = target.with_suffix(".source16")
                            _copy(source / relative, snapshot, inventory[relative], limits=limits)
                            convert_mono16(snapshot, target)
                            snapshot.unlink()
                            identity = file_identity(target)
                            conversion_evidence.append({
                                "stream_id": stream["id"], "sample_id": raw[0],
                                "source": {"path": relative, **{key: inventory[relative][key]
                                                              for key in ("sha256", "size_bytes")}},
                                "output": {"path": target_relative, **identity},
                                "resolution": stream["camera"]["resolution"],
                                "source_encoding": "mono16", "target_encoding": "mono8",
                            })
                        else:
                            account(inventory[relative]["size_bytes"])
                            identity = _copy(source / relative, target, inventory[relative], limits=limits)
                        prepared["files"].append({"path": target_relative, **identity, "role": "sensor"})
                        row[3] = f"images/{raw[0]}.png"
                    csv_bytes += writer.writerow(row)
                    if (csv_bytes > min(output_limits.max_csv_bytes, output_limits.max_file_bytes)
                            or payload_bytes + csv_bytes > output_limits.max_total_bytes):
                        raise ValueError("Prepared CSV exceeds the configured byte limits.")
                    count += 1
                    first = row[2] if first is None else first
                    last = row[2]
            account(csv_bytes, csv_file=True)
            prepared["files"].append({"path": index_path, **file_identity(index),
                                      "role": "reference" if kind == "reference_pose" else "sensor"})
            prepared["outputs"].append({"stream_id": stream["id"], "index_path": index_path,
                                        "frame_id": stream["frame_id"], "clock_id": stream["clock_id"],
                                        "timestamp_unit": "ns"})
            summaries.append({"stream_id": stream["id"], "samples": count,
                              "first_timestamp_ns": first, "last_timestamp_ns": last,
                              "segment_count": len(stream["segments"]),
                              "timestamp_conversion": {"source_unit": source_unit, "target_unit": "ns"},
                              "measurement_semantics": stream["measurement_semantics"],
                              "image_encoding": (("mono8" if convert_image else stream["camera"]["encoding"])
                                                 if kind == "camera" else None)})
        if application:
            prepared["operations"] = application["operations"]
            prepared["outputs"] = application["outputs"]
            documents = ([('calibration.json', calibration)] if calibration else []) + [
                (f"clock-{i:04d}.json", document) for i, document in enumerate(clock_mappings)] + [
                (f"acquisition-{i:04d}.json", document) for i, document in enumerate(dependency_acquisitions)]
            for name, document in documents:
                relative = "evidence/" + name
                encoded = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode()
                account(len(encoded))
                target = output / relative
                target.parent.mkdir(exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(encoded)
                prepared["files"].append({"path": relative, **file_identity(target), "role": "evidence"})
        prepared["files"].sort(key=lambda item: item["path"])
        records = {document["id"]: document for document in [acquisition, *dependency_acquisitions,
                   *([calibration] if calibration else []), *clock_mappings, prepared]}
        validate_bundle(list(records.values()))
        # Detect input changes while preparation was running, including sources
        # used by a decoder. Concurrent source mutation is not a supported mode.
        for relative, expected in inventory.items():
            actual = file_identity(source / relative)
            if any(actual[key] != expected[key] for key in actual):
                raise ValueError("Source identity changed during preparation.")
        descriptor_limits = replace(limits, max_file_bytes=min(limits.max_file_bytes, limits.max_descriptor_bytes))
        _copy(source / "acquisition.json", output / "source-acquisition.json", limits=descriptor_limits)
        retained = loads_contract((output / "source-acquisition.json").read_bytes())
        if contract_sha256(retained) != contract_sha256(acquisition):
            raise ValueError("Acquisition descriptor changed during preparation.")
        _json(output / "prepared-input.json", prepared)
        report = {
            "format": "meridian-sensor-preparation-report", "schema_version": 1,
            "status": "accepted", "acquisition": _identity(acquisition),
            "prepared_input": _identity(prepared), "method": dict(method),
            "input_validation": result["report"],
            "streams": summaries, "image_conversions": conversion_evidence,
            "reference_preparation": "included" if any(s["kind"] == "reference_pose" for s in streams) else "absent",
            "execution_readiness": "not_assessed", "calibration_validity": "not_assessed",
        }
        if application:
            report["application"] = {"request_sha256": application["request_sha256"],
                                     **application["assessment"]}
            report["application"]["application_status"] = "completed"
            for key in ("imu_intrinsics", "imu_frame_rotation"):
                if report["application"][key] == "selected_for_application":
                    report["application"][key] = "applied"
        _json(output / "preparation-report.json", report)
        return {"prepared_input": prepared, "report": report}
    except BaseException:
        shutil.rmtree(output)
        raise


def _metadata(path):
    # Metadata is explicit user input, never a recipe imported from a recording.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate metadata key.")
            result[key] = value
        return result
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Adapter metadata exceeds 1 MiB.")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite metadata.")))
    if type(value) is not dict:
        raise ValueError("Adapter metadata must be an object.")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Verify descriptor and all inventoried payloads.")
    validate.add_argument("source", type=Path)
    for name in ("package", "prepare", "tumvi", "dataflash"):
        command = commands.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("output", type=Path)
        if name == "prepare":
            command.add_argument("--id", dest="prepared_id")
            command.add_argument("--imu-stream")
            command.add_argument("--without-reference", action="store_true")
            command.add_argument("--image-encoding", choices=("preserve", "mono8"), default="preserve")
            command.add_argument("--calibration", type=Path)
            command.add_argument("--clock-mapping", type=Path, action="append", default=[])
            command.add_argument("--dependency-acquisition", type=Path, action="append", default=[])
            command.add_argument("--apply-imu-intrinsics", action="store_true")
            command.add_argument("--imu-target-frame")
        elif name == "tumvi":
            command.add_argument("--id", dest="acquisition_id", required=True)
            command.add_argument("--sequence", required=True)
            command.add_argument("--without-reference", action="store_true")
        elif name == "dataflash":
            command.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            report = validate_package(args.source)["report"]
        elif args.command == "package":
            report = import_package(args.source, args.output)
        elif args.command == "prepare":
            from .sensor_package import _read_bytes
            read_contract = lambda path: loads_contract(_read_bytes(path, DEFAULT_LIMITS.max_descriptor_bytes))
            report = prepare_package(args.source, args.output, prepared_id=args.prepared_id,
                                     imu_stream=args.imu_stream,
                                     include_reference=not args.without_reference,
                                     image_encoding=args.image_encoding,
                                     calibration=read_contract(args.calibration) if args.calibration else None,
                                     clock_mappings=[read_contract(path) for path in args.clock_mapping],
                                     dependency_acquisitions=[read_contract(path) for path in args.dependency_acquisition],
                                     apply_imu_intrinsics=args.apply_imu_intrinsics,
                                     imu_target_frame=args.imu_target_frame)["report"]
        elif args.command == "tumvi":
            from .sensor_tumvi import adapt_tumvi
            acquisition = adapt_tumvi(args.source, args.output, acquisition_id=args.acquisition_id,
                                      sequence=args.sequence, include_reference=not args.without_reference)
            report = {"status": "accepted", "acquisition": _identity(acquisition),
                      "execution_readiness": "not_assessed"}
        else:
            from .sensor_dataflash import build_dataflash_package
            metadata = _metadata(args.metadata)
            if type(metadata.get("instances")) is not dict:
                raise ValueError("DataFlash metadata requires an instances object.")
            if any(re.fullmatch(r"0|[1-9][0-9]*", key) is None for key in metadata["instances"]):
                raise ValueError("DataFlash instance keys must be canonical nonnegative integers.")
            metadata["instances"] = {int(key): value for key, value in metadata["instances"].items()}
            acquisition = build_dataflash_package(args.source, args.output, **metadata)
            report = {"status": "accepted", "acquisition": _identity(acquisition),
                      "execution_readiness": "not_assessed"}
    except (ValueError, OSError, ImportError, TypeError) as error:
        finding = getattr(error, "finding", None)
        if finding is None:
            # Exception text may contain arbitrary local paths or decoder data.
            finding = {"code": "import_rejected", "message": type(error).__name__}
            if type(error) is ValueError:
                finding["message"] = str(error)
        print(json.dumps({"status": "rejected", "findings": [finding]}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
