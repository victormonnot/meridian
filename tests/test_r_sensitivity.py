"""Verify fixed covariance comparisons without consuming evaluation seeds."""

from copy import deepcopy
from dataclasses import fields, replace
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from meridian import r_sensitivity as study
from meridian.ekf import AngleBiasEKF
from meridian.stress_evaluation import evaluate_scenario


CASES = ("nominal", "accel_noise_mismatch")
SETTINGS = ("0.1", "0.2", "0.6")
ARRAY_FIELDS = ("states", "covariance", "innovations", "innovation_covariance", "nis")


@pytest.fixture(scope="module")
def trials():
    # Seed 42 is a regression case, outside both reported phases.
    return {scenario.name: study.evaluate_trial(scenario, 42)
            for scenario in study.STUDY_SCENARIOS}


def test_protocol_keeps_fixed_settings_and_disjoint_phase_seeds():
    assert tuple(scenario.name for scenario in study.STUDY_SCENARIOS) == CASES
    assert study.ASSUMED_STDS == (0.1, 0.2, 0.6)
    assert study.PHASE_SEEDS == {
        "exploration": tuple(range(20)),
        "evaluation": tuple(range(1000, 1020)),
    }
    assert 42 not in set().union(*map(set, study.PHASE_SEEDS.values()))


@pytest.mark.parametrize("name", CASES)
def test_default_setting_and_baselines_reproduce_existing_controlled_trial(name, trials):
    scenario = next(item for item in study.STUDY_SCENARIOS if item.name == name)
    expected = evaluate_scenario(scenario, 42)
    actual = trials[name]
    assert tuple(actual["settings"]) == SETTINGS
    for attribute in ("gyro_rate_rad_s", "force_yz_m_s2", "observation_indices"):
        np.testing.assert_array_equal(getattr(actual["data"], attribute),
                                      getattr(expected["data"], attribute))
    for field in ARRAY_FIELDS:
        np.testing.assert_array_equal(actual["settings"]["0.2"][field], expected[field]["ekf"])
    assert actual["settings"]["0.2"]["metrics"] == expected["metrics"]["ekf"]
    assert set(actual["baselines"]) == {"gyro", "complementary"}
    for name, baseline in actual["baselines"].items():
        np.testing.assert_array_equal(baseline["angles"], expected["estimates"][name])
        assert baseline["metrics"] == expected["metrics"][name]
        assert all("bias" not in key and "nis" not in key for key in baseline["metrics"])


def _independent_angle_metrics(errors, times):
    late = errors[(times >= 25.0) & (times <= 30.0)]
    integral = math.fsum(
        float(dt) * (float(left)**2 + float(right)**2) / 2.0
        for dt, left, right in zip(np.diff(times), errors[:-1], errors[1:])
    )
    return {
        "angle_rmse_deg": math.sqrt(math.fsum(float(e)**2 for e in errors) / len(errors)),
        "angle_time_weighted_rmse_deg": math.sqrt(integral / (times[-1] - times[0])),
        "late_angle_rmse_deg": math.sqrt(math.fsum(float(e)**2 for e in late) / len(late)),
        "max_abs_angle_error_deg": max(abs(float(e)) for e in errors),
        "final_angle_error_deg": float(errors[-1]),
    }


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("setting", SETTINGS)
def test_metrics_use_all_endpoints_fixed_late_window_and_joint_nis(name, setting, trials):
    trial = trials[name]
    data, result = trial["data"], trial["settings"][setting]
    times = data.truth.time_s
    late = (times >= 25.0) & (times <= 30.0)
    assert len(times) == 3001 and np.count_nonzero(late) == 501
    assert result["states"].shape == (3001, 2)
    errors = np.rad2deg(result["states"][:, 0] - data.truth.angle_rad)
    expected = _independent_angle_metrics(errors, times)
    bias_error = np.rad2deg(result["states"][:, 1] - data.bias_rad_s)
    expected.update({
        "bias_rmse_deg_s": math.sqrt(math.fsum(float(e)**2 for e in bias_error) / len(times)),
        "late_bias_rmse_deg_s": math.sqrt(
            math.fsum(float(e)**2 for e in bias_error[late]) / np.count_nonzero(late)
        ),
        "max_abs_bias_error_deg_s": max(abs(float(e)) for e in bias_error),
        "final_bias_error_deg_s": float(bias_error[-1]),
    })
    assert result["innovations"].shape == (300, 2)
    assert result["innovation_covariance"].shape == (300, 2, 2)
    vy, vz = result["innovations"].T
    s = result["innovation_covariance"]
    determinant = s[:, 0, 0] * s[:, 1, 1] - s[:, 0, 1] * s[:, 1, 0]
    independent_nis = (
        s[:, 1, 1] * vy**2 - (s[:, 0, 1] + s[:, 1, 0]) * vy * vz + s[:, 0, 0] * vz**2
    ) / determinant
    np.testing.assert_allclose(result["nis"], independent_nis, rtol=3e-13, atol=1e-14)
    expected["mean_nis"] = math.fsum(map(float, independent_nis)) / len(independent_nis)
    assert result["metrics"] == pytest.approx(expected, rel=3e-13, abs=1e-14)
    assert bias_error[0] == pytest.approx(-0.5)
    for baseline in trial["baselines"].values():
        baseline_errors = np.rad2deg(baseline["angles"] - data.truth.angle_rad)
        assert baseline["metrics"] == pytest.approx(
            _independent_angle_metrics(baseline_errors, times), rel=3e-13, abs=1e-14,
        )


