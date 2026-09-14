"""Check the combined display contract against reproducible experiment outputs."""

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from meridian.ekf_experiment import run_experiment as paired_experiment
from meridian.stress_experiment import run_experiment as controlled_experiment
from meridian.web_scenarios import build_scenarios, export_scenarios, main


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("web-scenarios")
    paired, controlled = root/"paired", root/"controlled"
    paired_experiment(paired, validation_seeds=1)
    controlled_experiment(controlled, validation_seeds=1)
    return paired, controlled


def test_selection_units_metrics_and_preserved_corrections(sources):
    data = build_scenarios(*sources, stride=37)
    summary = json.loads((sources[1]/"summary.json").read_text())
    assert data["schema_version"] == 4
    assert list(data["scenarios"]) == ["nominal", "translation_pulse", "initial_offset", "accel_dropout", "bias_ramp",
                                       "initial_overconfident", "accel_noise_mismatch", "timing_jitter", "accel_delay"]
    for name, case in data["scenarios"].items():
        rows = np.array(case["rows"])
        assert rows[0, 0] == 0 and rows[-1, 0] == 30
        for time, previous in zip(case["correction_times_s"], case["timing"]["correction_predecessor_times_s"]):
            index = int(np.argmin(abs(rows[:, 0]-time)))
            assert rows[index, 0] == pytest.approx(time, abs=1e-12)
            assert rows[index-1, 0] == previous
        assert case["source_sample_count"] == 3001
        if case["source"] == "controlled":
            for method, metric in case["metrics"]["rmse_deg"].items():
                assert metric == summary["scenarios"][name]["metrics"][method]["angle_rmse_deg"]
            source = np.genfromtxt(sources[1]/name/"estimates.csv", names=True, delimiter=",")
            for row in rows:
                index = int(np.argmin(abs(source["time_s"]-row[0])))
                assert row[5] == pytest.approx(np.rad2deg(source["ekf_roll_rad"][index]), abs=5.01e-7)
                assert row[8] == pytest.approx(np.rad2deg(source["ekf_bias_rad_s"][index]), abs=5.01e-7)
    initial = data["scenarios"]["initial_offset"]
    assert initial["rows"][0][2:6] == [60]*4
    assert initial["initialization"]["angle_std_deg"] == 30
    loss = data["scenarios"]["accel_dropout"]
    assert len(loss["correction_times_s"]) == 250
    assert not any(12 <= time < 17 for time in loss["correction_times_s"])
    assert loss["event"]["last_correction_before_s"] == pytest.approx(11.9)
    assert loss["event"]["first_correction_after_s"] == 17
    assert {1199, 1200, 1699, 1700} <= {round(row[0]*100) for row in loss["rows"]}
    assert data["sources"]["paired"]["source_sha256"]["summary.json"] != data["sources"]["controlled"]["source_sha256"]["summary.json"]
    assert all(isinstance(value, str) for values in data["sources"]["controlled"]["stream_seeds"].values() for value in values.values())


