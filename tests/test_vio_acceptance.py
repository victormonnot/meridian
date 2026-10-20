"""Check fixed held-out execution and failure retention without benchmark data."""

from copy import deepcopy
import hashlib
import json
import subprocess

import pytest

from meridian import vio_acceptance as acceptance
from meridian.vio_specification import DATASET_ID, build_specification, specification_sha256


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("{}")
    baseline = build_specification(DATASET_ID, {"num_pts": 200})
    manifest = {
        "dataset_id": acceptance.dataset.ROOM2_DATASET_ID,
        "files": {
            **{f"config/{name}": value for name, value in baseline["configuration"]["baseline_files"].items()},
            "reference/trajectory.csv": {"sha256": "a" * 64, "size_bytes": 1},
        },
        "source": dict(acceptance.dataset.ROOM2),
        "streams": {},
    }
    provenance = {"binary-sha256.txt": "fixture binary\n", "dependency-versions.txt": "fixture dependencies\n"}
    baseline["runtime"]["provenance_sha256"] = {
        name: hashlib.sha256(value.encode()).hexdigest() for name, value in provenance.items()
    }
    monkeypatch.setattr(acceptance, "build_specification", lambda *args: deepcopy(baseline))
    monkeypatch.setattr(acceptance.dataset, "verify_prepared", lambda _: deepcopy(manifest))
    monkeypatch.setattr(acceptance.reference, "_image_identity", lambda _: acceptance.CONFIGURATION_ID)
    monkeypatch.setattr(acceptance.reference, "_runtime_version", lambda *_: deepcopy(baseline["runtime"]["version"]))
    monkeypatch.setattr(acceptance.reference, "_runtime_provenance", lambda *_: dict(provenance))
    return prepared, manifest, baseline, provenance


def test_preflight_checks_original_sources_and_returns_exact_runtime(preflight):
    prepared, manifest, _, _ = preflight
    observed, identity = acceptance._preflight(prepared)
    assert observed == manifest
    assert identity["configuration_image_id"] == acceptance.CONFIGURATION_ID
    assert identity["execution_image_id"] == acceptance.CONFIGURATION_ID


@pytest.mark.parametrize("fault", ["room1", "configuration", "runtime", "version", "dependencies", "runner", "evaluator", "gates"])
def test_preflight_rejects_changed_inputs_before_estimation(preflight, monkeypatch, fault):
    prepared, manifest, baseline, provenance = preflight
    if fault == "room1":
        manifest["dataset_id"] = acceptance.dataset.DATASET_ID
    elif fault == "configuration":
        manifest["files"]["config/estimator_config.yaml"] = {"sha256": "b" * 64, "size_bytes": 3689}
    elif fault == "runtime":
        monkeypatch.setattr(acceptance.reference, "_image_identity", lambda _: "sha256:" + "a" * 64)
    elif fault == "version":
        monkeypatch.setattr(acceptance.reference, "_runtime_version", lambda *_: {"protocol_version": 1})
    elif fault == "dependencies":
        provenance["dependency-versions.txt"] += "different\n"
    elif fault == "runner":
        monkeypatch.setattr(acceptance, "FROZEN_RUNNER_SHA256", "a" * 64)
    elif fault == "evaluator":
        monkeypatch.setattr(acceptance, "FROZEN_EVALUATOR_SHA256", "a" * 64)
    else:
        baseline["evaluation"]["qualification_criteria"]["minimum_evaluated_samples"] = 1
    with pytest.raises(ValueError):
        acceptance._preflight(prepared)


def _recorded_run(directory, *, passed=True, state="complete"):
    directory.mkdir()
    result = {
        "state": state,
        "run_spec_sha256": "a" * 64,
        "qualification": {"passed": passed},
        "artifacts": {},
    }
    (directory / "result.json").write_text(json.dumps(result))
    return result