@pytest.mark.parametrize("setting", SETTINGS)
def test_only_measurement_covariance_changes_at_first_correction(setting, trials):
    trial = trials["nominal"]
    data, result = trial["data"], trial["settings"][setting]
    reference = trial["settings"]["0.2"]
    initial_variance = float(np.deg2rad(1.0))**2
    np.testing.assert_array_equal(result["states"][0], [0.0, 0.0])
    np.testing.assert_array_equal(result["covariance"][0], np.diag([0.0, initial_variance]))
    # R has no effect before the first observation at endpoint 10.
    np.testing.assert_array_equal(result["states"][:10], reference["states"][:10])
    np.testing.assert_array_equal(result["covariance"][:10], reference["covariance"][:10])
    dt = np.diff(data.truth.time_s[:11])
    elapsed = float(data.truth.time_s[10])
    angle_prior = math.fsum(float(rate) * float(step)
                            for rate, step in zip(data.gyro_rate_rad_s[:10], dt))
    p_prior = initial_variance * np.array([
        [elapsed**2 + math.fsum(float(step)**2 for step in dt), -elapsed],
        [-elapsed, 1.0],
    ])
    g = 9.80665
    h = np.array([-g * math.sin(angle_prior), -g * math.cos(angle_prior)])
    jacobian = np.array([[-g * math.cos(angle_prior), 0.0],
                         [g * math.sin(angle_prior), 0.0]])
    cross = p_prior @ jacobian.T
    expected_s = jacobian @ cross + float(setting)**2 * np.eye(2)
    np.testing.assert_allclose(result["innovation_covariance"][0], expected_s,
                               rtol=2e-14, atol=1e-18)
    np.testing.assert_allclose(result["innovations"][0], data.force_yz_m_s2[0] - h,
                               rtol=0.0, atol=2e-15)
    expected_posterior = p_prior - cross @ np.linalg.solve(expected_s, cross.T)
    np.testing.assert_allclose(result["covariance"][10], expected_posterior,
                               rtol=3e-14, atol=1e-18)


@pytest.mark.parametrize("name", CASES)
def test_numerical_contract_preserves_finite_symmetric_covariances(name, trials):
    for result in trials[name]["settings"].values():
        for field in ARRAY_FIELDS:
            assert np.all(np.isfinite(result[field]))
        p, s = result["covariance"], result["innovation_covariance"]
        np.testing.assert_allclose(p, p.transpose(0, 2, 1), rtol=0.0, atol=1e-12)
        assert np.linalg.eigvalsh(p).min() >= -1e-12
        np.testing.assert_allclose(s, s.transpose(0, 2, 1), rtol=0.0, atol=1e-12)
        assert np.linalg.eigvalsh(s).min() > 0.0
        assert np.all(result["nis"] >= 0.0)
        assert all(math.isfinite(value) for value in result["metrics"].values())