def test_bias_ramp_truth_interval_means_and_shared_noise(sources):
    data = build_scenarios(*sources, stride=37)
    case = data["scenarios"]["bias_ramp"]
    rows = np.asarray(case["rows"])
    assert case["event"] == {"kind": "bias_ramp", "start_s": 10., "end_s": 20.,
                             "initial_bias_deg_s": .5, "final_bias_deg_s": 1.5, "slope_deg_s2": .1}
    assert len(case["correction_times_s"]) == 300
    assert {999, 1000, 1999, 2000} <= {round(row[0]*100) for row in rows}
    np.testing.assert_allclose(rows[:, 6], .5+.1*np.clip(rows[:, 0]-10, 0, 10), atol=5.01e-7, rtol=0)
    # Independent integral of the piecewise-linear bias, in degrees.
    intervals = np.genfromtxt(sources[1]/"bias_ramp"/"gyro_interval_truth.csv", names=True, delimiter=",")
    def integral(time):
        return .5*time+.05*(np.maximum(time-10, 0)**2-np.maximum(time-20, 0)**2)
    start, end = intervals["t_start_s"], intervals["t_end_s"]
    means = np.rad2deg(intervals["mean_bias_rad_s"])
    np.testing.assert_allclose(means, (integral(end)-integral(start))/(end-start), atol=2e-11, rtol=0)
    assert means[1499] == pytest.approx(.9995, abs=1e-11)
    assert means@np.diff(np.r_[0, end]) == pytest.approx(30.)
    ramp_gyro = np.genfromtxt(sources[1]/"bias_ramp"/"gyro_measurements.csv", names=True, delimiter=",")
    reference_gyro = np.genfromtxt(sources[1]/"initial_offset"/"gyro_measurements.csv", names=True, delimiter=",")
    np.testing.assert_allclose(np.rad2deg(ramp_gyro["rate_rad_s"]-reference_gyro["rate_rad_s"]),
                               means-.5, atol=2e-11, rtol=0)


