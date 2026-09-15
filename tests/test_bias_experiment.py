"""Check bias-study evidence using regression or exploratory inputs only."""

from copy import deepcopy
from dataclasses import fields, replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from meridian import bias_experiment as study
from meridian.cpp_parity import compare_traces, read_cpp_trace
from meridian.stress_evaluation import evaluate_scenario
from meridian.stress_scenarios import SCENARIOS


CASES = ("nominal", "bias_ramp")
SETTINGS = ("0", "0.03", "0.1")


@pytest.fixture(scope="module")
def binary():
    value = os.environ.get("MERIDIAN_CPP_BINARY")
    if not value:
        pytest.skip("C++ integration requires MERIDIAN_CPP_BINARY; see docs/cpp-ekf.md")
    path = Path(value).resolve()
    assert path.is_file(), f"configured C++ executable is missing: {path}"
    return path


@pytest.fixture(scope="module")
def trials(binary):
    # Root seed 42 is a regression case and is excluded from reported phases.
    return {scenario.name: study.evaluate_trial(scenario, 42, binary)
            for scenario in study.STUDY_SCENARIOS}


def test_protocol_retains_fixed_grid_and_distinct_final_seed_reserve():
    assert tuple(s.name for s in study.STUDY_SCENARIOS) == CASES
    assert study.BIAS_STDS_DEG_S_PER_SQRT_S == (0.0, 0.03, 0.1)
    assert study.PHASE_SEEDS == {
        "exploration": tuple(range(20)), "evaluation": tuple(range(2000, 2020)),
    }
    reserved = set(study.PHASE_SEEDS["evaluation"])
    assert not reserved.intersection(study.PHASE_SEEDS["exploration"])
    assert not reserved.intersection(range(1000, 1020))
    assert 42 not in set().union(*map(set, study.PHASE_SEEDS.values()))
    spec = study.protocol()
    assert spec["reference_setting"] == "0"
    assert spec["assumed_accel_std_m_s2"] == 0.2
    assert spec["closed_windows_s"] == {"ramp": [10.0, 20.0], "late": [25.0, 30.0]}
    assert spec["accuracy_thresholds"] is None


@pytest.mark.parametrize("name", CASES)
def test_constant_setting_exactly_reproduces_existing_controlled_reference(name, trials):
    scenario = next(s for s in study.STUDY_SCENARIOS if s.name == name)
    expected = evaluate_scenario(scenario, 42)
    trial = trials[name]
    actual = trial["settings"]["0"]
    assert tuple(trial["settings"]) == SETTINGS
    for field in ("states", "covariance", "nis"):
        np.testing.assert_array_equal(actual[field], expected[field]["ekf"])
    for metric, value in expected["metrics"]["ekf"].items():
        assert actual["metrics"][metric] == value
    for attribute in ("gyro_rate_rad_s", "force_yz_m_s2", "observation_indices"):
        np.testing.assert_array_equal(getattr(trial["data"], attribute),
                                      getattr(expected["data"], attribute))