def test_cases_share_gyro_and_standardized_accelerometer_noise(trials):
    nominal = trials["nominal"]["data"]
    mismatch = trials["accel_noise_mismatch"]["data"]
    np.testing.assert_array_equal(nominal.gyro_rate_rad_s, mismatch.gyro_rate_rad_s)
    np.testing.assert_array_equal(nominal.truth.time_s, mismatch.truth.time_s)
    assert nominal.stream_seeds == mismatch.stream_seeds
    angle = nominal.truth.angle_rad[nominal.observation_indices]
    noiseless = -9.80665 * np.column_stack((np.sin(angle), np.cos(angle)))
    np.testing.assert_allclose((nominal.force_yz_m_s2 - noiseless) / 0.2,
                               (mismatch.force_yz_m_s2 - noiseless) / 0.6,
                               rtol=2e-12, atol=1e-14)


def test_generate_once_and_supply_identical_unmodified_inputs_to_each_setting(monkeypatch, trials):
    data = deepcopy(trials["nominal"]["data"])
    arrays = [getattr(data, field.name) for field in fields(data)
              if isinstance(getattr(data, field.name), np.ndarray)]
    arrays.extend(getattr(data.truth, field.name) for field in fields(data.truth))
    before = [array.copy() for array in arrays]
    for array in arrays:
        array.setflags(write=False)
    generated, instances = [], []

    def generate(scenario, seed):
        generated.append((scenario.name, seed))
        return data

    class RecordingEKF(AngleBiasEKF):
        def __init__(self, **settings):
            super().__init__(**settings)
            self.received_rates, self.received_forces = [], []
            instances.append(self)

        def predict(self, rate_rad_s, dt_s):
            self.received_rates.append((rate_rad_s, dt_s))
            return super().predict(rate_rad_s, dt_s)

        def update(self, force_yz_m_s2):
            self.received_forces.append(np.asarray(force_yz_m_s2).copy())
            return super().update(force_yz_m_s2)

    monkeypatch.setattr(study, "generate_scenario", generate)
    monkeypatch.setattr(study, "AngleBiasEKF", RecordingEKF)
    result = study.evaluate_trial(study.STUDY_SCENARIOS[0], 42)
    assert generated == [("nominal", 42)] and len(instances) == 3
    for instance in instances:
        np.testing.assert_array_equal(instance.received_rates,
                                      np.column_stack((data.gyro_rate_rad_s, np.diff(data.truth.time_s))))
        np.testing.assert_array_equal(instance.received_forces, data.force_yz_m_s2)
    for original, actual in zip(before, arrays):
        np.testing.assert_array_equal(actual, original)
    for setting in SETTINGS:
        np.testing.assert_array_equal(result["settings"][setting]["states"],
                                      trials["nominal"]["settings"][setting]["states"])


def test_hidden_angle_and_bias_truth_never_feed_the_filters(monkeypatch, trials):
    reference = trials["nominal"]
    data = reference["data"]
    changed_truth = replace(data.truth, angle_rad=data.truth.angle_rad + 1.0,
                            interval_rate_rad_s=data.truth.interval_rate_rad_s + 2.0)
    changed = replace(data, truth=changed_truth, bias_rad_s=data.bias_rad_s + 3.0,
                      interval_bias_rad_s=data.interval_bias_rad_s + 4.0)
    monkeypatch.setattr(study, "generate_scenario", lambda scenario, seed: changed)
    actual = study.evaluate_trial(study.STUDY_SCENARIOS[0], 42)
    for setting in SETTINGS:
        for field in ARRAY_FIELDS:
            np.testing.assert_array_equal(actual["settings"][setting][field],
                                          reference["settings"][setting][field])
        assert actual["settings"][setting]["metrics"]["angle_rmse_deg"] != (
            reference["settings"][setting]["metrics"]["angle_rmse_deg"]
        )
        assert actual["settings"][setting]["metrics"]["bias_rmse_deg_s"] != (
            reference["settings"][setting]["metrics"]["bias_rmse_deg_s"]
        )
    for name in reference["baselines"]:
        np.testing.assert_array_equal(actual["baselines"][name]["angles"],
                                      reference["baselines"][name]["angles"])


@pytest.mark.parametrize("arguments", [[], ["--phase", "unknown"]])
def test_cli_requires_a_declared_phase_without_creating_outputs(tmp_path, arguments):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        study.main([*arguments, "--output", str(output)])
    assert failure.value.code == 2
    assert not output.exists()


def test_invalid_phase_or_existing_output_is_rejected_before_simulation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid requests must not execute any scenario.")

    monkeypatch.setattr(study, "evaluate_trial", forbidden)
    invalid = tmp_path / "invalid"
    with pytest.raises(ValueError):
        study.run_experiment(invalid, phase="unknown")
    assert not invalid.exists()
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("Do not overwrite this output.\n")
    with pytest.raises(FileExistsError):
        study.run_experiment(existing, phase="exploration")
    assert marker.read_text() == "Do not overwrite this output.\n"
    assert list(existing.iterdir()) == [marker]


