"""Verify continuous bias diffusion independently and across the native boundary."""

import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from meridian.cpp_parity import (
    compare_traces, python_trace, read_cpp_trace, replay_command, serialize_events,
)
from meridian.ekf import AngleBiasEKF, gravity_observation
from meridian.kalman import AngleBiasKalman


PARAMETER = "bias_random_walk_std_rad_s_per_sqrt_s"
FLAG = "--bias-random-walk-std-rad-s-per-sqrt-s"


def configuration(**overrides):
    values = {
        "initial_angle_rad": 0.2,
        "initial_bias_rad_s": 0.1,
        "initial_angle_std_rad": 0.4,
        "initial_bias_std_rad_s": 0.5,
        "gyro_noise_std_rad_s": 0.3,
        "accel_noise_std_m_s2": 0.2,
        "gravity_m_s2": 9.80665,
    }
    values.update(overrides)
    return values


def irregular_events():
    """Artificial inputs exercise mixed scheduling without evaluation seeds."""
    events = []
    time_s = 0.0
    for step in range(90):
        dt = (0.001, 0.037, 0.12, 0.006, 0.43)[step % 5]
        time_s += dt
        events.append(("predict", float(0.3 * np.cos(time_s) + 0.1), dt))
        if step % 3 != 1:
            force = gravity_observation(float(0.3 * np.sin(time_s)))
            events.append(("update", float(force[0]), float(force[1])))
    return events


@pytest.mark.parametrize("dt", [0.001, 0.03, 0.25, 1.75])
@pytest.mark.parametrize("density", [0.003, 0.2])
def test_bias_covariance_matches_exact_quadrature_of_disturbance_response(dt, density):
    estimator = AngleBiasEKF(**configuration(
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=0.0,
        gyro_noise_std_rad_s=0.0, **{PARAMETER: density},
    ))
    previous_bias = estimator.state[1]
    estimator.predict(0.7, dt)

    # A bias impulse of age u changes angle by -u and bias by +1. Two-point
    # Gauss-Legendre integrates the outer product exactly (degree at most 2).
    nodes, weights = np.polynomial.legendre.leggauss(2)
    ages = (nodes + 1.0) * dt / 2.0
    expected = np.zeros((2, 2))
    for age, weight in zip(ages, weights):
        response = np.array([-age, 1.0])
        expected += density**2 * dt / 2.0 * weight * np.outer(response, response)
    np.testing.assert_allclose(estimator.covariance, expected, rtol=2e-14, atol=0.0)
    assert estimator.covariance[0, 1] < 0.0
    assert np.linalg.det(estimator.covariance) > 0.0
    assert estimator.state[1] == previous_bias


@pytest.mark.parametrize("density", [0.0, 0.003, 0.2])
def test_bias_diffusion_composes_across_irregular_intervals(density):
    settings = configuration(gyro_noise_std_rad_s=0.0, **{PARAMETER: density})
    single, partitioned = AngleBiasEKF(**settings), AngleBiasEKF(**settings)
    intervals = (0.03, 0.13, 0.002, 0.49)
    rate = 0.7
    single.predict(rate, sum(intervals))
    for dt in intervals:
        partitioned.predict(rate, dt)
    np.testing.assert_allclose(partitioned.state, single.state, rtol=1e-14, atol=1e-15)
    np.testing.assert_allclose(partitioned.covariance, single.covariance,
                               rtol=1e-14, atol=1e-15)
    # This partition identity concerns continuous bias diffusion. Independent
    # gyro interval means have a fixed per-sample variance and are disabled.


def test_zero_diffusion_retains_unchanged_linear_prediction_exactly():
    settings = configuration()
    linear_settings = {key: value for key, value in settings.items()
                       if key not in ("accel_noise_std_m_s2", "gravity_m_s2")}
    linear = AngleBiasKalman(**linear_settings, angle_noise_std_rad=0.02)
    estimator = AngleBiasEKF(**settings, **{PARAMETER: 0.0})
    for rate, dt in [(0.4, 0.01), (-0.2, 0.5), (1.1, 2.0), (0.0, 0.037)]:
        estimator.predict(rate, dt)
        linear.predict(rate, dt)
        np.testing.assert_array_equal(estimator.state, linear.state)
        np.testing.assert_array_equal(estimator.covariance, linear.covariance)


def test_implicit_and_explicit_zero_have_identical_complete_operation_traces():
    serialized = serialize_events(irregular_events())
    operations, implicit = python_trace(configuration(), serialized)
    explicit_operations, explicit = python_trace(configuration(**{PARAMETER: 0.0}), serialized)
    assert operations == explicit_operations
    np.testing.assert_array_equal(implicit, explicit)


