"""One-axis roll truth and idealized gyro, angle, and accelerometer measurements."""

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


@dataclass(frozen=True)
class RollTruth:
    """Truth at N + 1 endpoints and mean angular rates over N intervals.

    Time is in seconds, angles in radians, and angular rates in rad/s.
    Positive roll follows the right-hand rule around the forward body axis.
    """

    time_s: NDArray[np.float64]
    angle_rad: NDArray[np.float64]
    interval_rate_rad_s: NDArray[np.float64]


def simulate_roll(
    *,
    duration_s: float = 30.0,
    sample_rate_hz: float = 100.0,
    amplitude_rad: float = np.deg2rad(20.0),
    frequency_hz: float = 0.1,
) -> RollTruth:
    """Generate sinusoidal roll with theta(t) = A * sin(2 * pi * f * t).

    The interval count duration_s * sample_rate_hz must be a positive integer.
    Rates are exact interval averages, computed from endpoint angle changes.
    They isolate sensor drift from numerical quadrature error: these samples
    do not represent instantaneous rates or a verified real-log convention.
    A zero amplitude or frequency describes a stationary level body.
    """
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise ValueError("duration_s must be finite and positive")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if not math.isfinite(amplitude_rad):
        raise ValueError("amplitude_rad must be finite")
    if not math.isfinite(frequency_hz) or not 0.0 <= frequency_hz < sample_rate_hz / 2.0:
        raise ValueError("frequency_hz must be finite, nonnegative, and below Nyquist")

    interval_count = duration_s * sample_rate_hz
    if not math.isfinite(interval_count):
        raise ValueError("duration_s * sample_rate_hz must be finite")
    rounded_count = round(interval_count)
    if rounded_count < 1 or not math.isclose(
        interval_count, rounded_count, rel_tol=0.0, abs_tol=1e-9
    ):
        raise ValueError("duration_s * sample_rate_hz must be a positive integer")

    time_s = np.linspace(0.0, duration_s, rounded_count + 1)
    angle_rad = amplitude_rad * np.sin(2.0 * np.pi * frequency_hz * time_s)
    interval_rate_rad_s = np.diff(angle_rad) / np.diff(time_s)
    return RollTruth(time_s, angle_rad, interval_rate_rad_s)


def sample_gyro(
    interval_rate_rad_s: ArrayLike,
    *,
    bias_rad_s: float = 0.0,
    noise_std_rad_s: float = 0.0,
    seed: int = 42,
) -> NDArray[np.float64]:
    """Add constant bias and independent Gaussian noise to interval rates.

    noise_std_rad_s is the standard deviation of each interval's mean-rate
    sample in rad/s, not a continuous-time noise density. Holding it constant
    while changing the sample rate changes the integrated noise statistics.
    A local PCG64 generator makes a seed reproducible without changing global
    NumPy random state. The input is never modified.
    """
    rates = np.asarray(interval_rate_rad_s, dtype=np.float64)
    if rates.ndim != 1 or rates.size == 0 or not np.all(np.isfinite(rates)):
        raise ValueError("interval_rate_rad_s must be a nonempty finite 1D array")
    if not math.isfinite(bias_rad_s):
        raise ValueError("bias_rad_s must be finite")
    if not math.isfinite(noise_std_rad_s) or noise_std_rad_s < 0.0:
        raise ValueError("noise_std_rad_s must be finite and nonnegative")
    if (
        isinstance(seed, (bool, np.bool_))
        or not isinstance(seed, (int, np.integer))
        or seed < 0
    ):
        raise ValueError("seed must be a nonnegative integer")

    rng = np.random.Generator(np.random.PCG64(seed))
    noise_rad_s = rng.normal(0.0, noise_std_rad_s, size=rates.size)
    return rates + bias_rad_s + noise_rad_s


def sample_angle(
    angle_rad: ArrayLike,
    *,
    noise_std_rad: float,
    seed: int,
) -> NDArray[np.float64]:
    """Generate independent noisy, unwrapped angle observations in radians.

    This is a direct synthetic observation, not an accelerometer model.
    noise_std_rad is the standard deviation per observation, not a density.
    """
    angles = np.asarray(angle_rad, dtype=np.float64)
    if angles.ndim != 1 or angles.size == 0 or not np.all(np.isfinite(angles)):
        raise ValueError("angle_rad must be a nonempty finite 1D array")
    if not math.isfinite(noise_std_rad) or noise_std_rad < 0.0:
        raise ValueError("noise_std_rad must be finite and nonnegative")
    if (
        isinstance(seed, (bool, np.bool_))
        or not isinstance(seed, (int, np.integer))
        or seed < 0
    ):
        raise ValueError("seed must be a nonnegative integer")
    rng = np.random.Generator(np.random.PCG64(seed))
    return angles + rng.normal(0.0, noise_std_rad, size=angles.size)


def sample_accelerometer(
    roll_rad: ArrayLike,
    *,
    noise_std_m_s2: float,
    seed: int,
    gravity_m_s2: float = 9.80665,
    translation_yz_m_s2: ArrayLike | None = None,
) -> NDArray[np.float64]:
    """Sample endpoint specific force [f_y, f_z] for pure roll, in m/s².

    In a forward-right-down body frame, f = [-g*sin(roll), -g*cos(roll)] + a.
    a is prescribed translational acceleration of the IMU, resolved in body y/z,
    either a constant pair or one pair per endpoint. No lever arm or full vehicle
    dynamics is modeled. Samples have independent Gaussian noise per axis with
    the specified per-sample standard deviation, not a continuous noise density.
    """
    angles = np.asarray(roll_rad, dtype=np.float64)
    if angles.ndim != 1 or angles.size == 0 or not np.all(np.isfinite(angles)):
        raise ValueError("roll_rad must be a nonempty finite 1D array")
    if not math.isfinite(gravity_m_s2) or gravity_m_s2 <= 0.0:
        raise ValueError("gravity_m_s2 must be finite and positive")
    if not math.isfinite(noise_std_m_s2) or noise_std_m_s2 < 0.0:
        raise ValueError("noise_std_m_s2 must be finite and nonnegative")
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    shape = (len(angles), 2)
    translation = np.zeros(shape) if translation_yz_m_s2 is None else np.asarray(
        translation_yz_m_s2, dtype=np.float64
    )
    if translation.shape not in ((2,), shape) or not np.all(np.isfinite(translation)):
        raise ValueError("translation_yz_m_s2 must be a finite pair or an (N, 2) array")
    rng = np.random.Generator(np.random.PCG64(seed))
    with np.errstate(over="ignore", invalid="ignore"):
        force = -gravity_m_s2 * np.column_stack((np.sin(angles), np.cos(angles)))
        force = force + translation + rng.normal(0.0, noise_std_m_s2, size=shape)
    if not np.all(np.isfinite(force)):
        raise ValueError("accelerometer values exceed the finite numerical range")
    return force
