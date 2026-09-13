"""One-axis accelerometer tilt and a fixed-gain complementary baseline."""

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


def wrap_angle(angle_rad: ArrayLike) -> NDArray[np.float64] | np.float64:
    """Return finite angles in [-pi, pi), preserving the input array shape."""
    angles = np.asarray(angle_rad, dtype=np.float64)
    if not np.all(np.isfinite(angles)):
        raise ValueError("angle_rad must contain only finite angles")
    return (angles + np.pi) % (2.0 * np.pi) - np.pi


def accel_roll(force_yz_m_s2: ArrayLike) -> NDArray[np.float64] | np.float64:
    """Extract wrapped roll from specific force with final dimension [f_y, f_z].

    The body frame is forward-right-down. For pure roll and negligible
    translational acceleration, f_y = -g*sin(angle), f_z = -g*cos(angle).
    Input units are m/s², and the returned angle is in radians in [-pi, pi).

    A y/z norm <= 1e-9 m/s² is numerically degenerate and is rejected. This
    threshold is not a physical gravity-magnitude or acceleration-disturbance
    gate. A nonzero force vector does not establish a valid tilt observation.
    """
    force = np.asarray(force_yz_m_s2, dtype=np.float64)
    if force.ndim < 1 or force.shape[-1] != 2 or force.size == 0:
        raise ValueError("force_yz_m_s2 must be nonempty with shape (..., 2)")
    if not np.all(np.isfinite(force)):
        raise ValueError("force_yz_m_s2 must contain only finite components")
    with np.errstate(over="ignore"):
        magnitude = np.hypot(force[..., 0], force[..., 1])
    if not np.all(np.isfinite(magnitude)):
        raise ValueError("specific-force magnitude exceeds the finite numerical range")
    if np.any(magnitude <= 1e-9):
        raise ValueError("specific-force y/z norm must exceed 1e-9 m/s²")
    return wrap_angle(np.arctan2(-force[..., 0], -force[..., 1]))


class ComplementaryRoll:
    """Combine gyro propagation with wrapped tilt corrections; estimate no bias.

    The state remains unwrapped. A correction uses the shortest angular
    innovation and gain 1 - exp(-elapsed_s/tau_s), where elapsed_s is the time
    since the previous accelerometer correction (or initialization for the
    first correction). The caller owns timestamps and observation scheduling.
    There is no covariance, adaptive gain, or disturbance rejection.
    """

    def __init__(self, initial_angle_rad: float, tau_s: float) -> None:
        if not math.isfinite(initial_angle_rad):
            raise ValueError("initial_angle_rad must be finite")
        if not math.isfinite(tau_s) or tau_s <= 0.0:
            raise ValueError("tau_s must be finite and positive")
        self._angle_rad = float(initial_angle_rad)
        self._tau_s = float(tau_s)

    @property
    def angle_rad(self) -> float:
        """Current unwrapped roll estimate in radians."""
        return self._angle_rad

    def predict(self, rate_rad_s: float, dt_s: float) -> None:
        """Integrate one mean gyro rate over its actual interval duration."""
        if not math.isfinite(rate_rad_s):
            raise ValueError("rate_rad_s must be finite")
        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError("dt_s must be finite and positive")
        angle = self._angle_rad + float(rate_rad_s) * float(dt_s)
        if not math.isfinite(angle):
            raise ValueError("prediction exceeds the finite numerical range")
        self._angle_rad = angle

    def update(self, observed_angle_rad: float, elapsed_s: float) -> None:
        """Correct at the current time with a new accelerometer tilt observation."""
        if not math.isfinite(observed_angle_rad):
            raise ValueError("observed_angle_rad must be finite")
        if not math.isfinite(elapsed_s) or elapsed_s <= 0.0:
            raise ValueError("elapsed_s must be finite and positive")
        difference = float(observed_angle_rad) - self._angle_rad
        if not math.isfinite(difference):
            raise ValueError("innovation exceeds the finite numerical range")
        # expm1 retains precision when the correction interval is short.
        gain = -math.expm1(-float(elapsed_s) / self._tau_s)
        angle = self._angle_rad + gain * float(wrap_angle(difference))
        if not math.isfinite(angle):
            raise ValueError("correction exceeds the finite numerical range")
        self._angle_rad = angle
