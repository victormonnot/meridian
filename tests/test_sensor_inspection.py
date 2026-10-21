"""Analytic evidence for timing/support, clock separation and bounded display."""
from copy import deepcopy
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from meridian import sensor_inspection as inspection
from meridian.sensor_package import materialize_package


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inspection_fixture", ROOT / "scripts/make-inspection-fixture.py")
fixture_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_builder)


def build(tmp_path, **options):
    archive = tmp_path / "source.zip"
    fixture_builder.build(archive, **options)
    source = tmp_path / "acquisition"
    validated = materialize_package(archive, source)
    output = tmp_path / "inspection"
    summary = inspection.write_inspection(source, output, validated=validated)
    reader = lambda path: json.loads((tmp_path / path).read_bytes())
    return source, output, summary, reader


def window(summary, reader, stream_id="imu0", segment_id="segment0", **kwargs):
    stream = next(x for x in summary["streams"] if x["id"] == stream_id)
    segment = next(x for x in stream["segments"] if x["id"] == segment_id)
    return inspection.inspection_window(summary, reader, stream_id=stream_id, segment_id=segment_id,
        first_ns=kwargs.pop("first_ns", segment["first_ns"]), last_ns=kwargs.pop("last_ns", segment["last_ns"]), **kwargs)


def rewrite(source, descriptor, stream, rows):
    path = source / stream["index_path"]
    old_header = path.read_text().splitlines()[0]
    content = io.StringIO(newline="")
    writer = csv.writer(content, lineterminator="\n")
    writer.writerow(old_header.split(","))
    writer.writerows(rows)
    path.write_text(content.getvalue())
    item = next(x for x in descriptor["files"] if x["path"] == stream["index_path"])
    item.update(size_bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (source / "acquisition.json").write_text(json.dumps(descriptor))


def test_analytic_support_counts_and_exact_large_nanoseconds(tmp_path):
    _, _, summary, reader = build(tmp_path)
    camera = window(summary, reader, "cam0")
    assert camera["sample_count"] == 17 and camera["display"]["point_count"] == 17
    assert camera["display"]["points"][0]["timestamp_ns"] == str(fixture_builder.ORIGIN)
    assert int(camera["display"]["points"][0]["timestamp_ns"]) > 2**53
    assert [(x["kind"], x["supported_samples"]) for x in camera["support"]] == [("imu", 15), ("reference_pose", 13)]
    stereo = next(x for x in summary["comparisons"] if x["kind"] == "stereo_pairing")
    assert stereo["matched_samples"] == 17 and stereo["unmatched_samples"] == {"cam0": 0, "cam1": 0}
    assert summary["evidence"]["estimate_support"] == "not_generated"
    assert summary["evidence"]["estimator_compatibility"] == "not_assessed"
    assert summary["evidence"]["calibration_suitability"] == "not_assessed"
    assert camera["timing"]["gap_count"] == 1
    assert sum(x["count"] for x in camera["timing"]["histogram"]) == 16


def test_chunk_boundary_gap_and_all_axis_extrema_survive_reduction(tmp_path):
    _, _, summary, reader = build(tmp_path)
    stream = next(x for x in summary["streams"] if x["id"] == "imu0")
    assert [c["rows"] for c in stream["segments"][0]["chunks"]] == [256, 105]
    full = window(summary, reader)
    reduced = window(summary, reader, max_points=20)
    assert reduced["sample_count"] == full["sample_count"] == 361
    assert reduced["imu"] == full["imu"] and reduced["timing"] == full["timing"]
    assert reduced["gaps"] == [{"previous_sample_id": "255", "previous_timestamp_ns": str(fixture_builder.ORIGIN + 637_500_000),
        "sample_id": "256", "timestamp_ns": str(fixture_builder.ORIGIN + 740_000_000), "interval_ns": "102500000"}]
    selected = {row["sample_id"] for row in reduced["display"]["points"]}
    assert {"0", "120", "255", "256", "360"} <= selected
    assert reduced["display"]["point_count"] <= 20
    assert reduced["imu"]["axes"]["fx"]["maximum"] == 8
    assert reduced["imu"]["saturation"] == {"status": "unknown", "reason": "sensor_limits_not_declared"}
    assert set(reduced["imu"]["axes"]) == {"wx", "wy", "wz", "fx", "fy", "fz"}


def test_full_resolution_interval_stats_and_final_endpoint_inside_chunk(tmp_path):
    _, _, summary, reader = build(tmp_path)
    selected = window(summary, reader, first_ns=str(fixture_builder.ORIGIN + 5_000_000),
                      last_ns=str(fixture_builder.ORIGIN + 25_000_000), max_points=6)
    assert selected["sample_count"] == 9
    assert selected["display"]["points"][0]["sample_id"] == "2"
    assert selected["display"]["points"][-1]["sample_id"] == "10"
    assert selected["imu"]["axes"]["wx"]["mean"] == pytest.approx(sum(i / 1000 for i in range(2, 11)) / 9)
    assert selected["timing"]["rate_hz"] == 400


def test_window_entirely_inside_gap_has_no_fabricated_samples(tmp_path):
    _, _, summary, reader = build(tmp_path)
    result = window(summary, reader, first_ns=str(fixture_builder.ORIGIN + 660_000_000),
                    last_ns=str(fixture_builder.ORIGIN + 700_000_000))
    assert result["sample_count"] == 0 and result["gap_count"] == 1
    assert result["display"]["points"] == []
    assert result["imu"]["activity_evidence"]["status"] == "no_samples"


def test_small_display_cap_requests_narrower_window_without_joining_gaps(tmp_path):
    _, _, summary, reader = build(tmp_path)
    result = window(summary, reader, max_points=2)
    assert result["display"]["status"] == "narrower_interval_required"
    assert result["display"]["points"] == [] and result["sample_count"] == 361
    assert result["imu"]["axes"]["fx"]["maximum"] == 8


@pytest.mark.parametrize("options,reason", [({"reset": True}, "ambiguous_reset_eras"),
                                           ({"foreign_clock": True}, "different_clock_domains")])
def test_clock_domains_and_reset_eras_are_never_implicitly_aligned(tmp_path, options, reason):
    _, _, summary, reader = build(tmp_path, **options)
    comparisons = [x for x in summary["comparisons"] if x["kind"] == "imu_support"]
    assert all(x["status"] == "not_comparable" and x["reason"] == reason and x["fraction"] is None for x in comparisons)
    camera = window(summary, reader, "cam0")
    assert camera["support"][0]["not_comparable_samples"] == 17
    assert camera["support"][0]["fraction"] is None
    if options.get("reset"):
        result = window(summary, reader, segment_id="reset1")
        assert result["sample_count"] == 41 and result["display"]["points"][0]["timestamp_ns"] == "0"
        stream = next(x for x in summary["streams"] if x["id"] == "imu0")
        assert stream["segments"][1]["boundary_from_previous"]["kind"] == "reset_or_overlap"


def test_reference_absence_does_not_become_an_estimator_rejection(tmp_path):
    _, _, summary, reader = build(tmp_path, no_reference=True)
    assert summary["reference_availability"] == "absent"
    assert summary["status"] == "available"
    assert summary["evidence"]["estimator_compatibility"] == "not_assessed"
    assert all(x["kind"] != "reference_pose" for x in window(summary, reader, "cam0")["support"])


def test_reference_boundaries_use_fixed_20ms_policy_instead_of_cadence(tmp_path):
    source, _, _, _ = build(tmp_path)
    descriptor = json.loads((source / "acquisition.json").read_text())
    stream = next(x for x in descriptor["streams"] if x["kind"] == "reference_pose")
    ticks = [fixture_builder.ORIGIN + offset for offset in (0, 20_000_000, 40_000_001)]
    rows = [[str(i), "segment0", str(tick), "0", "0", "0", "0", "0", "0", "1"]
            for i, tick in enumerate(ticks)]
    stream["segments"] = [{"id": "segment0", "first_tick": str(ticks[0]),
                           "last_tick": str(ticks[-1]), "sample_count": 3}]
    rewrite(source, descriptor, stream, rows)
    summary = inspection.write_inspection(source, tmp_path / "reference-boundary")
    reader = lambda path: json.loads((tmp_path / "reference-boundary" / Path(path).relative_to("inspection")).read_bytes())
    result = window(summary, reader, stream["id"])
    assert result["timing"]["gap_policy"] == "reference_bracket_limit"
    assert result["timing"]["gap_threshold_ns"] == "20000000"
    assert result["gap_count"] == 1
    assert [row["sample_id"] for row in result["display"]["points"] if row["gap_before"]] == ["2"]
    assert window(summary, reader)["timing"]["gap_policy"] == "cadence_median_multiplier"


def test_unknown_stereo_ties_nonmutual_neighbors_and_tolerance_remain_unmatched():
    # Five-millisecond ties are ambiguous; an isolated match exactly at the
    # boundary is valid, and 5 ms + 1 ns is outside it. Two left observations
    # cannot both consume their same nearest right observation.
    ticks = {"cam0": [0, 10_000_000, 20_000_000, 30_000_000, 40_000_000, 100_000_000, 200_000_000, 204_000_000],
             "cam1": [5_000_000, 20_000_000, 35_000_000, 45_000_001, 105_000_000, 201_000_000]}
    streams, rows = [], {}
    for name, stamps in ticks.items():
        streams.append({"id": name, "kind": "camera", "clock_id": "clock", "sample_count": len(stamps),
            "segments": [{"id": "segment0", "first_ns": str(stamps[0]), "last_ns": str(stamps[-1])}]})
        rows[(name, "segment0")] = [{"sample_id": str(i), "timestamp_ns": str(stamp),
            "support": [], "stereo_match": None} for i, stamp in enumerate(stamps)]
    comparisons = inspection._comparisons({"stereo_pairing": {"camera_ids": ["cam0", "cam1"], "policy": "unknown"}}, streams, rows)
    assert comparisons[0]["matched_samples"] == 3
    assert comparisons[0]["unmatched_samples"] == {"cam0": 5, "cam1": 3}
    assert [(r["sample_id"], r["stereo_match"]["sample_id"]) for r in rows[("cam0", "segment0")] if r["stereo_match"]] == [
        ("2", "1"), ("5", "4"), ("6", "5")]


def test_all_source_imus_keep_individual_clocks_and_explicit_si_conversion(tmp_path):
    source, _, _, _ = build(tmp_path)
    descriptor = json.loads((source / "acquisition.json").read_text())
    original = next(x for x in descriptor["streams"] if x["kind"] == "imu")
    added = deepcopy(original)
    added.update(id="imu1", sensor_instance="second-physical-instance", clock_id="microsecond-clock",
                 index_path="sensors/imu1/data.csv")
    added["imu"] = {"angular_velocity_unit": "deg/s", "specific_force_unit": "g", "local_gravity_m_s2": 9.7}
    added["segments"] = [{"id": "segment0", "first_tick": "1000", "last_tick": "2000", "sample_count": 2}]
    descriptor["clocks"].append({"id": "microsecond-clock", "unit": "us", "epoch": "device"})
    descriptor["streams"].append(added)
    target = source / added["index_path"]
    target.parent.mkdir()
    target.write_text((source / original["index_path"]).read_text().splitlines()[0] + "\n")
    descriptor["files"].append({"path": added["index_path"], "role": "sensor", "sha256": "0" * 64, "size_bytes": 1})
    rewrite(source, descriptor, added, [[str(i), "segment0", str(1000 + i * 1000), "180", "-90", "45", "1", "-2", "3"] for i in range(2)])
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    summary = inspection.write_inspection(source, tmp_path / "all-imus")
    reader = lambda path: json.loads((tmp_path / "all-imus" / Path(path).relative_to("inspection")).read_bytes())
    result = window(summary, reader, "imu1")
    assert result["sample_count"] == 2 and result["clock_id"] == "microsecond-clock"
    assert result["display"]["points"][0]["timestamp_ns"] == "1000000"
    assert result["display"]["points"][0]["gyro_rad_s"] == pytest.approx([math.pi, -math.pi / 2, math.pi / 4])
    assert result["display"]["points"][0]["specific_force_m_s2"] == pytest.approx([9.7, -19.4, 29.1])
    assert all(c["reason"] == "different_clock_domains" for c in summary["comparisons"] if c["stream_ids"][-1] == "imu1")
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before


def test_preview_brightness_and_low_texture_are_evidence_without_verdict(tmp_path):
    _, output, summary, reader = build(tmp_path)
    images = [inspection.inspection_image(summary, reader, stream_id="cam0", sample_id=str(i)) for i in range(3)]
    assert [x["indicators"]["brightness_mean_0_1"] for x in images] == pytest.approx([0, 1, 127 / 255])
    assert images[0]["indicators"]["dark_endpoint_fraction"] == 1
    assert images[1]["indicators"]["bright_endpoint_fraction"] == 1
    assert images[2]["indicators"]["edge_contrast_mean_0_1"] == 0
    assert all("passed" not in x["indicators"] for x in images)
    assert all((output.parent / x["preview_path"]).stat().st_size <= inspection.MAX_PREVIEW_BYTES for x in images)
    assert inspection.inspection_image(summary, reader, stream_id="cam0", sample_id="9999") is None


def test_mono16_preview_uses_declared_full_range_not_8bit_clamping():
    from PIL import Image
    raw = io.BytesIO()
    image = Image.new("I", (3, 1))
    image.putdata([0, 32768, 65535])
    image.convert("I;16").save(raw, format="PNG")
    preview, size, metrics = inspection._preview(raw.getvalue(), {"encoding": "mono16"})
    pixels = list(Image.open(io.BytesIO(preview)).get_flattened_data())
    assert pixels == [0, 127, 255] and size == (3, 1)
    assert metrics["brightness_mean_0_1"] == pytest.approx(sum(pixels) / (3 * 255))


def test_import_rejects_duplicate_ticks_and_does_not_repair_them(tmp_path):
    source, _, _, _ = build(tmp_path)
    descriptor = json.loads((source / "acquisition.json").read_text())
    stream = next(x for x in descriptor["streams"] if x["kind"] == "imu")
    rows = list(csv.reader((source / stream["index_path"]).read_text().splitlines()))[1:]
    rows[10][2] = rows[9][2]
    rewrite(source, descriptor, stream, rows)
    with pytest.raises(ValueError, match="strictly increase"):
        inspection.write_inspection(source, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_finite_extreme_measurements_make_undefined_indicators_explicit(tmp_path):
    source, _, _, _ = build(tmp_path)
    descriptor = json.loads((source / "acquisition.json").read_text())
    stream = next(x for x in descriptor["streams"] if x["kind"] == "imu")
    rows = list(csv.reader((source / stream["index_path"]).read_text().splitlines()))[1:]
    for index, row in enumerate(rows):
        row[3] = "1e308" if index % 2 else "-1e308"
    rewrite(source, descriptor, stream, rows)
    summary = inspection.write_inspection(source, tmp_path / "extreme")
    reader = lambda path: json.loads((tmp_path / "extreme" / Path(path).relative_to("inspection")).read_bytes())
    result = window(summary, reader)
    assert result["imu"]["axes"]["wx"]["status"] == "numerical_range_exceeded"
    assert result["imu"]["axes"]["wx"]["stddev"] is None
    json.dumps(result, allow_nan=False)


def test_new_output_and_independent_budget_preserve_valid_source(tmp_path):
    source, output, summary, _ = build(tmp_path)
    with pytest.raises(ValueError, match="new output"):
        inspection.write_inspection(source, output)
    with pytest.raises(inspection.InspectionLimitError):
        inspection.write_inspection(source, tmp_path / "too-small", max_bytes=1)
    assert not (tmp_path / "too-small").exists()
    second = tmp_path / "repeat"
    assert inspection.write_inspection(source, second) == summary
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == {
        p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}


@pytest.mark.parametrize("change", ["version", "path", "count", "identity", "nested"])
def test_summary_corruption_is_rejected_before_read_callback(tmp_path, change):
    _, _, summary, _ = build(tmp_path)
    summary = deepcopy(summary)
    if change == "version": summary["schema_version"] = True
    if change == "path": summary["streams"][0]["segments"][0]["chunks"][0]["path"] = "../private"
    if change == "count": summary["streams"][0]["sample_count"] += 1
    if change == "identity": summary["acquisition"]["sha256"] = None
    if change == "nested": summary["streams"][0] = []
    with pytest.raises(ValueError):
        inspection.validate_summary(summary)


def test_public_query_module_imports_without_pillow_or_numpy():
    program = '''
import builtins
original = builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'PIL','numpy'}:
        raise RuntimeError('Numerical/image dependency imported by query module')
    return original(name,*args,**kwargs)
builtins.__import__ = guarded
from meridian.sensor_inspection import validate_summary, inspection_window, inspection_image
'''
    subprocess.run([sys.executable, "-c", program], check=True, capture_output=True)
