"""Descriptor and cross-record checks; no fixture asserts real sensor validity."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meridian import sensor_contract as contract


ROOT = Path(__file__).resolve().parents[1]
STAMP = 1_520_530_308_199_447_626


def envelope(kind, name):
    return {"format": contract.FORMAT, "schema_version": 1, "kind": kind, "id": name}


def file_record(path, role):
    return {"path": path, "sha256": "0" * 64, "size_bytes": 0, "role": role}


def acquisition():
    value = {
        **envelope("acquisition", "test-acquisition"),
        "purpose": "fixture",
        "rig": {"id": "fixture-rig", "revision": "mounting-and-optics-v1"},
        "provenance": {"source": "Synthetic declarations without payloads", "license": "CC0-1.0",
                       "attribution": "Meridian test fixture", "session_id": "fixture-session", "synthetic": True},
        "frames": [{"id": name, "axes": ["x-forward", "y-left", "z-up"]}
                   for name in ("body", "cam0-frame", "cam1-frame", "world")],
        "clocks": [{"id": name, "unit": "ns", "epoch": "device"} for name in ("imu-clock", "camera-clock")],
        "files": [], "streams": [],
        "stereo_pairing": {"camera_ids": ["cam0", "cam1"], "policy": "exact_timestamp_pairs"},
    }
    for name, kind, frame in (("cam0", "camera", "cam0-frame"), ("cam1", "camera", "cam1-frame"),
                              ("imu0", "imu", "body"), ("reference0", "reference_pose", "body")):
        index = ("references/" if kind == "reference_pose" else "sensors/") + name + ".csv"
        stream = {
            "id": name, "kind": kind, "sensor_instance": name, "frame_id": frame,
            "clock_id": "imu-clock" if kind == "imu" else "camera-clock",
            "timestamp_semantics": "exposure_midpoint" if kind == "camera" else "sample",
            "measurement_semantics": {"camera": "image", "imu": "instantaneous_imu", "reference_pose": "pose"}[kind],
            "index_path": index,
            "segments": [{"id": "segment0", "first_tick": str(STAMP), "last_tick": str(STAMP + 100),
                          "sample_count": 3}],
            "processing": {"calibration": "not_applied", "filtering": "none", "clock_correction": "not_applied",
                           "description": "Synthetic uncorrected metadata"},
        }
        if kind == "camera":
            stream["camera"] = {"resolution": [640, 480], "encoding": "mono16", "shutter": "global"}
        elif kind == "imu":
            stream["imu"] = {"angular_velocity_unit": "deg/s", "specific_force_unit": "g", "local_gravity_m_s2": 9.81}
        else:
            stream["reference"] = {
                "parent_frame_id": "world", "position_unit": "m", "orientation": "Hamilton_xyzw_frame_to_parent",
                "use": "independent_evaluation_only", "coverage": "partial",
                "position_uncertainty_m": None, "orientation_uncertainty_rad": 0.01,
            }
        value["streams"].append(stream)
        value["files"].append(file_record(index, "reference" if kind == "reference_pose" else "sensor"))
    return value


def identity(value):
    return {"id": value["id"], "sha256": contract.contract_sha256(value)}


def records():
    source = acquisition()
    source["streams"][2]["imu"] = {
        "angular_velocity_unit": "rad/s", "specific_force_unit": "m/s^2", "local_gravity_m_s2": None}
    calibration = {
        **envelope("calibration", "test-calibration"), "acquisition": identity(source), "state": "supplied",
        "method": {"name": "synthetic-fixture", "version": "v1"},
        "cameras": [{"sensor_id": "cam0", "frame_id": "cam0-frame", "resolution": [640, 480],
                     "projection": "pinhole", "distortion": "radtan", "intrinsics": [200, 200, 320, 240],
                     "coefficients": [0, 0, 0, 0]}],
        "imu": {"sensor_id": "imu0", "frame_id": "body", "gyro_bias_rad_s": [0, 0, 0],
                "accel_bias_m_s2": [0, 0, 0], "gyro_scale": [1, 1, 1], "accel_scale": [1, 1, 1],
                "noise": {"model": "continuous_white_noise_and_bias_diffusion",
                          "gyro_density_rad_s_per_sqrt_hz": 0.001, "accel_density_m_s2_per_sqrt_hz": 0.01,
                          "gyro_bias_diffusion_rad_s_per_sqrt_s": 0.0001,
                          "accel_bias_diffusion_m_s2_per_sqrt_s": 0.001, "assumptions": "Synthetic white noise"}},
        "transforms": [{"source_frame": "body", "target_frame": "cam0-frame",
                        "rotation": [[0, -1, 0], [1, 0, 0], [0, 0, 1]], "translation_m": [0.1, 0, 0]}],
        "applicability": {"description": "Synthetic candidate only", "imu_intrinsics": "candidate_not_applied"},
        "validation": {"status": "not_provided", "acquisitions": [], "report": None},
    }
    clock = {
        **envelope("clock_mapping", "test-clock"), "acquisition": identity(source),
        "source_stream_id": "imu0", "source_segment_id": "segment0", "target_clock_id": "camera-clock",
        "purpose": "clock_domain_mapping", "model": "constant_offset_ns",
        "source_origin_ns": str(STAMP), "target_origin_ns": str(STAMP), "offset_ns": "2",
        "first_source_ns": str(STAMP), "last_source_ns": str(STAMP + 100),
        "evidence": {"status": "declared", "description": "Synthetic two-nanosecond mapping", "report": None},
    }
    prepared = {
        **envelope("prepared_input", "test-prepared"), "acquisition": identity(source),
        "method": {"name": "synthetic-fixture", "version": "v1"},
        "calibration": identity(calibration), "clock_mappings": [identity(clock)],
        "selections": [{"stream_id": name, "segment_id": "segment0",
                        "first_tick": str(STAMP), "last_tick": str(STAMP + 100)}
                       for name in ("cam0", "cam1", "imu0")],
        "operations": [{"kind": "clock_mapping", "stream_id": "imu0", "artifact": identity(clock),
                        "description": "Declared time mapping only", "parameters": {}}],
        "files": [file_record("sensors/" + name + ".csv", "sensor") for name in ("cam0", "cam1", "imu0")],
        "outputs": [{"stream_id": name, "index_path": "sensors/" + name + ".csv",
                     "frame_id": frame, "clock_id": "camera-clock", "timestamp_unit": "ns"}
                    for name, frame in (("cam0", "cam0-frame"), ("cam1", "cam1-frame"), ("imu0", "body"))],
        "target_profile": "stereo_imu_candidate_v1",
    }
    return [source, calibration, clock, prepared]


def set_path(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def test_four_kinds_are_canonical_independent_and_cross_bound():
    bundle = records()
    original = deepcopy(bundle)
    contract.validate_bundle(bundle)
    for item in bundle:
        contract.validate_contract(item)
        assert contract.loads_contract(json.dumps(item)) == item
        assert contract.contract_sha256(dict(reversed(list(item.items())))) == contract.contract_sha256(item)
    assert bundle == original


@pytest.mark.parametrize("value", [None, [], True, 1, "acquisition", {"kind": []}, {"kind": "unknown"}])
def test_invalid_envelopes_raise_value_error(value):
    with pytest.raises(ValueError):
        contract.validate_contract(value)


@pytest.mark.parametrize("path,replacement", [
    (("schema_version",), True), (("schema_version",), 1.0), (("schema_version",), 2),
    (("id",), "../escape"), (("id",), "été"), (("provenance", "synthetic"), 1),
    (("provenance", "source"), "\ud800"), (("frames", 0, "axes"), ["x", "y"]),
    (("frames", 0, "axes"), ["", "y", "z"]), (("clocks", 0, "unit"), "ticks"),
    (("files", 0, "sha256"), "A" * 64), (("files", 0, "size_bytes"), True),
    (("streams", 2, "imu", "local_gravity_m_s2"), None),
    (("streams", 2, "imu", "local_gravity_m_s2"), float("inf")),
    (("streams", 0, "camera", "resolution"), [True, 480]),
    (("streams", 0, "segments", 0, "first_tick"), STAMP),
    (("streams", 0, "segments", 0, "first_tick"), "01"),
    (("streams", 0, "segments", 0, "first_tick"), "-0"),
    (("streams", 0, "segments", 0, "sample_count"), 0),
    (("streams", 0, "segments", 0, "sample_count"), True),
    (("streams", 0, "frame_id"), "missing"), (("streams", 0, "clock_id"), "missing"),
    (("streams", 0, "measurement_semantics"), "filtered_snapshot"),
    (("streams", 3, "reference", "use"), "estimator_initialization"),
    (("streams", 3, "reference", "position_uncertainty_m"), -1),
    (("stereo_pairing", "camera_ids"), ["cam0", "cam0"]),
    (("streams", 1, "clock_id"), "imu-clock"),
    (("streams", 1, "segments", 0, "sample_count"), 4),
])
def test_invalid_acquisition_declarations(path, replacement):
    value = acquisition()
    set_path(value, path, replacement)
    with pytest.raises(ValueError):
        contract.validate_contract(value)


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../escape", "a/./b", "a//b",
                                "a\\b", "C:/data", "https://example.invalid/a", "a/\x00b", "a/"])
def test_unsafe_payload_paths_are_rejected(path):
    value = acquisition()
    value["files"][0]["path"] = path
    with pytest.raises(ValueError):
        contract.validate_contract(value)


def test_unknown_semantics_and_absent_reference_do_not_invent_readiness():
    value = acquisition()
    value["streams"] = value["streams"][:-1]
    value["frames"][0]["axes"] = None
    value["streams"][2]["timestamp_semantics"] = "unknown"
    value["streams"][2]["processing"]["calibration"] = "unknown"
    result = contract.assess_acquisition(value)
    assert result["descriptor_validity"] == "valid"
    assert result["reference_availability"] == "absent"
    assert result["metadata_blockers"]
    assert not any("reference" in item for item in result["metadata_blockers"])
    assert result["payload_verification"] == "not_performed"
    assert result["execution_readiness"] == "not_assessed"
    assert result["calibration_validity"] == "not_assessed"


def test_resets_remain_ordered_segments_and_are_not_silently_sorted():
    value = acquisition()
    imu = value["streams"][2]
    imu["segments"].append({"id": "after-reset", "first_tick": "5", "last_tick": "15", "sample_count": 3})
    contract.validate_contract(value)
    digest = contract.contract_sha256(value)
    imu["segments"].reverse()
    assert contract.contract_sha256(value) != digest


@pytest.mark.parametrize("index,path,replacement", [
    (1, ("cameras", 0, "intrinsics"), [0, 200, 320, 240]),
    (1, ("cameras", 0, "coefficients"), [0, 0, 0, True]),
    (1, ("imu", "gyro_scale"), [1, -1, 1]),
    (1, ("imu", "noise", "gyro_density_rad_s_per_sqrt_hz"), -0.1),
    (1, ("imu", "noise", "gyro_density_rad_s_per_sqrt_hz"), True),
    (1, ("transforms", 0, "rotation"), [[-1, 0, 0], [0, 1, 0], [0, 0, 1]]),
    (1, ("transforms", 0, "translation_m"), [0, float("nan"), 0]),
    (1, ("validation", "status"), "validated"),
    (2, ("model",), "affine_drift"),
    (2, ("offset_ns",), 2), (2, ("offset_ns",), "9223372036854775807"),
    (2, ("first_source_ns",), str(STAMP + 101)),
    (3, ("operations", 0, "artifact", "sha256"), "1" * 64),
    (3, ("operations", 0, "parameters"), {"offset": 2}),
])
def test_invalid_calibration_clock_and_preparation_metadata(index, path, replacement):
    value = records()[index]
    set_path(value, path, replacement)
    with pytest.raises(ValueError):
        contract.validate_contract(value)


def test_no_unknown_fields_defaults_or_non_json_types_are_admitted():
    for value in records():
        for key in list(value):
            invalid = deepcopy(value)
            del invalid[key]
            with pytest.raises(ValueError):
                contract.validate_contract(invalid)
        for extra in ("validated", "execution_ready", "command"):
            with pytest.raises(ValueError):
                contract.validate_contract({**value, extra: True})
    for replacement in ({"not-a-list"}, (1, 2), b"bytes", {1: "non-string-key"}):
        value = acquisition()
        value["frames"] = replacement
        with pytest.raises(ValueError):
            contract.validate_contract(value)
    cyclic = acquisition()
    cyclic["frames"] = cyclic
    with pytest.raises(ValueError, match="cycle"):
        contract.validate_contract(cyclic)


@pytest.mark.parametrize("text", ['{"kind":"acquisition","kind":"calibration"}', '{"a":{"x":1,"x":2}}',
                                '{"x":NaN}', '{"x":Infinity}', '{"x":1e309}', b"\xff", "[]"])
def test_json_parser_rejects_ambiguous_or_invalid_metadata(text):
    with pytest.raises(ValueError):
        contract.loads_contract(text)


def test_size_and_depth_limits_apply_before_using_metadata(monkeypatch):
    monkeypatch.setattr(contract, "MAX_JSON_BYTES", 100)
    with pytest.raises(ValueError):
        contract.loads_contract(" " * 101)
    with pytest.raises(ValueError):
        contract.validate_contract(acquisition())
    nested = []
    for _ in range(34):
        nested = [nested]
    with pytest.raises(ValueError):
        contract.validate_contract(nested)


def test_bundle_does_not_treat_hash_shape_as_resolved_evidence():
    bundle = records()
    with pytest.raises(ValueError, match="missing"):
        contract.validate_bundle(bundle[1:])
    bundle[1]["acquisition"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="mismatched"):
        contract.validate_bundle(bundle)


@pytest.mark.parametrize("index,path,replacement", [
    (1, ("cameras", 0, "frame_id"), "body"),
    (1, ("cameras", 0, "resolution"), [320, 240]),
    (1, ("transforms", 0, "target_frame"), "not-a-frame"),
    (2, ("source_segment_id",), "missing"), (2, ("target_clock_id",), "missing"),
    (2, ("last_source_ns",), str(STAMP + 101)),
    (3, ("selections", 0, "first_tick"), str(STAMP - 1)),
    (3, ("operations", 0, "stream_id"), "cam0"),
])
def test_cross_record_contradictions_are_rejected(index, path, replacement):
    bundle = records()
    set_path(bundle[index], path, replacement)
    # Rebind dependent identities so the substantive inconsistency is examined.
    if index == 1:
        bundle[3]["calibration"] = identity(bundle[1])
    elif index == 2:
        bundle[3]["clock_mappings"] = [identity(bundle[2])]
        bundle[3]["operations"][0]["artifact"] = identity(bundle[2])
    contract.validate_contract(bundle[index])
    with pytest.raises(ValueError):
        contract.validate_bundle(bundle)


def operation(kind, stream_id, parameters, artifact=None):
    return {"kind": kind, "stream_id": stream_id, "artifact": artifact,
            "description": "Synthetic declared transformation", "parameters": parameters}


def test_all_declared_operations_resolve_known_source_conventions():
    bundle = records()
    bundle[0]["streams"][2]["imu"] = acquisition()["streams"][2]["imu"]
    bundle[1]["transforms"][0]["translation_m"] = [0, 0, 0]
    rebind_source(bundle)
    prepared = bundle[3]
    prepared["operations"] += [
        operation("unit_conversion", "imu0", {"quantity": "angular_velocity", "source_unit": "deg/s",
                  "target_unit": "rad/s", "local_gravity_m_s2": None}),
        operation("unit_conversion", "imu0", {"quantity": "specific_force", "source_unit": "g",
                  "target_unit": "m/s^2", "local_gravity_m_s2": 9.81}),
        operation("image_conversion", "cam0", {"source_encoding": "mono16", "target_encoding": "mono8",
                  "scale": 255 / 65535, "offset": 0, "rounding": "opencv_convertTo"}),
        operation("imu_calibration", "imu0", {"model": "diagonal_scale_after_bias_subtraction"}, identity(bundle[1])),
        operation("frame_transform", "imu0", {"source_frame": "body", "target_frame": "cam0-frame"}, identity(bundle[1])),
    ]
    prepared["outputs"][2]["frame_id"] = "cam0-frame"
    contract.validate_bundle(bundle)
    prepared["operations"].append(deepcopy(prepared["operations"][-1]))
    with pytest.raises(ValueError, match="Only one frame transform"):
        contract.validate_contract(prepared)


def test_full_imu_calibration_cannot_be_applied_twice():
    bundle = records()
    bundle[3]["operations"] = [
        operation("imu_calibration", "imu0", {"model": "diagonal_scale_after_bias_subtraction"}, identity(bundle[1]))]
    bundle[1]["applicability"]["imu_intrinsics"] = "already_applied"
    bundle[3]["calibration"] = identity(bundle[1])
    bundle[3]["operations"][0]["artifact"] = identity(bundle[1])
    with pytest.raises(ValueError, match="already applied"):
        contract.validate_bundle(bundle)


def test_reported_validation_is_a_declaration_bound_to_other_acquisitions():
    bundle = records()
    independent = deepcopy(bundle[0])
    independent["id"] = "validation-acquisition"
    independent["provenance"]["session_id"] = "separate-session"
    calibration = bundle[1]
    calibration["validation"] = {
        "status": "reported", "acquisitions": [identity(independent)],
        "report": {"path": "evidence/validation.json", "sha256": "e" * 64, "size_bytes": 12}}
    contract.validate_bundle([bundle[0], independent, calibration])
    calibration["validation"]["acquisitions"] = [identity(bundle[0])]
    with pytest.raises(ValueError, match="distinct acquisition"):
        contract.validate_contract(calibration)


def independent_replay_bundle():
    bundle = records()
    replay = deepcopy(bundle[0])
    replay["id"] = "separate-replay"
    replay["provenance"]["session_id"] = "separate-session"
    bundle[3]["acquisition"] = identity(replay)
    bundle[3]["clock_mappings"] = []
    bundle[3]["operations"] = []
    bundle[3]["outputs"][2]["clock_id"] = "imu-clock"
    return [bundle[0], replay, bundle[1], bundle[3]]


def test_calibration_can_be_supplied_to_an_independent_recording_of_same_rig():
    bundle = independent_replay_bundle()
    contract.validate_bundle(bundle)
    assert bundle[-1]["acquisition"] != bundle[-2]["acquisition"]


@pytest.mark.parametrize("path,replacement", [
    (("rig", "revision"), "remounted-v2"), (("rig", "id"), "another-rig"),
    (("streams", 0, "sensor_instance"), "replacement-camera"),
    (("streams", 0, "camera", "resolution"), [320, 240]),
    (("frames", 1, "axes"), ["x-backward", "y-right", "z-up"]),
])
def test_calibration_transfer_rejects_changed_rig_sensor_resolution_or_axes(path, replacement):
    bundle = independent_replay_bundle()
    set_path(bundle[1], path, replacement)
    bundle[3]["acquisition"] = identity(bundle[1])
    with pytest.raises(ValueError):
        contract.validate_bundle(bundle)


def test_descriptor_checks_never_open_or_hash_referenced_files(monkeypatch):
    bundle = records()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("Referenced file must not be opened."))
    contract.validate_bundle(bundle)
    assert contract.assess_acquisition(bundle[0])["reference_coverage_verification"] == "not_performed"


def test_cli_requires_no_third_party_packages_and_reads_only_descriptor(tmp_path):
    path = tmp_path / "acquisition.json"
    path.write_text(json.dumps(acquisition()), encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-S", "-m", "meridian.sensor_contract", str(path)],
                            capture_output=True, text=True, env=environment, timeout=10)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["sha256"] == contract.contract_sha256(acquisition())
    assert summary["payload_verification"] == "not_performed"
    assert summary["execution_readiness"] == "not_assessed"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["acquisition.json"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_payload_encoding_cannot_change_implicit_json_parser_encoding(encoding):
    with pytest.raises(ValueError):
        contract.loads_contract(json.dumps(acquisition()).encode(encoding))


def test_candidate_profiles_check_metadata_topology_without_claiming_readiness():
    bundle = records()
    bundle[-1]["selections"] = bundle[-1]["selections"][2:]
    bundle[-1]["outputs"] = bundle[-1]["outputs"][2:]
    with pytest.raises(ValueError, match="Stereo candidate"):
        contract.validate_bundle(bundle)
    bundle[-1]["target_profile"] = "imu_snapshot_candidate_v1"
    with pytest.raises(ValueError, match="Snapshot candidate"):
        contract.validate_bundle(bundle)
    bundle[0]["streams"][2]["measurement_semantics"] = "filtered_snapshot"
    bundle[1]["acquisition"] = identity(bundle[0])
    bundle[2]["acquisition"] = identity(bundle[0])
    bundle[-1]["acquisition"] = identity(bundle[0])
    bundle[-1]["calibration"] = identity(bundle[1])
    bundle[-1]["clock_mappings"] = [identity(bundle[2])]
    bundle[-1]["operations"][0]["artifact"] = identity(bundle[2])
    contract.validate_bundle(bundle)
    assert contract.assess_acquisition(bundle[0])["execution_readiness"] == "not_assessed"


def test_mapping_must_cover_selected_source_segment_interval():
    bundle = records()
    bundle[2]["last_source_ns"] = str(STAMP + 99)
    bundle[3]["clock_mappings"] = [identity(bundle[2])]
    bundle[3]["operations"][0]["artifact"] = identity(bundle[2])
    with pytest.raises(ValueError, match="does not cover"):
        contract.validate_bundle(bundle)
    bundle = records()
    bundle[3]["selections"] = bundle[3]["selections"][:2]
    bundle[3]["outputs"] = bundle[3]["outputs"][:2]
    # Topology already rejects removal of the only selected IMU.
    with pytest.raises(ValueError):
        contract.validate_bundle(bundle)


def test_overlapping_clock_mappings_cannot_silently_form_a_correction_chain():
    bundle = records()
    second = deepcopy(bundle[2])
    second["id"] = "other-offset"
    second["purpose"] = "sensor_time_offset"
    bundle[3]["clock_mappings"].append(identity(second))
    with pytest.raises(ValueError, match="overlap"):
        contract.validate_bundle([*bundle, second])


def rebind_source(bundle):
    for item in bundle[1:]:
        item["acquisition"] = identity(bundle[0])
    bundle[3]["calibration"] = identity(bundle[1])
    bundle[3]["clock_mappings"] = [identity(bundle[2])]
    bundle[3]["operations"][0]["artifact"] = identity(bundle[2])


def test_stereo_candidate_rejects_different_camera_crops():
    bundle = records()
    bundle[3]["selections"][1]["first_tick"] = str(STAMP + 1)
    contract.validate_contract(bundle[3])
    with pytest.raises(ValueError, match="Stereo candidate selections"):
        contract.validate_bundle(bundle)


def test_stereo_pairing_uses_segment_positions_not_matching_local_names():
    bundle = records()
    bundle[0]["streams"][1]["segments"][0]["id"] = "right-camera-segment"
    bundle[3]["selections"][1]["segment_id"] = "right-camera-segment"
    rebind_source(bundle)
    contract.validate_bundle(bundle)
    for camera in bundle[0]["streams"][:2]:
        reset = deepcopy(camera["segments"][0])
        reset["id"] = "after-reset"
        camera["segments"].append(reset)
    bundle[3]["selections"][1]["segment_id"] = "after-reset"
    rebind_source(bundle)
    # Equal ticks after a reset still belong to a different physical segment.
    with pytest.raises(ValueError, match="paired segment positions"):
        contract.validate_bundle(bundle)
    bundle[3]["selections"][0]["segment_id"] = "after-reset"
    contract.validate_bundle(bundle)


def test_duplicate_quantity_conversion_cannot_change_an_irrelevant_parameter():
    prepared = records()[3]
    first = operation("unit_conversion", "imu0", {"quantity": "angular_velocity", "source_unit": "deg/s",
                      "target_unit": "rad/s", "local_gravity_m_s2": None})
    second = deepcopy(first)
    second["parameters"]["local_gravity_m_s2"] = 9.81
    prepared["operations"] = [first, second]
    with pytest.raises(ValueError, match="Repeated preparation operation"):
        contract.validate_contract(prepared)


@pytest.mark.parametrize("path,replacement", [
    (("outputs",), []),
    (("outputs", 0, "stream_id"), "unselected-camera"),
    (("outputs", 0, "index_path"), "sensors/missing.csv"),
    (("outputs", 0, "timestamp_unit"), "us"),
    (("files", 0, "sha256"), "invalid"),
])
def test_prepared_outputs_require_selected_identity_inventory_and_ns(path, replacement):
    prepared = records()[3]
    set_path(prepared, path, replacement)
    with pytest.raises(ValueError):
        contract.validate_contract(prepared)


@pytest.mark.parametrize("path,replacement", [
    (("outputs", 0, "frame_id"), "body"),
    (("outputs", 2, "clock_id"), "imu-clock"),
    (("files", 0, "role"), "reference"),
])
def test_prepared_output_conventions_and_roles_are_cross_checked(path, replacement):
    bundle = records()
    set_path(bundle[3], path, replacement)
    contract.validate_contract(bundle[3])
    with pytest.raises(ValueError):
        contract.validate_bundle(bundle)


def test_prepared_imu_output_requires_explicit_non_si_conversion():
    bundle = records()
    bundle[0]["streams"][2]["imu"]["angular_velocity_unit"] = "deg/s"
    rebind_source(bundle)
    with pytest.raises(ValueError, match="canonical SI"):
        contract.validate_bundle(bundle)
    bundle[3]["operations"].append(operation(
        "unit_conversion", "imu0", {"quantity": "angular_velocity", "source_unit": "deg/s",
                                  "target_unit": "rad/s", "local_gravity_m_s2": None}))
    contract.validate_bundle(bundle)


def test_changed_clock_requires_complete_mapping_of_selected_reset_segments():
    bundle = records()
    bundle[0]["streams"][2]["segments"].append({
        "id": "after-reset", "first_tick": "0", "last_tick": "100", "sample_count": 3})
    bundle[3]["selections"].append({
        "stream_id": "imu0", "segment_id": "after-reset", "first_tick": "0", "last_tick": "100"})
    rebind_source(bundle)
    with pytest.raises(ValueError, match="every selected interval and reset segment"):
        contract.validate_bundle(bundle)
    after_reset = deepcopy(bundle[2])
    after_reset.update({"id": "after-reset-mapping", "source_segment_id": "after-reset",
                        "source_origin_ns": "0", "first_source_ns": "0", "last_source_ns": "100"})
    bundle[3]["clock_mappings"].append(identity(after_reset))
    bundle[3]["operations"].append(operation("clock_mapping", "imu0", {}, identity(after_reset)))
    contract.validate_bundle([*bundle, after_reset])


@pytest.mark.parametrize("second_start", [20, 50])
def test_a_stream_segment_cannot_have_overlapping_or_disjoint_selections(second_start):
    prepared = records()[3]
    prepared["selections"][0]["last_tick"] = str(STAMP + 40)
    second = deepcopy(prepared["selections"][0])
    second.update(first_tick=str(STAMP + second_start), last_tick=str(STAMP + 100))
    prepared["selections"].append(second)
    with pytest.raises(ValueError, match="one contiguous selection per stream segment"):
        contract.validate_contract(prepared)


def test_selected_reset_segments_keep_source_order_with_other_streams_interleaved():
    bundle = records()
    bundle[0]["streams"][2]["segments"].append({
        "id": "after-reset", "first_tick": "0", "last_tick": "100", "sample_count": 3})
    rebind_source(bundle)
    prepared = bundle[3]
    prepared["clock_mappings"], prepared["operations"] = [], []
    prepared["outputs"][2]["clock_id"] = "imu-clock"
    cam0, cam1, imu_before = prepared["selections"]
    imu_after = {"stream_id": "imu0", "segment_id": "after-reset", "first_tick": "0", "last_tick": "100"}
    prepared["selections"] = [imu_before, cam0, imu_after, cam1]
    contract.validate_bundle(bundle)
    # Sorting by tick value would reorder a reset; source sequence wins.
    prepared["selections"] = [imu_after, cam0, imu_before, cam1]
    contract.validate_contract(prepared)
    with pytest.raises(ValueError, match="original segment order"):
        contract.validate_bundle(bundle)
