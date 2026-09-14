"""Read endpoint uncertainty and pre-correction innovations without running filters."""

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from meridian.web_export import COLUMNS, _read_columns


STATE_COLUMNS = [*COLUMNS, "kalman_roll_std_deg", "ekf_roll_std_deg",
                 "kalman_bias_std_deg_s", "ekf_bias_std_deg_s"]
KALMAN_COLUMNS = ["arrival_time_s", "innovation_deg", "s_deg2", "nis"]
EKF_COLUMNS = ["arrival_time_s", "innovation_y_m_s2", "innovation_z_m_s2",
               "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4", "nis"]
COVARIANCE_FIELDS = ["p_angle_rad2", "p_angle_bias_rad2_s", "p_bias_rad2_s2"]


def _aligned(actual, expected, label):
    if np.shape(actual) != np.shape(expected) or not np.allclose(actual, expected, atol=1e-12, rtol=0):
        raise ValueError(f"inconsistent diagnostic {label}")


def _covariance(values, label, *, positive=False):
    """Check symmetric 2x2 covariance via diagonals and its cross term."""
    aa, ab, bb = values.T
    if (np.any(aa < 0) or np.any(bb < 0)
            or np.any(ab**2 > aa*bb*(1+1e-10)+1e-24)
            or (positive and (np.any(aa <= 0) or np.any(bb <= 0) or np.any(aa*bb-ab**2 <= 0)))):
        raise ValueError(f"invalid diagnostic {label} covariance")


def build_diagnostics(source: Path, name: str, case: dict) -> tuple[dict, dict]:
    """Retain all endpoints and only actual corrections, with explicit SI/display units.

    States/P are endpoint values after any correction. Innovations/S describe
    the prior at the correction, not the preceding gyro endpoint or posterior.
    """
    source = Path(source)
    paired = case["source"] == "paired"
    hashes = {}

    def read(filename, columns):
        path = source/name/filename
        hashes[f"{name}/{filename}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return _read_columns(path, columns)

    truth = read("truth.csv", ["time_s", "roll_rad", "bias_rad_s"])
    time = truth[:, 0]
    if paired:
        baseline = read("estimates.csv", ["time_s", "gyro_roll_rad", "complementary_roll_rad",
                                         "kalman_roll_rad", "kalman_bias_rad_s", *COVARIANCE_FIELDS])
        ekf = read("ekf_estimates.csv", ["time_s", "ekf_roll_rad", "ekf_bias_rad_s", *COVARIANCE_FIELDS])
        _aligned(ekf[:, 0], time, "EKF endpoints")
        angles = np.column_stack([baseline[:, 1:4], ekf[:, 1]])
        biases = np.column_stack([baseline[:, 4], ekf[:, 2]])
        covariances = [baseline[:, 5:8], ekf[:, 3:6]]
    else:
        baseline = read("estimates.csv", ["time_s", "gyro_roll_rad", "complementary_roll_rad",
                                         "kalman_roll_rad", "ekf_roll_rad", "kalman_bias_rad_s", "ekf_bias_rad_s",
                                         *[f"{method}_{field}" for method in ("kalman", "ekf") for field in COVARIANCE_FIELDS]])
        angles, biases = baseline[:, 1:5], baseline[:, 5:7]
        covariances = [baseline[:, 7:10], baseline[:, 10:13]]
    _aligned(baseline[:, 0], time, "state endpoints")
    if len(time) != case["source_sample_count"]:
        raise ValueError("inconsistent diagnostic endpoint count")
    std = []
    for method, covariance in zip(("kalman", "ekf"), covariances):
        _covariance(covariance, method)
        std.append(np.rad2deg(np.sqrt(covariance[:, [0, 2]])))
    states = np.column_stack([time, np.rad2deg(truth[:, 1]), np.rad2deg(angles),
                              np.rad2deg(truth[:, 2]), np.rad2deg(biases),
                              std[0][:, 0], std[1][:, 0], std[0][:, 1], std[1][:, 1]])
    states[:, 1:] = np.round(states[:, 1:], 6)
    # The old reduced view must still represent the same underlying endpoints.
    display = np.asarray(case["rows"])
    indices = np.searchsorted(time, display[:, 0])
    if np.any(indices >= len(time)):
        raise ValueError("inconsistent diagnostic display extent")
    _aligned(states[indices, :9], display, "display rows")
    expected_initial = [case["initialization"]["angle_std_deg"]]*2 + [case["initialization"]["bias_std_deg_s"]]*2
    _aligned(states[0, 9:], expected_initial, "initial uncertainty")

    arrivals = case["correction_times_s"]
    kf = read("innovations.csv" if paired else "kalman_innovations.csv",
              ["time_s" if paired else "arrival_time_s", "innovation_rad",
               "innovation_variance_rad2" if paired else "s_rad2", *([] if paired else ["nis"])])
    vector = read("ekf_innovations.csv", ["time_s" if paired else "arrival_time_s",
                  "innovation_y_m_s2", "innovation_z_m_s2", "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4",
                  "normalized_innovation_squared" if paired else "nis"])
    _aligned(kf[:, 0], arrivals, "KF arrivals")
    _aligned(vector[:, 0], arrivals, "EKF arrivals")
    if np.any(kf[:, 2] <= 0):
        raise ValueError("invalid diagnostic scalar innovation covariance")
    _covariance(vector[:, 3:6], "vector innovation", positive=True)
    scalar_nis = kf[:, 1]**2/kf[:, 2]
    matrices = np.stack([vector[:, [3, 4]], vector[:, [4, 5]]], axis=1)
    vector_nis = np.sum(vector[:, 1:3]*np.linalg.solve(matrices, vector[:, 1:3, None])[:, :, 0], axis=1)
    for actual, computed in [(vector[:, 6], vector_nis), *([] if paired else [(kf[:, 3], scalar_nis)])]:
        if np.any(actual < 0) or not np.allclose(actual, computed, atol=1e-10, rtol=1e-10):
            raise ValueError("inconsistent diagnostic NIS")
    summary = json.loads((source/"summary.json").read_text())["scenarios"][name]
    means = [summary["baselines"]["mean_normalized_innovation_squared"], summary["vector_ekf"]["mean_normalized_innovation_squared"]] if paired else [summary["metrics"][m]["mean_nis"] for m in ("kalman", "ekf")]
    for actual, values in zip(means, (scalar_nis, vector_nis)):
        if not math.isclose(actual, float(values.mean()), abs_tol=1e-10, rel_tol=1e-10):
            raise ValueError("inconsistent diagnostic mean NIS")
    scalar = np.column_stack([kf[:, 0], np.rad2deg(kf[:, 1]), kf[:, 2]*(180/np.pi)**2, scalar_nis])
    return {
        "state_phase": "endpoint_after_available_correction", "innovation_phase": "prior_before_correction",
        "state_columns": STATE_COLUMNS, "states": states.tolist(),
        "kalman": {"dimension": 1, "columns": KALMAN_COLUMNS, "rows": scalar.tolist(), "mean_nis": means[0]},
        "ekf": {"dimension": 2, "columns": EKF_COLUMNS, "rows": vector.tolist(), "mean_nis": means[1]},
    }, hashes