def test_diffusion_allows_bias_learning_from_zero_initial_bias_variance():
    settings = configuration(initial_angle_rad=0.0, initial_bias_rad_s=0.0,
                             initial_angle_std_rad=0.0, initial_bias_std_rad_s=0.0,
                             gyro_noise_std_rad_s=0.0)
    constant = AngleBiasEKF(**settings)
    variable = AngleBiasEKF(**settings, **{PARAMETER: 0.1})
    variable.predict(0.2, 0.5)
    constant.predict(0.2, 0.5)
    prior_bias_variance = variable.covariance[1, 1]
    assert prior_bias_variance > 0.0 and variable.covariance[0, 1] < 0.0
    variable.update([0.0, -9.80665])
    constant.update([0.0, -9.80665])
    assert 0.0 < variable.state[1] < 0.2
    assert variable.covariance[1, 1] < prior_bias_variance
    assert constant.state[1] == 0.0
    np.testing.assert_array_equal(constant.covariance[1], [0.0, 0.0])


@pytest.mark.parametrize("value", [-0.1, np.nan, np.inf, -np.inf, 1e308])
def test_invalid_diffusion_intensity_is_rejected(value):
    with pytest.raises(ValueError, match=PARAMETER):
        AngleBiasEKF(**configuration(**{PARAMETER: value}))


def test_bias_covariance_overflow_rejects_prediction_atomically():
    estimator = AngleBiasEKF(**configuration(
        initial_angle_rad=0.0, initial_bias_rad_s=0.0,
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=0.0,
        gyro_noise_std_rad_s=0.0, **{PARAMETER: 1e154},
    ))
    previous_state, previous_covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="prediction.*finite"):
        estimator.predict(0.0, 10.0)
    np.testing.assert_array_equal(estimator.state, previous_state)
    np.testing.assert_array_equal(estimator.covariance, previous_covariance)


def test_zero_diffusion_does_not_evaluate_unneeded_large_powers_of_duration():
    estimator = AngleBiasEKF(**configuration(
        initial_angle_rad=0.0, initial_bias_rad_s=0.0,
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=0.0,
        gyro_noise_std_rad_s=0.0, **{PARAMETER: 0.0},
    ))
    estimator.predict(0.0, 1e150)
    np.testing.assert_array_equal(estimator.state, [0.0, 0.0])
    np.testing.assert_array_equal(estimator.covariance, np.zeros((2, 2)))


@pytest.fixture(scope="module")
def binary():
    value = os.environ.get("MERIDIAN_CPP_BINARY")
    if not value:
        pytest.skip("C++ integration requires MERIDIAN_CPP_BINARY; see docs/cpp-ekf.md")
    path = Path(value).resolve()
    assert path.is_file(), f"configured C++ executable is missing: {path}"
    return path


def native_trace(binary, directory, settings, serialized, *, extra=()):
    directory.mkdir()
    source, output = directory / "events.csv", directory / "cpp.csv"
    source.write_text(serialized, encoding="ascii")
    command = replay_command(binary, settings, source, output) + list(extra)
    process = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return process, output


def test_native_optional_density_defaults_to_exactly_zero(binary, tmp_path):
    serialized = serialize_events(irregular_events())
    operations, expected = python_trace(configuration(), serialized)
    implicit, implicit_output = native_trace(binary, tmp_path / "implicit", configuration(), serialized)
    explicit, explicit_output = native_trace(
        binary, tmp_path / "explicit", configuration(**{PARAMETER: 0.0}), serialized,
    )
    assert implicit.returncode == 0, implicit.stderr
    assert explicit.returncode == 0, explicit.stderr
    assert implicit_output.read_bytes() == explicit_output.read_bytes()
    assert compare_traces(expected, read_cpp_trace(implicit_output, operations))["passed"]


@pytest.mark.parametrize("density", [float(np.deg2rad(0.03)), float(np.deg2rad(0.1)), 0.15])
def test_native_positive_diffusion_matches_python_at_every_irregular_operation(binary, tmp_path, density):
    serialized = serialize_events(irregular_events())
    settings = configuration(**{PARAMETER: density})
    operations, expected = python_trace(settings, serialized)
    process, output = native_trace(binary, tmp_path / "positive", settings, serialized)
    assert process.returncode == 0, process.stderr
    actual = read_cpp_trace(output, operations)
    result = compare_traces(expected, actual)
    assert result["passed"], result
    assert result["record_count"] == 151
    _, constant = python_trace(configuration(), serialized)
    assert np.max(np.abs(actual[:, 5] - constant[:, 5])) > 1e-9


@pytest.mark.parametrize("value", ["-0.1", "nan", "inf", "-inf", "1e308"])
def test_native_invalid_diffusion_option_creates_no_output(binary, tmp_path, value):
    process, output = native_trace(
        binary, tmp_path / "invalid", configuration(), serialize_events([]), extra=(FLAG, value),
    )
    assert process.returncode != 0 and process.stderr
    assert not output.exists()


def test_native_duplicate_diffusion_option_is_rejected(binary, tmp_path):
    process, output = native_trace(
        binary, tmp_path / "duplicate", configuration(**{PARAMETER: 0.0}),
        serialize_events([]), extra=(FLAG, "0.1"),
    )
    assert process.returncode != 0 and process.stderr
    assert not output.exists()