def root_mean_square(values):
    return math.sqrt(math.fsum(float(value)**2 for value in values) / len(values))


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("setting", SETTINGS)
def test_metrics_use_closed_windows_centered_fluctuation_and_joint_nis(name, setting, trials):
    trial = trials[name]
    data, result = trial["data"], trial["settings"][setting]
    times = data.truth.time_s
    late = (times >= 25.0) & (times <= 30.0)
    ramp = (times >= 10.0) & (times <= 20.0)
    assert len(times) == 3001
    assert np.count_nonzero(late) == 501 and np.count_nonzero(ramp) == 1001
    assert result["states"].shape == (3001, 2)
    angle = np.rad2deg(result["states"][:, 0] - data.truth.angle_rad)
    bias = np.rad2deg(result["states"][:, 1] - data.bias_rad_s)
    late_mean = math.fsum(map(float, bias[late])) / 501
    integral = math.fsum(float(dt) * (float(left)**2 + float(right)**2) / 2.0
                         for dt, left, right in zip(np.diff(times), angle[:-1], angle[1:]))
    expected = {
        "angle_rmse_deg": root_mean_square(angle),
        "angle_time_weighted_rmse_deg": math.sqrt(integral / 30.0),
        "late_angle_rmse_deg": root_mean_square(angle[late]),
        "max_abs_angle_error_deg": max(abs(float(e)) for e in angle),
        "final_angle_error_deg": float(angle[-1]),
        "bias_rmse_deg_s": root_mean_square(bias),
        "ramp_angle_rmse_deg": root_mean_square(angle[ramp]),
        "ramp_bias_rmse_deg_s": root_mean_square(bias[ramp]),
        "late_bias_rmse_deg_s": root_mean_square(bias[late]),
        "late_bias_mean_error_deg_s": late_mean,
        "late_bias_temporal_std_deg_s": root_mean_square(bias[late] - late_mean),
        "max_abs_bias_error_deg_s": max(abs(float(e)) for e in bias),
        "final_bias_error_deg_s": float(bias[-1]),
    }
    correction_rows = result["python_trace"][np.array(trial["operations"]) == "update"]
    assert correction_rows.shape == (300, 12)
    vy, vz = correction_rows[:, 6:8].T
    syy, syz, szy, szz = correction_rows[:, 8:12].T
    # Explicit 2 x 2 inverse provides an independent check of the joint score.
    nis = (szz * vy**2 - (syz + szy) * vy * vz + syy * vz**2) / (syy * szz - syz * szy)
    np.testing.assert_allclose(result["nis"], nis, rtol=3e-13, atol=1e-14)
    expected["mean_nis"] = math.fsum(map(float, nis)) / 300
    assert result["metrics"] == pytest.approx(expected, rel=3e-13, abs=1e-14)
    assert bias[0] == pytest.approx(-0.5)
    assert expected["late_bias_rmse_deg_s"]**2 == pytest.approx(
        late_mean**2 + expected["late_bias_temporal_std_deg_s"]**2, rel=3e-13, abs=1e-15,
    )


def test_case_pairing_and_pre_ramp_inputs_are_preserved(trials):
    nominal, ramp = (trials[name]["data"] for name in CASES)
    assert nominal.stream_seeds == ramp.stream_seeds
    np.testing.assert_array_equal(nominal.truth.time_s, ramp.truth.time_s)
    np.testing.assert_array_equal(nominal.force_yz_m_s2, ramp.force_yz_m_s2)
    before_ramp = nominal.truth.time_s[1:] <= 10.0
    np.testing.assert_array_equal(nominal.gyro_rate_rad_s[before_ramp],
                                  ramp.gyro_rate_rad_s[before_ramp])
    np.testing.assert_allclose(ramp.gyro_rate_rad_s - nominal.gyro_rate_rad_s,
                               ramp.interval_bias_rad_s - nominal.interval_bias_rad_s,
                               rtol=2e-13, atol=1e-16)


def test_generate_once_and_send_same_immutable_inputs_to_every_setting_and_language(binary, monkeypatch, trials):
    data = deepcopy(trials["nominal"]["data"])
    arrays = [getattr(data, field.name) for field in fields(data)
              if isinstance(getattr(data, field.name), np.ndarray)]
    arrays.extend(getattr(data.truth, field.name) for field in fields(data.truth))
    before = [array.copy() for array in arrays]
    for array in arrays:
        array.setflags(write=False)
    generated, python_inputs, native_inputs = [], [], []
    real_python, real_run = study.python_trace, study.subprocess.run

    def generate(scenario, seed):
        generated.append((scenario.name, seed))
        assert seed == 42
        return data

    def record_python(config, serialized):
        python_inputs.append((config.copy(), serialized))
        return real_python(config, serialized)

    def record_native(command, **kwargs):
        native_inputs.append(Path(command[command.index("--input") + 1]).read_text())
        return real_run(command, **kwargs)

    monkeypatch.setattr(study, "generate_scenario", generate)
    monkeypatch.setattr(study, "python_trace", record_python)
    monkeypatch.setattr(study.subprocess, "run", record_native)
    trial = study.evaluate_trial(study.STUDY_SCENARIOS[0], 42, binary)
    assert generated == [("nominal", 42)]
    assert len(python_inputs) == len(native_inputs) == 3
    assert all(serialized == trial["events"] for _, serialized in python_inputs)
    assert native_inputs == [trial["events"]] * 3
    for (config, _), setting in zip(python_inputs, SETTINGS):
        assert config.pop(study.BIAS_PARAMETER) == float(np.deg2rad(float(setting)))
        assert config == {key: value for key, value in trial["settings"]["0"]["configuration"].items()
                          if key != study.BIAS_PARAMETER}
    for original, actual in zip(before, arrays):
        np.testing.assert_array_equal(actual, original)