def test_export_is_deterministic_and_refuses_overwrite(sources, tmp_path):
    first, second = tmp_path/"one.json", tmp_path/"two.json"
    export_scenarios(*sources, first)
    assert main([str(sources[0]), str(sources[1]), "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        export_scenarios(*sources, first)
    assert first.read_bytes() == second.read_bytes()


def test_recorded_irregular_endpoints_and_delayed_acquisition(sources):
    cases = build_scenarios(*sources, stride=37)["scenarios"]
    jitter = cases["timing_jitter"]
    truth = np.loadtxt(sources[1]/"timing_jitter/truth.csv", skiprows=1, delimiter=",")
    np.testing.assert_array_equal(jitter["correction_times_s"], truth[10::10, 0])
    np.testing.assert_array_equal(jitter["timing"]["correction_predecessor_times_s"], truth[9::10, 0])
    assert jitter["correction_times_s"][0] == pytest.approx(.09331262108751584, abs=1e-15)
    assert jitter["timing"]["gyro_interval_min_s"] == np.diff(truth[:, 0]).min()
    assert jitter["timing"]["gyro_interval_max_s"] == np.diff(truth[:, 0]).max()
    assert jitter["event"] is None
    delay = cases["accel_delay"]
    samples = np.asarray(delay["timing"]["correction_sample_times_s"])
    np.testing.assert_allclose(samples, np.asarray(delay["correction_times_s"])-.1, atol=1e-15, rtol=0)
    assert samples[0] == 0 and samples[-1] == 29.9
    assert delay["event"] is None
    # Noise is paired by draw order even when physical acquisition times differ.
    for name in ("timing_jitter", "accel_delay"):
        force = np.loadtxt(sources[1]/name/"accel_measurements.csv", skiprows=1, delimiter=",")[:, 1:]
        times = np.asarray(cases[name]["timing"]["correction_sample_times_s"])
        reference = np.loadtxt(sources[1]/"initial_offset/accel_measurements.csv", skiprows=1, delimiter=",")
        theta = np.deg2rad(20)*np.sin(2*np.pi*.1*times)
        theta_ref = np.deg2rad(20)*np.sin(2*np.pi*.1*reference[:, 0])
        expected_difference = -9.80665*np.column_stack([np.sin(theta)-np.sin(theta_ref), np.cos(theta)-np.cos(theta_ref)])
        np.testing.assert_allclose(force-reference[:, 1:], expected_difference, atol=1e-12, rtol=0)


def test_duration_weighting_uses_original_unrounded_endpoints(sources):
    cases = build_scenarios(*sources, stride=37)["scenarios"]
    summary = json.loads((sources[1]/"summary.json").read_text())
    for name, case in cases.items():
        folder = sources[0 if case["source"] == "paired" else 1]/name
        truth = np.genfromtxt(folder/"truth.csv", names=True, delimiter=",")
        estimates = np.genfromtxt(folder/"estimates.csv", names=True, delimiter=",")
        for method, actual in case["metrics"]["time_weighted_rmse_deg"].items():
            table = np.genfromtxt(folder/"ekf_estimates.csv", names=True, delimiter=",") if method == "ekf" and case["source"] == "paired" else estimates
            errors = np.rad2deg(table[f"{method}_roll_rad"]-truth["roll_rad"])
            # Explicit trapezoid areas, independently of the adapter's np.trapezoid.
            area = sum((left*left+right*right)*(end-start)/2 for left, right, start, end
                       in zip(errors[:-1], errors[1:], truth["time_s"][:-1], truth["time_s"][1:]))
            assert actual == pytest.approx(np.sqrt(area/30), abs=1e-12, rel=0)
            if case["source"] == "controlled":
                assert actual == summary["scenarios"][name]["metrics"][method]["angle_time_weighted_rmse_deg"]


@pytest.mark.parametrize("mutation", ["rounded_arrival", "rounded_clock", "delay_as_arrival",
                                      "missing_delay", "arrival_force", "weighted_metric", "timing_seed"])
def test_reject_misrepresented_timing(sources, tmp_path, mutation):
    controlled = Path(shutil.copytree(sources[1], tmp_path/"controlled"))
    if mutation in ("weighted_metric", "timing_seed"):
        path = controlled/"summary.json"
        summary = json.loads(path.read_text())
        record = summary["scenarios"]["timing_jitter"]
        if mutation == "weighted_metric": record["metrics"]["ekf"]["angle_time_weighted_rmse_deg"] += .1
        else: record["stream_seeds"]["timing"] += 1
        path.write_text(json.dumps(summary))
    else:
        name = "timing_jitter" if mutation.startswith("rounded") else "accel_delay"
        filename = "truth.csv" if mutation == "rounded_clock" else "accel_measurements.csv" if mutation == "arrival_force" else "observation_schedule_truth.csv"
        path = controlled/name/filename
        header = path.read_text().splitlines()[0]
        values = np.loadtxt(path, delimiter=",", skiprows=1)
        if mutation.startswith("rounded"): values[:, 0] = np.round(values[:, 0], 2)
        elif mutation == "delay_as_arrival": values[:, 0] = values[:, 1]
        elif mutation == "missing_delay": values[:, 1] = values[:, 0]
        else:
            theta = np.deg2rad(20)*np.sin(2*np.pi*.1*values[:, 0])
            old_theta = np.deg2rad(20)*np.sin(2*np.pi*.1*(values[:, 0]-.1))
            values[:, 1:] += -9.80665*np.column_stack([np.sin(theta)-np.sin(old_theta), np.cos(theta)-np.cos(old_theta)])
        np.savetxt(path, values, delimiter=",", header=header, comments="", fmt="%.17g")
    with pytest.raises(ValueError):
        build_scenarios(sources[0], controlled)


def test_initial_confidence_changes_only_kalman_references(sources):
    cases = build_scenarios(*sources)["scenarios"]
    uncertain, confident = cases["initial_offset"], cases["initial_overconfident"]
    assert confident["rows"][0][2:6] == [60]*4
    assert confident["initialization"]["angle_std_deg"] == 0
    assert uncertain["initialization"]["angle_std_deg"] == 30
    assert confident["event"] is None
    np.testing.assert_array_equal(np.asarray(uncertain["rows"])[:, :4], np.asarray(confident["rows"])[:, :4])
    assert uncertain["metrics"]["rmse_deg"]["ekf"] != confident["metrics"]["rmse_deg"]["ekf"]


def test_noise_scale_is_separate_from_fixed_filter_assumption(sources):
    cases = build_scenarios(*sources)["scenarios"]
    assert cases["accel_noise_mismatch"]["event"] is None
    for name, case in cases.items():
        assert case["accelerometer_noise"] == {
            "actual_std_m_s2": .6 if name == "accel_noise_mismatch" else .2, "assumed_std_m_s2": .2}
    reference, noisy = [np.loadtxt(sources[1]/name/"accel_measurements.csv", delimiter=",", skiprows=1)
                        for name in ("initial_offset", "accel_noise_mismatch")]
    # Independent force identity for the same standardized noise draw at 3x sigma.
    theta = np.deg2rad(20)*np.sin(2*np.pi*.1*reference[:, 0])
    gravity = -9.80665*np.column_stack([np.sin(theta), np.cos(theta)])
    np.testing.assert_allclose(noisy[:, 1:], 3*reference[:, 1:]-2*gravity, atol=1e-12, rtol=0)


@pytest.mark.parametrize("mutation", ["actual_noise", "assumed_noise", "accel_component", "overconfident_covariance"])
def test_reject_misrepresented_confidence_or_noise(sources, tmp_path, mutation):
    controlled = Path(shutil.copytree(sources[1], tmp_path/"controlled"))
    if mutation in ("actual_noise", "assumed_noise"):
        path = controlled/"summary.json"
        summary = json.loads(path.read_text())
        if mutation == "actual_noise":
            definition = next(item for item in summary["scenario_definitions"] if item["name"] == "accel_noise_mismatch")
            definition["accel_noise_std_m_s2"] = .2
        else:
            summary["shared_settings"]["assumed_accel_noise_std_m_s2"] = .6
        path.write_text(json.dumps(summary))
        message = "definition|settings"
    else:
        path = controlled/("accel_noise_mismatch/accel_measurements.csv" if mutation == "accel_component"
                           else "initial_overconfident/estimates.csv")
        header = path.read_text().splitlines()[0]
        values = np.loadtxt(path, delimiter=",", skiprows=1)
        if mutation == "accel_component":
            values[40, 1] += .01
            message = "paired accelerometer noise"
        else:
            values[0, header.split(",").index("ekf_p_angle_rad2")] = np.deg2rad(30)**2
            message = "initial covariance"
        np.savetxt(path, values, delimiter=",", header=header, comments="", fmt="%.17g")
    with pytest.raises(ValueError, match=message):
        build_scenarios(sources[0], controlled)


@pytest.mark.parametrize("stride", [0, -1, True, 1.5])
def test_invalid_stride(sources, stride):
    with pytest.raises(ValueError, match="stride"):
        build_scenarios(*sources, stride=stride)


@pytest.mark.parametrize("mutation", ["seed", "definition", "rmse", "availability", "sample_time",
                                      "initial_covariance", "gyro", "missing_correction", "early_bias",
                                      "ramp_truth", "ramp_interval_mean", "ramp_final_bias"])
def test_reject_inconsistent_controlled_records(sources, tmp_path, mutation):
    controlled = Path(shutil.copytree(sources[1], tmp_path/"controlled"))
    summary_path = controlled/"summary.json"
    summary = json.loads(summary_path.read_text())
    if mutation == "seed":
        summary["seed"] += 1
    elif mutation == "definition":
        summary["scenario_definitions"][1]["initial_angle_std_deg"] = 0
    elif mutation == "rmse":
        summary["scenarios"]["initial_offset"]["metrics"]["ekf"]["angle_rmse_deg"] += 1
    elif mutation == "ramp_final_bias":
        summary["scenarios"]["bias_ramp"]["metrics"]["ekf"]["final_bias_error_deg_s"] += .1
    else:
        filename = {"availability": "observation_schedule_truth.csv", "sample_time": "observation_schedule_truth.csv",
                    "initial_covariance": "estimates.csv", "gyro": "gyro_measurements.csv",
                    "missing_correction": "ekf_innovations.csv", "early_bias": "estimates.csv",
                    "ramp_truth": "truth.csv", "ramp_interval_mean": "gyro_interval_truth.csv"}[mutation]
        target = controlled/("bias_ramp" if mutation.startswith("ramp_") else "accel_dropout")/filename
        header = target.read_text().splitlines()[0]
        values = np.loadtxt(target, delimiter=",", skiprows=1)
        if mutation == "availability": values[119, 2] = 1
        elif mutation == "sample_time": values[10, 1] -= .01
        elif mutation == "initial_covariance": values[0, header.split(",").index("ekf_p_angle_rad2")] = 1
        elif mutation == "gyro": values[50, 2] += .1
        elif mutation == "missing_correction": values = np.delete(values, 10, axis=0)
        elif mutation == "early_bias": values[1650, header.split(",").index("ekf_bias_rad_s")] += .01
        elif mutation == "ramp_truth": values[1500, 2] += .01
        elif mutation == "ramp_interval_mean": values[1499, 3] = np.deg2rad(1.)
        np.savetxt(target, values, delimiter=",", header=header, comments="", fmt="%.17g")
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError):
        build_scenarios(sources[0], controlled)


def test_reject_self_consistent_but_unpaired_ramp_gyro(sources, tmp_path):
    controlled = Path(shutil.copytree(sources[1], tmp_path/"controlled"))
    case = controlled/"bias_ramp"
    gyro_path, estimate_path = case/"gyro_measurements.csv", case/"estimates.csv"
    gyro = np.loadtxt(gyro_path, skiprows=1, delimiter=",")
    gyro[1000, 2] += np.deg2rad(.1)
    estimates = np.loadtxt(estimate_path, skiprows=1, delimiter=",")
    estimates[:, 1] = np.r_[0, np.cumsum(gyro[:, 2]*(gyro[:, 1]-gyro[:, 0]))]
    for path, values in [(gyro_path, gyro), (estimate_path, estimates)]:
        header = path.read_text().splitlines()[0]
        np.savetxt(path, values, delimiter=",", header=header, comments="", fmt="%.17g")
    truth = np.loadtxt(case/"truth.csv", skiprows=1, delimiter=",")
    summary_path = controlled/"summary.json"
    summary = json.loads(summary_path.read_text())
    summary["scenarios"]["bias_ramp"]["metrics"]["gyro"]["angle_rmse_deg"] = float(
        np.sqrt(np.mean(np.rad2deg(estimates[:, 1]-truth[:, 1])**2)))
    summary["scenarios"]["bias_ramp"]["metrics"]["gyro"]["angle_time_weighted_rmse_deg"] = float(
        np.sqrt(np.trapezoid(np.rad2deg(estimates[:, 1]-truth[:, 1])**2, truth[:, 0])/30))
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="paired gyro noise"):
        build_scenarios(sources[0], controlled)


