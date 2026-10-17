"""Reject unsupported experiments and bind the profile to qualified evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from meridian.vio_specification import (
    DATASET_ID, build_specification, configuration_profile,
    specification_sha256, validate_specification,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("points", [100, 200, 400])
def test_supported_specs_are_complete_canonical_and_independent(points):
    supplied = {"num_pts": points}
    spec = build_specification(DATASET_ID, supplied)
    validate_specification(spec)
    assert supplied == {"num_pts": points}
    assert spec["parameters"] == supplied
    assert len(specification_sha256(spec)) == 64
    assert specification_sha256(json.loads(json.dumps(spec))) == specification_sha256(spec)
    assert specification_sha256(dict(reversed(list(spec.items())))) == specification_sha256(spec)
    spec["configuration"]["rendered_files"]["estimator_config.yaml"]["sha256"] = "changed"
    fresh = build_specification(DATASET_ID, supplied)
    assert fresh["configuration"]["rendered_files"]["estimator_config.yaml"]["sha256"] != "changed"
    assert fresh["configuration"]["baseline_files"]["estimator_config.yaml"]["sha256"] != "changed"


def test_default_is_applied_only_when_building_not_when_validating():
    default = build_specification(DATASET_ID, {})
    assert default == build_specification(DATASET_ID, {"num_pts": 200})
    del default["parameters"]["num_pts"]
    with pytest.raises(ValueError, match="explicitly"):
        validate_specification(default)


@pytest.mark.parametrize("parameters", [None, [], "200", {"num_pts": True}, {"num_pts": 200.0},
                                        {"num_pts": "200"}, {"num_pts": None}, {"num_pts": 99},
                                        {"num_pts": 300}, {"num_pts": 401}, {"num_pts": float("nan")},
                                        {"num_pts": float("inf")}, {"num_pts": [200]},
                                        {"num_pts": 200, "image": "untrusted"}])
def test_builder_rejects_unknown_parameters_wrong_types_and_unlisted_values(parameters):
    with pytest.raises(ValueError):
        build_specification(DATASET_ID, parameters)


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 0, 2])
def test_builder_rejects_unknown_or_inexact_schema_version(version):
    with pytest.raises(ValueError, match="schema_version"):
        build_specification(DATASET_ID, {}, schema_version=version)


@pytest.mark.parametrize("dataset", [None, 1, "room1", "tumvi-room2-512-16-v1", "../room1", "https://example.invalid/room1"])
def test_builder_rejects_other_datasets_or_resource_addresses(dataset):
    with pytest.raises(ValueError, match="dataset"):
        build_specification(dataset, {})


@pytest.mark.parametrize("path,value", [
    (("schema_version",), True), (("schema_version",), 1.0),
    (("profile_id",), "another-profile"),
    (("dataset", "prepared_manifest_sha256"), "0" * 64),
    (("dataset", "reference_use"), "initialization"),
    (("interval", "mode"), "crop"),
    (("interval", "first_camera_timestamp_ns"), 1520530308199447626),
    (("interval", "camera_pairs"), 2821.0),
    (("estimator", "adapter_protocol_version"), True),
    (("estimator", "upstream_revision"), "main"),
    (("runtime", "image_id"), "untrusted:latest"),
    (("runtime", "version", "adapter_sha256"), "0" * 64),
    (("configuration", "rendering_version"), 1.0),
    (("configuration", "operation"), "run_shell"),
    (("scheduling", "wall_clock_pacing"), 0),
    (("conventions", "orientation"), "world_to_body"),
    (("resources", "memory_mb"), 4096.0),
    (("resources", "network"), "host"),
    (("evaluation", "maximum_reference_bracket_ns"), 20000000.0),
    (("evaluation", "evaluator_sha256"), "0" * 64),
    (("evaluation", "time_offset_fitting"), 0),
    (("reproducibility", "opencv_threads"), True),
    (("parameters", "num_pts"), 400),
])
def test_validation_rejects_changed_pins_inexact_types_or_unrendered_parameter_change(path, value):
    spec = build_specification(DATASET_ID, {})
    node = spec
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    for operation in (validate_specification, specification_sha256):
        with pytest.raises(ValueError):
            operation(spec)


@pytest.mark.parametrize("operation", ["delete", "extra"])
def test_validation_requires_all_and_only_the_immutable_fields(operation):
    for key in build_specification(DATASET_ID, {}):
        spec = build_specification(DATASET_ID, {})
        if operation == "delete":
            del spec[key]
        elif type(spec[key]) is dict:
            spec[key]["unsupported"] = "value"
        else:
            spec["unsupported"] = "value"
        with pytest.raises(ValueError):
            validate_specification(spec)


def test_only_parameter_and_derived_rendered_hash_change_between_choices():
    values = [build_specification(DATASET_ID, {"num_pts": n}) for n in (100, 200, 400)]
    assert len({specification_sha256(spec) for spec in values}) == 3
    for spec in values:
        spec.pop("parameters")
        spec["configuration"].pop("rendered_files")
    assert values[0] == values[1] == values[2]


def test_profile_metadata_is_fresh_and_describes_actual_parameter_semantics():
    profile = configuration_profile()
    feature = profile["parameters"]["num_pts"]
    assert feature["choices"] == [100, 200, 400]
    assert feature["default"] == 200
    assert "two cameras after initialization" in feature["unit"]
    assert "initialization uses its separate" in feature["description"]
    feature["choices"].append(800)
    assert configuration_profile()["parameters"]["num_pts"]["choices"] == [100, 200, 400]


def test_pins_match_public_qualification_and_existing_source_files():
    spec = build_specification(DATASET_ID, {})
    summary = json.loads((ROOT / "results/vio-reference/summary.json").read_text())
    assert spec["dataset"]["prepared_manifest_sha256"] == summary["prepared_manifest_sha256"]
    assert spec["dataset"]["archive_sha256"] == summary["dataset"]["source"]["sha256"]
    assert spec["interval"]["camera_pairs"] == summary["dataset"]["streams"]["cam0"]["samples"]
    assert spec["interval"]["imu_samples"] == summary["dataset"]["streams"]["imu0"]["samples"]
    assert spec["evaluation"]["qualification_criteria"] == summary["qualification_criteria"]
    for name, identity in spec["configuration"]["baseline_files"].items():
        assert identity == summary["configuration_files"]["config/" + name]
        assert hashlib.sha256((ROOT / "integrations/openvins/config" / name).read_bytes()).hexdigest() == identity["sha256"]
    assert hashlib.sha256((ROOT / "integrations/openvins/runner.cpp").read_bytes()).hexdigest() == spec["estimator"]["adapter_sha256"]
    assert hashlib.sha256((ROOT / "src/meridian/vio_evaluation.py").read_bytes()).hexdigest() == spec["evaluation"]["evaluator_sha256"]


def test_contract_can_run_without_installed_third_party_packages():
    script = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from meridian.vio_specification import *; "
              "specification_sha256(build_specification(DATASET_ID, {})); "
              "assert 'numpy' not in sys.modules and 'django' not in sys.modules")
    subprocess.run([sys.executable, "-S", "-c", script, str(ROOT / "src")], check=True, timeout=10)