def test_hidden_angle_and_bias_truth_cannot_change_filter_operations(binary, monkeypatch, trials):
    reference = trials["nominal"]
    data = reference["data"]
    changed = replace(data, truth=replace(data.truth,
                      angle_rad=data.truth.angle_rad + 1.0,
                      interval_rate_rad_s=data.truth.interval_rate_rad_s + 2.0),
                      bias_rad_s=data.bias_rad_s + 3.0,
                      interval_bias_rad_s=data.interval_bias_rad_s + 4.0)
    monkeypatch.setattr(study, "generate_scenario", lambda scenario, seed: changed)
    actual = study.evaluate_trial(study.STUDY_SCENARIOS[0], 42, binary)
    assert actual["events"] == reference["events"]
    assert study._compact(actual, 42)["input_sha256"] == study._compact(reference, 42)["input_sha256"]
    for setting in SETTINGS:
        for field in ("states", "covariance", "nis", "python_trace", "cpp_trace"):
            np.testing.assert_array_equal(actual["settings"][setting][field],
                                          reference["settings"][setting][field])
        for metric in ("angle_rmse_deg", "bias_rmse_deg_s"):
            assert actual["settings"][setting]["metrics"][metric] != reference["settings"][setting]["metrics"][metric]


def test_compact_scores_retain_paired_setting_minus_constant_differences(trials):
    trial = trials["nominal"]
    compact = study._compact(trial, 42)
    assert compact["seed"] == 42
    assert compact["stream_seeds"] == trial["data"].stream_seeds
    assert compact["input_sha256"] == hashlib.sha256(trial["events"].encode()).hexdigest()
    reference = trial["settings"]["0"]["metrics"]
    for setting in SETTINGS:
        metrics = trial["settings"][setting]["metrics"]
        assert compact["settings"][setting]["metrics"] == metrics
        assert compact["settings"][setting]["delta_from_constant"] == {
            key: value - reference[key] for key, value in metrics.items()
        }


def test_aggregates_preserve_mixed_sign_pairs_and_descriptive_ranges():
    values = {"0": (3.0, 4.0, 7.0), "0.03": (1.0, 4.0, 10.0), "0.1": (4.0, 2.0, 6.0)}
    metrics = ("angle_rmse_deg", "bias_rmse_deg_s", "ramp_bias_rmse_deg_s",
               "late_bias_temporal_std_deg_s", "mean_nis")
    records = []
    for index in range(3):
        settings = {}
        for setting in SETTINGS:
            value, reference = values[setting][index], values["0"][index]
            settings[setting] = {
                "metrics": {name: value for name in metrics},
                "delta_from_constant": {name: value - reference for name in metrics},
            }
        records.append({"seed": index, "settings": settings})
    result = study._aggregates(records)
    for metric in metrics:
        assert result["0.03"]["metrics"][metric] == {"mean": 5.0, "min": 1.0, "max": 10.0}
        assert result["0.03"]["delta_from_constant"][metric] == pytest.approx(
            {"mean": 1.0 / 3.0, "min": -2.0, "max": 3.0},
        )
        assert result["0"]["delta_from_constant"][metric] == {"mean": 0.0, "min": 0.0, "max": 0.0}
    for metric in metrics[:3]:
        assert result["0.03"]["rmse_delta_counts"][metric] == {"negative": 1, "zero": 1, "positive": 1}
        assert result["0.1"]["rmse_delta_counts"][metric] == {"negative": 2, "zero": 0, "positive": 1}
        assert result["0"]["rmse_delta_counts"][metric] == {"negative": 0, "zero": 3, "positive": 0}
    assert set(result["0.03"]["rmse_delta_counts"]) == set(metrics[:3])