def test_diagnostics_use_full_endpoints_and_si_covariance_before_conversion(sources):
    data = build_scenarios(*sources, stride=37)
    for name, case in data["scenarios"].items():
        diagnostic = case["diagnostics"]
        states = np.asarray(diagnostic["states"])
        assert states.shape == (3001, 13)
        assert len(case["rows"]) < len(states)
        assert diagnostic["state_phase"] == "endpoint_after_available_correction"
        assert diagnostic["innovation_phase"] == "prior_before_correction"
        folder = sources[0 if case["source"] == "paired" else 1]/name
        for method, angle_column, bias_column in [("kalman", 9, 11), ("ekf", 10, 12)]:
            filename = "ekf_estimates.csv" if method == "ekf" and case["source"] == "paired" else "estimates.csv"
            table = np.genfromtxt(folder/filename, names=True, delimiter=",")
            prefix = "" if case["source"] == "paired" else method+"_"
            np.testing.assert_array_equal(states[:, 0], table["time_s"])
            for field, column in [("p_angle_rad2", angle_column), ("p_bias_rad2_s2", bias_column)]:
                np.testing.assert_allclose(states[:, column], np.sqrt(table[prefix+field])*180/np.pi, atol=5.01e-7, rtol=0)
            record = diagnostic[method]
            assert record["dimension"] == (1 if method == "kalman" else 2)
            np.testing.assert_allclose(np.asarray(record["rows"])[:, 0], case["correction_times_s"], atol=1e-12, rtol=0)
    zero = data["scenarios"]["initial_overconfident"]["diagnostics"]["states"][0]
    assert zero[5]-zero[1] == 60 and zero[10] == 0
    pulse = np.asarray(data["scenarios"]["translation_pulse"]["diagnostics"]["states"])
    nominal = np.asarray(data["scenarios"]["nominal"]["diagnostics"]["states"])
    np.testing.assert_array_equal(pulse[:, 9:], nominal[:, 9:])
    assert abs(pulse[1400, 5]-pulse[1400, 1]) > 6
    assert pulse[1400, 10] < .23


