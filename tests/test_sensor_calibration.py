"""Analytic supplied corrections, immutable revisions and execution boundaries."""
from copy import deepcopy
import csv
import hashlib
import io
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from meridian.sensor_calibration import validate_application_request
from meridian.sensor_contract import contract_sha256, loads_contract, validate_bundle
from meridian.sensor_import import prepare_package
from meridian.sensor_package import materialize_package


EXAMPLES = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"


def identity(document):
    return {"id": document["id"], "sha256": contract_sha256(document)}


def fixture(tmp_path, *, snapshots=False):
    source = tmp_path / "source"
    source.mkdir()
    acquisition = json.loads((EXAMPLES / ("imu-snapshots.json" if snapshots else "stereo-partial-reference.json")).read_text())
    for entry in acquisition["files"]:
        target = source / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / entry["path"], target)
    save(source, acquisition)
    return source, acquisition


def save(source, acquisition):
    for entry in acquisition["files"]:
        payload = (source / entry["path"]).read_bytes()
        entry.update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    (source / "acquisition.json").write_text(json.dumps(acquisition))


def calibration(acquisition):
    value = json.loads((EXAMPLES / "calibration.json").read_text())
    value["acquisition"] = identity(acquisition)
    value["imu"] = {"sensor_id": "imu0", "frame_id": "imu", "gyro_bias_rad_s": [1, 2, 3],
        "accel_bias_m_s2": [.8, .4, .2], "gyro_scale": [2, 3, 4], "accel_scale": [2, 3, 4],
        "noise": {"model": "continuous_white_noise_and_bias_diffusion", "gyro_density_rad_s_per_sqrt_hz": .01,
            "accel_density_m_s2_per_sqrt_hz": .02, "gyro_bias_diffusion_rad_s_per_sqrt_s": .001,
            "accel_bias_diffusion_m_s2_per_sqrt_s": .002, "assumptions": "Supplied corrected-domain model; not fitted or consumed here."}}
    value["applicability"]["imu_intrinsics"] = "candidate_not_applied"
    return value


def mapping(acquisition, *, purpose="clock_domain_mapping", stream_id="imu0", segment=0, offset="2"):
    stream = next(s for s in acquisition["streams"] if s["id"] == stream_id)
    selected = stream["segments"][segment]
    clock = next(c for c in acquisition["clocks"] if c["id"] == stream["clock_id"])
    factor = {"ns": 1, "us": 1000, "ms": 1_000_000, "s": 1_000_000_000}[clock["unit"]]
    origin = str(int(selected["first_tick"]) * factor)
    return {"format": "meridian-sensor-contract", "schema_version": 1, "kind": "clock_mapping",
        "id": f"mapping-{stream_id}-{segment}", "acquisition": identity(acquisition),
        "source_stream_id": stream_id, "source_segment_id": selected["id"],
        "target_clock_id": "camera-clock" if purpose == "clock_domain_mapping" else stream["clock_id"],
        "purpose": purpose, "model": "constant_offset_ns", "source_origin_ns": origin,
        "target_origin_ns": origin, "offset_ns": offset, "first_source_ns": origin,
        "last_source_ns": str(int(selected["last_tick"]) * factor),
        "evidence": {"status": "declared", "description": "Independent analytic constant correction.", "report": None}}


def rows(path):
    with path.open(newline="") as handle:
        return list(csv.reader(handle))[1:]