def test_invalid_phase_and_existing_outputs_are_rejected_before_work(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected requests must not run binaries or scenarios.")

    monkeypatch.setattr(study, "evaluate_trial", forbidden)
    monkeypatch.setattr(study.subprocess, "run", forbidden)
    absent_binary = tmp_path / "absent-binary"
    invalid = tmp_path / "invalid"
    with pytest.raises(ValueError, match="phase"):
        study.run_experiment(absent_binary, invalid, phase="unknown")
    assert not invalid.exists()
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("Preserve existing output.\n")
    with pytest.raises(FileExistsError):
        study.run_experiment(absent_binary, existing, phase="exploration")
    assert marker.read_text() == "Preserve existing output.\n"
    assert list(existing.iterdir()) == [marker]


def test_scenario_outside_fixed_study_is_rejected_before_generation(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Out-of-study scenarios must not generate inputs.")

    monkeypatch.setattr(study, "generate_scenario", forbidden)
    scenario = next(s for s in SCENARIOS if s.name == "accel_dropout")
    with pytest.raises(ValueError, match="outside"):
        study.evaluate_trial(scenario, 42, tmp_path / "absent-binary")


@pytest.mark.parametrize("failure", ["native_exit", "state_disagreement"])
def test_native_failure_aborts_experiment_without_publishing_partial_results(binary, monkeypatch, tmp_path, failure):
    monkeypatch.setitem(study.PHASE_SEEDS, "exploration", (0,))
    calls = []
    generate = study.generate_scenario

    def guarded_generate(scenario, seed):
        assert seed == 0
        calls.append((scenario.name, seed))
        return generate(scenario, seed)

    monkeypatch.setattr(study, "generate_scenario", guarded_generate)
    if failure == "native_exit":
        real_run = study.subprocess.run

        def reject_replay(command, **kwargs):
            if "--input" in command:
                return subprocess.CompletedProcess(command, 17, stdout="", stderr="Injected native failure")
            return real_run(command, **kwargs)

        monkeypatch.setattr(study.subprocess, "run", reject_replay)
        message = "C\\+\\+ replay failed"
    else:
        real_read = study.read_cpp_trace

        def corrupt_state(path, operations):
            values = real_read(path, operations)
            values[20, 0] += 0.01
            return values

        monkeypatch.setattr(study, "read_cpp_trace", corrupt_state)
        message = "Python/C\\+\\+ disagreement"
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match=message):
        study.run_experiment(binary, output, phase="exploration")
    assert calls == [("nominal", 0)]
    assert not output.exists()


@pytest.mark.parametrize("row,column,value", [
    (10, 2, -1.0), (10, 3, 1.0), (11, 8, -1.0), (11, 9, 1.0),
])
def test_matching_languages_cannot_hide_invalid_prior_or_innovation_covariance(binary, monkeypatch, row, column, value):
    real_python, real_native = study.python_trace, study.read_cpp_trace

    def corrupt_python(config, serialized):
        operations, values = real_python(config, serialized)
        # Row 10 is the prediction immediately before correction row 11;
        # it is intentionally absent from the physical-endpoint score arrays.
        assert operations[10:12] == ["predict", "update"]
        values[row, column] = value
        return operations, values

    def corrupt_native(path, operations):
        values = real_native(path, operations)
        values[row, column] = value
        return values

    monkeypatch.setattr(study, "python_trace", corrupt_python)
    monkeypatch.setattr(study, "read_cpp_trace", corrupt_native)
    with pytest.raises(ValueError, match="trace covariance"):
        study.evaluate_trial(study.STUDY_SCENARIOS[0], 42, binary)


@pytest.mark.parametrize("changed", ["source", "binary"])
def test_changed_execution_sources_are_rejected_before_any_export(binary, monkeypatch, tmp_path, trials, changed):
    monkeypatch.setitem(study.PHASE_SEEDS, "exploration", (0,))
    calls = []

    def cached_regression_trial(scenario, seed, executable):
        # Cached seed-42 traces suffice to test the publication guard. No
        # new numerical experiment or claimed seed-0 report is produced here.
        assert seed == 0 and executable == binary
        calls.append((scenario.name, seed))
        return trials[scenario.name]

    monkeypatch.setattr(study, "evaluate_trial", cached_regression_trial)
    if changed == "source":
        hashes = iter(({"ekf.py": "before"}, {"ekf.py": "after"}))
        monkeypatch.setattr(study, "_source_hashes", lambda: next(hashes))
    else:
        read_bytes = Path.read_bytes
        binary_reads = []

        def changed_binary_bytes(path):
            original = read_bytes(path)
            if path == binary:
                binary_reads.append(path)
                if len(binary_reads) == 2:
                    return original + b"changed during execution"
            return original

        monkeypatch.setattr(Path, "read_bytes", changed_binary_bytes)
    output = tmp_path / "changed"
    with pytest.raises(ValueError, match="changed during the experiment"):
        study.run_experiment(binary, output, phase="exploration")
    assert calls == [(name, 0) for name in CASES]
    assert not output.exists()


def test_exploratory_export_reproduces_all_files_and_complete_native_traces(binary, monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(study.PHASE_SEEDS, "exploration", (0,))
    evaluate = study.evaluate_trial
    captured, calls = {}, []

    def guarded_trial(scenario, seed, executable):
        assert seed == 0, "Export checks must not execute reserved evaluation seeds."
        trial = evaluate(scenario, seed, executable)
        calls.append((scenario.name, seed))
        captured[scenario.name] = trial
        return trial

    monkeypatch.setattr(study, "evaluate_trial", guarded_trial)
    first, second = tmp_path / "first", tmp_path / "second"
    summary = study.run_experiment(binary, first, phase="exploration")
    assert study.main(["--binary", str(binary), "--phase", "exploration", "--output", str(second)]) == 0
    assert "exploration: 1 seeds per case; every Python/C++ comparison passed." in capsys.readouterr().out
    assert calls == [(name, 0) for name in CASES] * 2
    assert summary == json.loads((first / "summary.json").read_text())
    assert summary == json.loads((second / "summary.json").read_text())
    files = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    assert len(files) == 22
    assert files == {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()}
    assert files[Path("overview.png")].startswith(b"\x89PNG\r\n\x1a\n")
    assert summary["phase"] == "exploration"
    assert summary["seeds"] == [0] and summary["representative_seed"] == 0
    assert summary["cpp_binary"]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    spec_bytes = json.dumps(summary["protocol"], sort_keys=True, allow_nan=False).encode()
    assert summary["protocol_sha256"] == hashlib.sha256(spec_bytes).hexdigest()
    for name, fingerprint in summary["source_sha256"].items():
        assert hashlib.sha256(Path(study.__file__).with_name(name).read_bytes()).hexdigest() == fingerprint

    for name in CASES:
        trial = captured[name]
        data, directory = trial["data"], first / name
        times = data.truth.time_s
        assert (directory / "events.csv").read_text() == trial["events"]
        assert summary["scenarios"][name]["trials"] == [study._compact(trial, 0)]
        for filename, expected in (
            ("truth.csv", np.column_stack((times, data.truth.angle_rad, data.bias_rad_s))),
            ("gyro_measurements.csv", np.column_stack((times[:-1], times[1:], data.gyro_rate_rad_s))),
            ("accel_measurements.csv", np.column_stack((times[data.observation_indices], data.force_yz_m_s2))),
        ):
            np.testing.assert_array_equal(np.genfromtxt(directory / filename, delimiter=",", skip_header=1), expected)
        assert len(trial["operations"]) == 3301
        assert trial["operations"].count("predict") == 3000
        assert trial["operations"].count("update") == 300
        for setting in SETTINGS:
            result = trial["settings"][setting]
            python_values = read_cpp_trace(directory / f"python-{setting}.csv", trial["operations"])
            native_values = read_cpp_trace(directory / f"cpp-{setting}.csv", trial["operations"])
            assert python_values.shape == native_values.shape == (3301, 12)
            np.testing.assert_array_equal(python_values, result["python_trace"])
            np.testing.assert_array_equal(native_values, result["cpp_trace"])
            np.testing.assert_array_equal(python_values[trial["endpoint_rows"], :2], result["states"])
            comparison = compare_traces(python_values, native_values)
            assert comparison["passed"] and comparison == result["comparison"]
            for metric, value in result["metrics"].items():
                assert summary["scenarios"][name]["aggregates"][setting]["metrics"][metric] == {
                    "mean": value, "min": value, "max": value,
                }