def test_compact_scores_keep_seed_pairs_and_exclude_truth_from_input_fingerprint(trials):
    trial = trials["nominal"]
    compact = study._compact(trial, 42)
    assert compact["seed"] == 42
    assert compact["stream_seeds"] == trial["data"].stream_seeds
    default = trial["settings"]["0.2"]["metrics"]
    for setting in SETTINGS:
        metrics = trial["settings"][setting]["metrics"]
        assert compact["settings"][setting]["metrics"] == metrics
        assert compact["settings"][setting]["delta_from_default"] == {
            name: value - default[name] for name, value in metrics.items()
        }
    data = trial["data"]
    changed_truth = replace(data, truth=replace(data.truth, angle_rad=data.truth.angle_rad + 1.0),
                            bias_rad_s=data.bias_rad_s + 1.0)
    fingerprint = study._measurement_fingerprint(data)
    assert study._measurement_fingerprint(changed_truth) == fingerprint
    for attribute in ("gyro_rate_rad_s", "force_yz_m_s2", "observation_indices"):
        values = getattr(data, attribute).copy()
        values.flat[0] += 1
        assert study._measurement_fingerprint(replace(data, **{attribute: values})) != fingerprint
    changed_time = replace(data, truth=replace(data.truth, time_s=data.truth.time_s + 0.01))
    assert study._measurement_fingerprint(changed_time) != fingerprint


def test_aggregates_use_per_seed_differences_and_observed_ranges():
    # A mixed-sign example distinguishes paired differences from differences
    # of extrema or a ranking that silently discards unfavorable runs.
    rmse_names = ("angle_rmse_deg", "angle_time_weighted_rmse_deg", "late_angle_rmse_deg",
                  "bias_rmse_deg_s", "late_bias_rmse_deg_s")
    values = {"0.1": (1.0, 4.0, 10.0), "0.2": (3.0, 4.0, 7.0), "0.6": (4.0, 2.0, 6.0)}
    records = []
    for seed in range(3):
        settings = {}
        for setting in SETTINGS:
            value, default = values[setting][seed], values["0.2"][seed]
            metrics = {name: value for name in rmse_names}
            metrics["mean_nis"] = value + 10.0
            settings[setting] = {
                "metrics": metrics,
                "delta_from_default": {name: value - default for name in metrics},
            }
        records.append({
            "seed": seed, "settings": settings,
            "baselines": {"gyro": {"angle_rmse_deg": 11.0 + seed},
                          "complementary": {"angle_rmse_deg": 3.0 - seed}},
        })
    aggregate = study._aggregates(records)
    low, default, high = (aggregate["settings"][name] for name in SETTINGS)
    for metric in rmse_names:
        assert low["metrics"][metric] == {"mean": 5.0, "min": 1.0, "max": 10.0}
        assert low["delta_from_default"][metric] == pytest.approx({"mean": 1.0 / 3.0, "min": -2.0, "max": 3.0})
        assert low["rmse_delta_counts"][metric] == {"negative": 1, "zero": 1, "positive": 1}
        assert default["delta_from_default"][metric] == {"mean": 0.0, "min": 0.0, "max": 0.0}
        assert default["rmse_delta_counts"][metric] == {"negative": 0, "zero": 3, "positive": 0}
        assert high["delta_from_default"][metric] == pytest.approx({"mean": -2.0 / 3.0, "min": -2.0, "max": 1.0})
        assert high["rmse_delta_counts"][metric] == {"negative": 2, "zero": 0, "positive": 1}
    assert low["metrics"]["mean_nis"] == {"mean": 15.0, "min": 11.0, "max": 20.0}
    assert set(low["rmse_delta_counts"]) == set(rmse_names)
    assert aggregate["baselines"] == {
        "gyro": {"angle_rmse_deg": {"mean": 12.0, "min": 11.0, "max": 13.0}},
        "complementary": {"angle_rmse_deg": {"mean": 2.0, "min": 1.0, "max": 3.0}},
    }