def contents(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_si_bias_gain_rotation_order_is_analytic_and_originals_are_immutable(tmp_path):
    source, acquisition = fixture(tmp_path)
    imu = next(s for s in acquisition["streams"] if s["kind"] == "imu")
    imu["imu"].update(angular_velocity_unit="deg/s", specific_force_unit="g", local_gravity_m_s2=9.8)
    path = source / imu["index_path"]
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(path.read_text().splitlines()[0].split(","))
    writer.writerows([r[:3] + ["180", "-90", "45", "1", "-2", "-1"] for r in rows(path)])
    path.write_text(text.getvalue())
    save(source, acquisition)
    value = calibration(acquisition)
    original = contents(source)
    plan = validate_application_request(acquisition, calibration=value,
                                        apply_imu_intrinsics=True, imu_target_frame="cam0")
    assert plan["assessment"]["application_status"] == "planned"
    assert plan["assessment"]["imu_intrinsics"] == "selected_for_application"
    result = prepare_package(source, tmp_path / "corrected", calibration=value,
                             apply_imu_intrinsics=True, imu_target_frame="cam0")
    output = rows(tmp_path / "corrected/sensors/imu0/data.csv")
    assert list(map(float, output[0][3:])) == pytest.approx([
        -1.5 * math.pi - 6, math.pi - 12, 2 * math.pi - 2, -60, -40, 18])
    assert [r[:3] for r in output] == [r[:3] for r in rows(path)]
    assert [op["kind"] for op in result["prepared_input"]["operations"]] == [
        "unit_conversion", "unit_conversion", "imu_calibration", "frame_transform"]
    assert result["prepared_input"]["outputs"][2]["frame_id"] == "cam0"
    assert result["report"]["application"]["noise_parameters"] == "retained_unconsumed"
    assert result["report"]["application"]["application_status"] == "completed"
    assert result["report"]["calibration_validity"] == "not_assessed"
    assert contents(source) == original
    for stream in (s for s in acquisition["streams"] if s["kind"] != "imu"):
        raw = rows(source / stream["index_path"])
        prepared = rows(tmp_path / "corrected" / stream["index_path"])
        assert [r[:3] for r in prepared] == [r[:3] for r in raw]
        if stream["kind"] == "reference_pose":
            assert prepared == raw
    for entry in acquisition["files"]:
        if entry["path"].endswith(".png"):
            assert (tmp_path / "corrected" / entry["path"]).read_bytes() == (source / entry["path"]).read_bytes()


def test_selected_parameters_preserve_processed_observations_and_repeat_exactly(tmp_path):
    source, acquisition = fixture(tmp_path)
    for stream in acquisition["streams"]:
        stream["processing"].update(calibration="applied", clock_correction="applied")
    save(source, acquisition)
    value = calibration(acquisition)
    value["applicability"]["imu_intrinsics"] = "already_applied"
    first = prepare_package(source, tmp_path / "one", calibration=value)
    second = prepare_package(source, tmp_path / "two", calibration=value)
    assert first == second and contents(tmp_path / "one") == contents(tmp_path / "two")
    assert first["prepared_input"]["operations"] == []
    assert first["prepared_input"]["method"]["version"] == "2"
    assert rows(tmp_path / "one/sensors/imu0/data.csv") == rows(source / "sensors/imu0/data.csv")
    retained = json.loads((tmp_path / "one/evidence/calibration.json").read_text())
    assert retained == value
    assert first["report"]["application"]["imu_intrinsics"] == "not_applied_by_preparation"
    before = contents(tmp_path / "one")
    changed = deepcopy(value)
    changed["cameras"][0]["intrinsics"][0] += .5
    third = prepare_package(source, tmp_path / "three", calibration=changed)
    assert third["prepared_input"]["id"] != first["prepared_input"]["id"]
    assert third["prepared_input"]["calibration"] != first["prepared_input"]["calibration"]
    assert contents(tmp_path / "one") == before
    plain = prepare_package(source, tmp_path / "plain")
    assert plain["prepared_input"]["method"]["version"] == "1"


@pytest.mark.parametrize("purpose,offset", [("clock_domain_mapping", "2"), ("sensor_time_offset", "-2")])
def test_clock_mapping_exact_epoch_sign_and_measurement_preservation(tmp_path, purpose, offset):
    source, acquisition = fixture(tmp_path)
    clock = mapping(acquisition, purpose=purpose, offset=offset)
    result = prepare_package(source, tmp_path / "mapped", clock_mappings=[clock])
    before, after = rows(source / "sensors/imu0/data.csv"), rows(tmp_path / "mapped/sensors/imu0/data.csv")
    assert all(int(a[2]) == int(b[2]) + int(offset) for b, a in zip(before, after, strict=True))
    assert all(a[:2] + a[3:] == b[:2] + b[3:] for b, a in zip(before, after, strict=True))
    assert int(after[0][2]) > 2**53
    assert result["prepared_input"]["outputs"][2]["clock_id"] == clock["target_clock_id"]
    validate_bundle([acquisition, clock, result["prepared_input"]])


@pytest.mark.parametrize("state", ["applied", "unknown"])
def test_unknown_or_applied_corrections_cannot_be_executed_again(tmp_path, state):
    source, acquisition = fixture(tmp_path)
    imu = next(s for s in acquisition["streams"] if s["kind"] == "imu")
    imu["processing"].update(calibration=state, clock_correction=state)
    save(source, acquisition)
    with pytest.raises(ValueError, match="unapplied"):
        prepare_package(source, tmp_path / "intrinsics", calibration=calibration(acquisition), apply_imu_intrinsics=True)
    with pytest.raises(ValueError, match="already applied|unknown"):
        prepare_package(source, tmp_path / "clock", clock_mappings=[mapping(acquisition)])
    assert not (tmp_path / "intrinsics").exists() and not (tmp_path / "clock").exists()


def test_clock_purposes_and_composition_are_not_interchangeable(tmp_path):
    _, acquisition = fixture(tmp_path)
    domain = mapping(acquisition)
    sensor = mapping(acquisition, purpose="sensor_time_offset")
    sensor["id"] = "sensor-offset"
    with pytest.raises(ValueError, match="composition"):
        validate_application_request(acquisition, clock_mappings=[domain, sensor])
    domain["target_clock_id"] = sensor["target_clock_id"]
    with pytest.raises(ValueError, match="distinct target clock"):
        validate_application_request(acquisition, clock_mappings=[domain])
    sensor["target_origin_ns"] = str(int(sensor["source_origin_ns"]) + 1)
    with pytest.raises(ValueError, match="equal origins"):
        validate_application_request(acquisition, clock_mappings=[sensor])


def test_reset_segments_are_mapped_individually_without_merging(tmp_path):
    source, acquisition = fixture(tmp_path, snapshots=True)
    stream = acquisition["streams"][0]
    stream["processing"]["clock_correction"] = "not_applied"
    save(source, acquisition)
    clocks = [mapping(acquisition, purpose="sensor_time_offset", segment=i, offset="7")
              for i in range(len(stream["segments"]))]
    result = prepare_package(source, tmp_path / "mapped", clock_mappings=clocks)
    before, after = rows(source / stream["index_path"]), rows(tmp_path / "mapped/sensors/imu0/data.csv")
    assert [r[:2] for r in after] == [r[:2] for r in before]
    assert [int(r[2]) for r in after] == [int(r[2]) * 1000 + 7 for r in before]
    assert int(after[2][2]) < int(after[1][2])
    assert result["report"]["application"]["clock_corrections"] == 2


def test_corrected_stereo_must_still_pair_exactly(tmp_path):
    _, acquisition = fixture(tmp_path)
    clock = mapping(acquisition, purpose="sensor_time_offset", stream_id="cam0")
    with pytest.raises(ValueError, match="exact stereo"):
        validate_application_request(acquisition, clock_mappings=[clock])


@pytest.mark.parametrize("change", ["direction", "translation", "reflection", "resolution", "model", "noise_units"])
def test_application_rejects_frame_model_resolution_and_noise_mistakes(tmp_path, change):
    _, acquisition = fixture(tmp_path)
    value = calibration(acquisition)
    if change == "direction":
        value["transforms"][0]["source_frame"], value["transforms"][0]["target_frame"] = "cam0", "imu"
    if change == "translation": value["transforms"][0]["translation_m"][0] = .1
    if change == "reflection": value["transforms"][0]["rotation"][0][1] = -1
    if change == "resolution": value["cameras"][0]["resolution"][0] += 1
    if change == "model": value["cameras"][0]["projection"] = "unknown"
    if change == "noise_units": value["imu"]["noise"]["gyro_variance"] = .01
    with pytest.raises(ValueError):
        validate_application_request(acquisition, calibration=value, imu_target_frame="cam0")


def test_cross_acquisition_artifact_closure_is_retained_and_hashed(tmp_path):
    source, acquisition = fixture(tmp_path)
    support = deepcopy(acquisition)
    support["id"] = "calibration-source"
    value = calibration(support)
    result = prepare_package(source, tmp_path / "prepared", calibration=value, dependency_acquisitions=[support])
    assert json.loads((tmp_path / "prepared/evidence/acquisition-0000.json").read_text()) == support
    validate_bundle([acquisition, support, value, result["prepared_input"]])
    for entry in result["prepared_input"]["files"]:
        assert hashlib.sha256((tmp_path / "prepared" / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    with pytest.raises(ValueError):
        validate_application_request(acquisition, calibration=value)


def test_numeric_overflow_rejects_partial_revision_and_preserves_source(tmp_path):
    source, acquisition = fixture(tmp_path)
    value = calibration(acquisition)
    value["imu"]["gyro_bias_rad_s"][0] = -1e308
    value["imu"]["gyro_scale"][0] = 2
    before = contents(source)
    with pytest.raises(ValueError, match="finite numerical range"):
        prepare_package(source, tmp_path / "overflow", calibration=value, apply_imu_intrinsics=True)
    assert not (tmp_path / "overflow").exists() and contents(source) == before


def test_application_planner_has_no_payload_or_third_party_dependency(tmp_path):
    _, acquisition = fixture(tmp_path)
    document = tmp_path / "request.json"
    document.write_text(json.dumps({"acquisition": acquisition, "calibration": calibration(acquisition)}))
    program = '''
import builtins,json,sys
with open(sys.argv[1]) as stream: request=json.load(stream)
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'numpy','PIL'}: raise RuntimeError('Forbidden dependency')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
from meridian.sensor_calibration import validate_application_request
builtins.open=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('Payload access'))
assert validate_application_request(request['acquisition'], calibration=request['calibration'])['method']['version']=='2'
'''
    subprocess.run([sys.executable, "-c", program, str(document)], check=True, capture_output=True)


def test_cli_accepts_structured_artifacts_and_never_overwrites_prior_revision(tmp_path):
    source, acquisition = fixture(tmp_path)
    value = calibration(acquisition)
    document = tmp_path / "calibration.json"
    document.write_text(json.dumps(value))
    command = [sys.executable, "-m", "meridian.sensor_import", "prepare", str(source), str(tmp_path / "prepared"),
               "--calibration", str(document), "--apply-imu-intrinsics", "--imu-target-frame", "cam0"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["application"]["imu_intrinsics"] == "applied"
    before = contents(tmp_path / "prepared")
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert contents(tmp_path / "prepared") == before


def test_public_generator_repeats_and_two_revisions_match_independent_csv_expectations(tmp_path):
    path = EXAMPLES.parents[1] / "scripts/make-calibration-fixture.py"
    spec = importlib.util.spec_from_file_location("calibration_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = tmp_path / "generated"
    manifest = module.build(generated)
    assert module.build(tmp_path / "repeated") == manifest
    assert contents(generated) == contents(tmp_path / "repeated")
    acquisitions = {}
    for name in ("source", "replay", "validation"):
        acquisitions[name] = materialize_package(generated / (name + ".zip"), tmp_path / name)["acquisition"]
    mappings = [loads_contract((generated / "clock-domain.json").read_bytes())]
    dependencies = [acquisitions["source"], acquisitions["validation"]]
    for name, expected_key in (("calibration.json", "expected_revision_a"), ("calibration-alternative.json", "expected_revision_b")):
        supplied = loads_contract((generated / name).read_bytes())
        result = prepare_package(tmp_path / "replay", tmp_path / expected_key, calibration=supplied,
            clock_mappings=mappings, dependency_acquisitions=dependencies, apply_imu_intrinsics=True,
            imu_target_frame="imu-corrected")
        path = tmp_path / expected_key / "sensors/imu0/data.csv"
        assert rows(path) == manifest[expected_key]["rows"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[expected_key]["imu_csv"]["sha256"]
        assert result["report"]["application"]["independent_validation"] == "reported_not_verified"
    for name in ("invalid-duplicate-key.json", "invalid-rotation.json", "invalid-noise-unit.json"):
        with pytest.raises(ValueError):
            loads_contract((generated / name).read_bytes())
    wrong = loads_contract((generated / "invalid-resolution.json").read_bytes())
    with pytest.raises(ValueError):
        validate_application_request(acquisitions["replay"], calibration=wrong, dependency_acquisitions=dependencies)