def test_innovation_units_and_full_vector_nis(sources):
    data = build_scenarios(*sources)
    kf = np.asarray(data["scenarios"]["nominal"]["diagnostics"]["kalman"]["rows"])
    source = np.genfromtxt(sources[0]/"nominal/innovations.csv", names=True, delimiter=",")
    np.testing.assert_allclose(kf[:, 1]*np.pi/180, source["innovation_rad"], atol=1e-14, rtol=0)
    np.testing.assert_allclose(kf[:, 2]*(np.pi/180)**2, source["innovation_variance_rad2"], atol=1e-14, rtol=0)
    np.testing.assert_allclose(kf[:, 3], source["innovation_rad"]**2/source["innovation_variance_rad2"], atol=1e-12, rtol=0)
    vector = np.asarray(data["scenarios"]["initial_offset"]["diagnostics"]["ekf"]["rows"])
    for row in vector:
        # Independent closed form; the adapter uses a batched linear solve.
        _, vy, vz, syy, syz, szz, nis = row
        expected = (szz*vy*vy-2*syz*vy*vz+syy*vz*vz)/(syy*szz-syz*syz)
        assert nis == pytest.approx(expected, rel=1e-10, abs=1e-10)
    loss = data["scenarios"]["accel_dropout"]["diagnostics"]["ekf"]["rows"]
    assert len(loss) == 250 and not any(12 <= row[0] < 17 for row in loss)