def test_exact_three_attempts_use_frozen_image_and_no_parameter_override(preflight, monkeypatch, tmp_path):
    prepared, _, _, _ = preflight
    calls = []

    def run(path, image, output):
        calls.append((path, image, output))
        return _recorded_run(output)

    monkeypatch.setattr(acceptance.reference, "run_reference", run)
    monkeypatch.setattr(acceptance.reference, "compare_repeats",
                        lambda dirs: {"passed": len(dirs) == 3, "run_count": len(dirs)})
    report = acceptance.run_acceptance(prepared, tmp_path / "acceptance")
    assert report["passed"] is True
    assert len(calls) == 3
    assert all(image == acceptance.CONFIGURATION_ID for _, image, _ in calls)
    assert [output.name for _, _, output in calls] == ["run-1", "run-2", "run-3"]
    assert report == json.loads((tmp_path / "acceptance/acceptance-report.json").read_text())
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.parametrize("failure", ["gate", "exception", "timeout"])
def test_failed_attempt_is_retained_with_no_replacement(preflight, monkeypatch, tmp_path, failure):
    prepared, _, _, _ = preflight
    calls = []

    def run(path, image, output):
        calls.append(output)
        if output.name == "run-2":
            _recorded_run(output, passed=False, state="complete" if failure == "gate" else "failed")
            if failure == "exception":
                raise ValueError(f"private path {tmp_path}")
            if failure == "timeout":
                raise subprocess.TimeoutExpired("fixture", 900)
        else:
            _recorded_run(output)

    monkeypatch.setattr(acceptance.reference, "run_reference", run)
    monkeypatch.setattr(acceptance.reference, "compare_repeats", lambda _: {"passed": True})
    report = acceptance.run_acceptance(prepared, tmp_path / "acceptance")
    assert report["state"] == "complete" and report["passed"] is False
    assert len(calls) == 3 and len(report["attempts"]) == 3
    assert [item["passed"] for item in report["attempts"]] == [True, False, True]
    assert str(tmp_path) not in json.dumps(report)
    if failure != "gate":
        assert report["repeatability"] is None


def test_interrupted_attempt_stops_and_retains_incomplete_report(preflight, monkeypatch, tmp_path):
    prepared, _, _, _ = preflight
    calls = []

    def run(path, image, output):
        calls.append(output)
        _recorded_run(output, passed=False, state="failed")
        raise KeyboardInterrupt()

    monkeypatch.setattr(acceptance.reference, "run_reference", run)
    with pytest.raises(KeyboardInterrupt):
        acceptance.run_acceptance(prepared, tmp_path / "acceptance")
    report = json.loads((tmp_path / "acceptance/acceptance-report.json").read_text())
    assert len(calls) == 1 and report["state"] == "incomplete" and report["passed"] is False
    assert report["attempts"][0]["error_type"] == "KeyboardInterrupt"


def test_invalid_repeat_evidence_retains_explicit_failed_comparison(preflight, monkeypatch, tmp_path):
    prepared, _, _, _ = preflight
    monkeypatch.setattr(acceptance.reference, "run_reference", lambda path, image, output: _recorded_run(output))

    def invalid(_):
        raise ValueError(f"private evidence path {tmp_path}")

    monkeypatch.setattr(acceptance.reference, "compare_repeats", invalid)
    report = acceptance.run_acceptance(prepared, tmp_path / "acceptance")
    assert report["state"] == "complete" and report["passed"] is False
    assert report["repeatability"] == {"passed": False, "error_type": "ValueError"}
    assert str(tmp_path) not in json.dumps(report)


def test_changed_manifest_prevents_further_execution_and_fails_first_attempt(preflight, monkeypatch, tmp_path):
    prepared, _, _, _ = preflight
    calls = []

    def run(path, image, output):
        calls.append(output)
        _recorded_run(output)
        (prepared / "manifest.json").write_text('{"changed": true}')

    monkeypatch.setattr(acceptance.reference, "run_reference", run)
    report = acceptance.run_acceptance(prepared, tmp_path / "acceptance")
    assert len(calls) == 1 and report["passed"] is False
    assert len(report["attempts"]) == 3
    assert all(not attempt["passed"] for attempt in report["attempts"])
    assert report["repeatability"] is None


def test_existing_dangling_or_nested_output_is_refused_before_runtime(preflight, monkeypatch, tmp_path):
    prepared, _, _, _ = preflight
    monkeypatch.setattr(acceptance, "_preflight", lambda _: pytest.fail("must not inspect runtime"))
    existing = tmp_path / "existing"
    existing.mkdir()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    for output in (existing, dangling):
        with pytest.raises(FileExistsError):
            acceptance.run_acceptance(prepared, output)
    with pytest.raises(ValueError, match="outside"):
        acceptance.run_acceptance(prepared, prepared / "nested")


def test_room1_specification_identities_remain_frozen():
    assert {
        points: specification_sha256(build_specification(DATASET_ID, {"num_pts": points}))
        for points in (100, 200, 400)
    } == {
        100: "98e780babbc5fd1b8214a3afbe65e5b5dbe39196017a83361b93f7f7cd3e5f73",
        200: "521227054edeac5226ec9ab25e6a9190475affc62c64ce96057b5402bbf73677",
        400: "d8c72388ae4b45d32c92f4cf2af978069b829e2873410362192f567ecfc8e520",
    }


@pytest.mark.parametrize("argument", ["--num-pts", "--image", "--timeout-s", "--runs"])
def test_acceptance_cli_does_not_offer_tuning_or_replacement_controls(argument):
    with pytest.raises(SystemExit) as error:
        acceptance.main(["run", "--prepared", "unused", "--output", "unused", argument, "100"])
    assert error.value.code == 2
