"""Gyroscope-only roll integration, independent of simulated truth."""

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


def integrate_gyro(
    time_s: ArrayLike,
    rate_rad_s: ArrayLike,
    *,
    initial_angle_rad: float = 0.0,
) -> NDArray[np.float64]:
    """Integrate N interval angular rates into N + 1 endpoint roll angles.

    time_s contains finite, strictly increasing endpoints in seconds;
    rate_rad_s[k] applies to [time_s[k], time_s[k + 1]] in rad/s.
    Each actual interval duration is used, including on irregular grids.
    The initial angle in radians is supplied externally; no angle or bias
    truth is available to this function. Returned angles remain unwrapped.

    This is a one-axis roll model. A gyro's body-x rate is not generally the
    roll-angle derivative during arbitrary three-dimensional rotation.
    """
    times = np.asarray(time_s, dtype=np.float64)
    rates = np.asarray(rate_rad_s, dtype=np.float64)
    if times.ndim != 1 or times.size < 2 or not np.all(np.isfinite(times)):
        raise ValueError("time_s must be a finite 1D array with at least two endpoints")
    if rates.ndim != 1 or rates.size != times.size - 1 or not np.all(np.isfinite(rates)):
        raise ValueError("rate_rad_s must be a finite 1D array with one rate per interval")
    dt_s = np.diff(times)
    if not np.all(np.isfinite(dt_s)) or np.any(dt_s <= 0.0):
        raise ValueError("time_s must have finite, strictly positive interval durations")
    if not math.isfinite(initial_angle_rad):
        raise ValueError("initial_angle_rad must be finite")

    angles = np.empty(times.size, dtype=np.float64)
    angles[0] = initial_angle_rad
    angles[1:] = initial_angle_rad + np.cumsum(rates * dt_s)
    return angles
