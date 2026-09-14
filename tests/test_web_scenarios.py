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
    assert data["schema_version"] == 2
    assert list(data["scenarios"]) == ["nominal", "translation_pulse", "initial_offset", "accel_dropout", "bias_ramp"]
    for name, case in data["scenarios"].items():
        rows = np.array(case["rows"])
        ticks = set(np.rint(rows[:, 0]*100).astype(int))
        assert 0 in ticks and 3000 in ticks
        for time in case["correction_times_s"]:
            assert round(time*100) in ticks and round(time*100)-1 in ticks
        assert case["source_sample_count"] == 3001
        if case["source"] == "controlled":
            for method, metric in case["metrics"]["rmse_deg"].items():
                assert metric == summary["scenarios"][name]["metrics"][method]["angle_rmse_deg"]
            source = np.genfromtxt(sources[1]/name/"estimates.csv", names=True, delimiter=",")
            for row in rows:
                index = int(round(row[0]*100))
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
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="paired gyro noise"):
        build_scenarios(sources[0], controlled)