@pytest.mark.parametrize("mutation", ["negative_p", "invalid_p_cross", "scalar_s", "vector_s_cross",
                                      "vector_nis", "scalar_nis", "mean_nis", "innovation_time"])
def test_reject_inconsistent_diagnostic_sources(sources, tmp_path, mutation):
    controlled = Path(shutil.copytree(sources[1], tmp_path/"controlled"))
    if mutation == "mean_nis":
        path = controlled/"summary.json"
        summary = json.loads(path.read_text())
        summary["scenarios"]["initial_offset"]["metrics"]["ekf"]["mean_nis"] += 1
        path.write_text(json.dumps(summary))
    else:
        filename = "estimates.csv" if mutation in ("negative_p", "invalid_p_cross") else "kalman_innovations.csv" if mutation in ("scalar_s", "scalar_nis") else "ekf_innovations.csv"
        path = controlled/"initial_offset"/filename
        header = path.read_text().splitlines()[0]
        columns = header.split(",")
        rows = np.loadtxt(path, delimiter=",", skiprows=1)
        field, value = {
            "negative_p": ("ekf_p_angle_rad2", -.01), "invalid_p_cross": ("ekf_p_angle_bias_rad2_s", 1.),
            "scalar_s": ("s_rad2", 0.), "vector_s_cross": ("s_yz_m2_s4", 100.),
            "vector_nis": ("nis", 100.), "scalar_nis": ("nis", 100.),
            "innovation_time": ("arrival_time_s", .119),
        }[mutation]
        rows[10, columns.index(field)] = value
        np.savetxt(path, rows, delimiter=",", header=header, comments="", fmt="%.17g")
    with pytest.raises(ValueError):
        build_scenarios(sources[0], controlled)
