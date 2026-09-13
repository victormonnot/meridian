"""Evaluate fixed controlled scenarios with unchanged Python reference filters."""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike

from meridian.ekf import AngleBiasEKF
from meridian.integration import integrate_gyro
from meridian.kalman import AngleBiasKalman
from meridian.stress_scenarios import Scenario, generate_scenario
from meridian.tilt import ComplementaryRoll, accel_roll


GRAVITY_M_S2 = 9.80665
ASSUMED_GYRO_STD_RAD_S = float(np.deg2rad(1.0))
ASSUMED_ACCEL_STD_M_S2 = 0.2
COMPLEMENTARY_TAU_S = 1.0


def summarize_errors(error_deg: ArrayLike, time_s: ArrayLike) -> dict[str, float]:
    """Summarize unwrapped endpoint angle errors, including initialization.

    The duration-weighted value integrates squared endpoint errors with the
    trapezoid rule. It is not an exact continuous estimator-error integral at
    corrections. The late window is fixed at t >= 25 s for this 30 s study.
    """
    errors = np.asarray(error_deg, dtype=np.float64)
    times = np.asarray(time_s, dtype=np.float64)
    if (
        errors.ndim != 1 or times.ndim != 1 or errors.shape != times.shape
        or times.size < 2 or not np.all(np.isfinite(errors))
        or not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0.0)
    ):
        raise ValueError("errors and times must be matching finite vectors with increasing times")
    late = times >= 25.0
    if not np.any(late):
        raise ValueError("the fixed late window requires an endpoint at or after 25 s")
    return {
        "angle_rmse_deg": float(np.sqrt(np.mean(errors**2))),
        "angle_time_weighted_rmse_deg": float(
            np.sqrt(np.trapezoid(errors**2, times) / (times[-1] - times[0]))
        ),
        "late_angle_rmse_deg": float(np.sqrt(np.mean(errors[late]**2))),
        "max_abs_angle_error_deg": float(np.max(np.abs(errors))),
        "final_angle_error_deg": float(errors[-1]),
    }


def evaluate_scenario(scenario: Scenario, seed: int) -> dict:
    """Run gyro, complementary, angle KF, and vector EKF on shared inputs.

    Every gyro interval is propagated with its actual duration. Only available
    force samples trigger corrections, at their reported arrival endpoints.
    Hidden source times and bias truth are reserved for provenance and metrics;
    they are never supplied to a filter. Noise assumptions remain fixed even
    when a scenario changes the generated noise or true bias.
    """
    data = generate_scenario(scenario, seed)
    times = data.truth.time_s
    initial_angle = float(np.deg2rad(scenario.initial_angle_deg))
    shared_settings = {
        "initial_angle_rad": initial_angle,
        "initial_bias_rad_s": 0.0,
        "initial_angle_std_rad": float(np.deg2rad(scenario.initial_angle_std_deg)),
        "initial_bias_std_rad_s": float(np.deg2rad(1.0)),
        "gyro_noise_std_rad_s": ASSUMED_GYRO_STD_RAD_S,
    }
    filters = {
        "kalman": AngleBiasKalman(
            **shared_settings,
            angle_noise_std_rad=ASSUMED_ACCEL_STD_M_S2 / GRAVITY_M_S2,
        ),
        "ekf": AngleBiasEKF(
            **shared_settings,
            accel_noise_std_m_s2=ASSUMED_ACCEL_STD_M_S2,
            gravity_m_s2=GRAVITY_M_S2,
        ),
    }
    complementary = ComplementaryRoll(initial_angle, COMPLEMENTARY_TAU_S)
    indices = data.observation_indices[data.available]
    force = data.force_yz_m_s2[data.available]
    tilt = accel_roll(force)
    states = {name: np.empty((len(times), 2)) for name in filters}
    covariance = {name: np.empty((len(times), 2, 2)) for name in filters}
    innovations = {
        "kalman": np.empty((len(indices), 1)),
        "ekf": np.empty((len(indices), 2)),
    }
    innovation_covariance = {
        "kalman": np.empty((len(indices), 1, 1)),
        "ekf": np.empty((len(indices), 2, 2)),
    }
    nis = {name: np.empty(len(indices)) for name in filters}
    complementary_angles = np.empty(len(times))
    complementary_angles[0] = initial_angle
    for name, estimator in filters.items():
        states[name][0], covariance[name][0] = estimator.state, estimator.covariance

    next_observation, previous_observation_time = 0, times[0]
    for endpoint, dt in enumerate(np.diff(times), start=1):
        rate = float(data.gyro_rate_rad_s[endpoint - 1])
        for estimator in filters.values():
            estimator.predict(rate, float(dt))
        complementary.predict(rate, float(dt))
        if next_observation < len(indices) and endpoint == indices[next_observation]:
            j = next_observation
            elapsed = float(times[endpoint] - previous_observation_time)
            complementary.update(float(tilt[j]), elapsed)
            angle_innovation, angle_variance = filters["kalman"].update_wrapped_angle(float(tilt[j]))
            innovations["kalman"][j, 0] = angle_innovation
            innovation_covariance["kalman"][j, 0, 0] = angle_variance
            nis["kalman"][j] = angle_innovation**2 / angle_variance
            vector_innovation, vector_covariance = filters["ekf"].update(force[j])
            innovations["ekf"][j] = vector_innovation
            innovation_covariance["ekf"][j] = vector_covariance
            nis["ekf"][j] = vector_innovation @ np.linalg.solve(
                vector_covariance, vector_innovation
            )
            previous_observation_time = times[endpoint]
            next_observation += 1
        for name, estimator in filters.items():
            states[name][endpoint], covariance[name][endpoint] = estimator.state, estimator.covariance
        complementary_angles[endpoint] = complementary.angle_rad

    estimates = {
        "gyro": integrate_gyro(times, data.gyro_rate_rad_s, initial_angle_rad=initial_angle),
        "complementary": complementary_angles,
        "kalman": states["kalman"][:, 0],
        "ekf": states["ekf"][:, 0],
    }
    metrics = {
        name: summarize_errors(np.rad2deg(angle - data.truth.angle_rad), times)
        for name, angle in estimates.items()
    }
    for name in filters:
        bias_error = np.rad2deg(states[name][:, 1] - data.bias_rad_s)
        metrics[name].update({
            "bias_rmse_deg_s": float(np.sqrt(np.mean(bias_error**2))),
            "late_bias_rmse_deg_s": float(np.sqrt(np.mean(bias_error[times >= 25.0]**2))),
            "max_abs_bias_error_deg_s": float(np.max(np.abs(bias_error))),
            "final_bias_error_deg_s": float(bias_error[-1]),
            "mean_nis": float(np.mean(nis[name])),
        })
    return {
        "data": data,
        "estimates": estimates,
        "states": states,
        "covariance": covariance,
        "observation_indices": indices,
        "innovations": innovations,
        "innovation_covariance": innovation_covariance,
        "nis": nis,
        "metrics": metrics,
    }