@pytest.mark.parametrize("field,index,value", [
    ("states", (1, 0), np.nan),
    ("covariance", (1, 0, 1), 1.0),
    ("covariance", (1, 0, 0), -1.0),
    ("innovation_covariance", (0, 0, 1), 1.0),
    ("innovation_covariance", (0, 0, 0), -1.0),
    ("innovations", (0, 0), np.inf),
    ("nis", (0,), -1.0),
])
def test_invalid_numerical_evidence_is_rejected(field, index, value, trials):
    result = deepcopy(trials["nominal"]["settings"]["0.2"])
    result[field][index] = value
    with pytest.raises(ValueError):
        study._check_numerics(result)


def test_exploration_export_reproduces_full_resolution_evidence(tmp_path, monkeypatch, capsys):
    # Reduce only exploration for this export contract; evaluation is never run.
    monkeypatch.setitem(study.PHASE_SEEDS, "exploration", (0,))
    evaluate = study.evaluate_trial
    captured, calls = {}, []

    def record_trial(scenario, seed):
        assert seed == 0, "Export tests must not consume final-evaluation seeds."
        trial = evaluate(scenario, seed)
        captured[scenario.name] = trial
        calls.append((scenario.name, seed))
        return trial

    monkeypatch.setattr(study, "evaluate_trial", record_trial)
    first, second = tmp_path / "first", tmp_path / "second"
    summary = study.run_experiment(first, phase="exploration")
    assert study.main(["--phase", "exploration", "--output", str(second)]) == 0
    assert "exploration: 1 seeds per case; all R settings retained." in capsys.readouterr().out
    assert calls == [(name, 0) for name in CASES] * 2
    assert summary == json.loads((first / "summary.json").read_text())
    assert summary == json.loads((second / "summary.json").read_text())
    files = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    assert len(files) == 22
    assert files == {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()}
    assert files[Path("overview.png")].startswith(b"\x89PNG\r\n\x1a\n")
    assert summary["phase"] == "exploration"
    assert summary["seeds"] == [0] and summary["representative_seed"] == 0
    assert summary["protocol"]["phase_seeds"]["evaluation"] == list(range(1000, 1020))
    assert summary["protocol"]["accuracy_thresholds"] is None
    protocol_bytes = json.dumps(summary["protocol"], sort_keys=True, allow_nan=False).encode()
    assert summary["protocol_sha256"] == hashlib.sha256(protocol_bytes).hexdigest()
    for name, fingerprint in summary["source_sha256"].items():
        assert hashlib.sha256(Path(study.__file__).with_name(name).read_bytes()).hexdigest() == fingerprint

    def read_matrix(path):
        return np.genfromtxt(path, delimiter=",", skip_header=1)

    for name in CASES:
        trial = captured[name]
        data = trial["data"]
        times = data.truth.time_s
        directory = first / name
        case = summary["scenarios"][name]
        assert case["trials"] == [study._compact(trial, 0)]
        np.testing.assert_array_equal(read_matrix(directory / "gyro_measurements.csv"),
                                      np.column_stack((times[:-1], times[1:], data.gyro_rate_rad_s)))
        np.testing.assert_array_equal(read_matrix(directory / "accel_measurements.csv"),
                                      np.column_stack((times[data.observation_indices], data.force_yz_m_s2)))
        np.testing.assert_array_equal(read_matrix(directory / "truth.csv"),
                                      np.column_stack((times, data.truth.angle_rad, data.bias_rad_s)))
        np.testing.assert_array_equal(read_matrix(directory / "baseline_estimates.csv"),
                                      np.column_stack((times, trial["baselines"]["gyro"]["angles"],
                                                       trial["baselines"]["complementary"]["angles"])))
        for setting in SETTINGS:
            result = trial["settings"][setting]
            p, s = result["covariance"], result["innovation_covariance"]
            np.testing.assert_array_equal(read_matrix(directory / f"ekf-{setting}-estimates.csv"),
                                          np.column_stack((times, result["states"], p[:, 0, 0], p[:, 0, 1], p[:, 1, 1])))
            np.testing.assert_array_equal(read_matrix(directory / f"ekf-{setting}-innovations.csv"),
                                          np.column_stack((times[data.observation_indices], result["innovations"],
                                                           s[:, 0, 0], s[:, 0, 1], s[:, 1, 1], result["nis"])))
            for metric, value in result["metrics"].items():
                assert case["aggregates"]["settings"][setting]["metrics"][metric] == {
                    "mean": value, "min": value, "max": value,
                }
