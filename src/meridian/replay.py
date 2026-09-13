"""Causal one-axis replay of selected, contiguous IMU frontend snapshots."""

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

from meridian.kalman import AngleBiasKalman
from meridian.tilt import ComplementaryRoll, accel_roll


@dataclass(frozen=True)
class ReplayConfig:
    """Explicit replay assumptions, not noise identified from a physical IMU.

    Standard deviations are in degrees per second and degrees as named. Gyro
    noise is modeled as independent between held snapshots: Q_theta = sigma_g²
    dt². This effective approximation ignores filtering and sample correlation.
    The angle observation variance is fixed; there is no acceleration gate,
    bias random walk, or timing/synchronization uncertainty in the covariance.
    """

    gyro_noise_std_deg_s: float = 1.0
    angle_noise_std_deg: float = 2.0
    initial_bias_std_deg_s: float = 1.0
    complementary_tau_s: float = 1.0
    max_gap_s: float = 0.2

    def __post_init__(self) -> None:
        for name in (
            "gyro_noise_std_deg_s", "angle_noise_std_deg", "initial_bias_std_deg_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")
            radians_std = math.radians(value)
            variance = radians_std * radians_std
            if not math.isfinite(variance):
                raise ValueError(f"{name} must give a finite variance in radians")
            if name == "angle_noise_std_deg" and variance <= 0.0:
                raise ValueError("angle_noise_std_deg must give a strictly positive variance")
        for name in ("complementary_tau_s", "max_gap_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class ReplayResult:
    """Estimates at N snapshot epochs, plus N-1 pre-correction innovations.

    All angle estimates except accelerometer tilt remain unwrapped. State order
    is [roll_rad, bias_rad_s]. Innovation index j belongs to snapshot j+1.
    Arrays own their storage independently of the inputs; freezing the container
    does not make the arrays immutable.
    """

    time_us: NDArray[np.int64]
    elapsed_s: NDArray[np.float64]
    accel_roll_rad: NDArray[np.float64]
    gyro_roll_rad: NDArray[np.float64]
    complementary_roll_rad: NDArray[np.float64]
    kalman_state: NDArray[np.float64]
    kalman_covariance: NDArray[np.float64]
    innovation_rad: NDArray[np.float64]
    innovation_variance_rad2: NDArray[np.float64]


def _timestamps(time_us: ArrayLike) -> NDArray[np.int64]:
    # Inspect values before coercion: [0, True] must not become [0, 1].
    values = np.asarray(time_us, dtype=object)
    if values.ndim != 1 or len(values) < 2:
        raise ValueError("time_us must have shape (N,) with at least two timestamps")
    maximum = np.iinfo(np.int64).max
    if any(
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, (int, np.integer))
        or not 0 <= value <= maximum
        for value in values
    ):
        raise ValueError("time_us must contain nonnegative int64-range integers, not booleans")
    times = np.array(values, dtype=np.int64)
    if np.any(np.diff(times) <= 0):
        raise ValueError("time_us must be strictly increasing in original order")
    return times


def _sensor_values(values: ArrayLike, count: int, name: str) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (count, 3):
        raise ValueError(f"{name} must have shape (N, 3), matching time_us")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite components")
    return array


def replay_snapshots(
    time_us: ArrayLike,
    gyro_rad_s: ArrayLike,
    accel_m_s2: ArrayLike,
    *,
    config: ReplayConfig = ReplayConfig(),
) -> ReplayResult:
    """Replay one contiguous IMU stream with an explicit previous-snapshot hold.

    Inputs are N integer message timestamps in microseconds and N x 3 gyro and
    specific-force snapshots in the forward-right-down body frame. At epoch k,
    propagate with gyro_x[k-1] over the actual elapsed interval, then correct
    with atan2(-accel_y[k], -accel_z[k]). The last gyro sample is unused. This is
    causal rectangular quadrature of logged frontend snapshots, not a claim
    that they are interval means or simultaneous sensor samples.

    All methods start at the first accelerometer tilt. Kalman starts with zero
    bias, angular variance equal to the configured observation variance, and
    independent configured bias uncertainty. The first tilt is not applied a
    second time as a correction. Every later snapshot produces a correction.

    Reject invalid samples, duplicate/backward timestamps, and gaps exceeding
    max_gap_s. Never sort, drop, resample, or bridge a gap by resetting a filter.
    The caller selects the instance and acquisition interval. No ground truth,
    arming state, or physical validity of the one-axis assumptions is inferred.
    """
    times = _timestamps(time_us)
    # Subtract integer epochs first to preserve small intervals near int64 max.
    intervals_s = np.diff(times).astype(np.float64) / 1_000_000.0
    if np.any(intervals_s > config.max_gap_s):
        raise ValueError("time_us contains a gap exceeding max_gap_s")
    gyro = _sensor_values(gyro_rad_s, len(times), "gyro_rad_s")
    accel = _sensor_values(accel_m_s2, len(times), "accel_m_s2")
    tilt = np.asarray(accel_roll(accel[:, 1:3]), dtype=np.float64)
    initial_angle = float(tilt[0])
    angle_std = math.radians(config.angle_noise_std_deg)
    kalman = AngleBiasKalman(
        initial_angle_rad=initial_angle,
        initial_bias_rad_s=0.0,
        initial_angle_std_rad=angle_std,
        initial_bias_std_rad_s=math.radians(config.initial_bias_std_deg_s),
        gyro_noise_std_rad_s=math.radians(config.gyro_noise_std_deg_s),
        angle_noise_std_rad=angle_std,
    )
    complementary = ComplementaryRoll(initial_angle, config.complementary_tau_s)
    gyro_roll = np.empty(len(times), dtype=np.float64)
    complementary_roll = np.empty(len(times), dtype=np.float64)
    states = np.empty((len(times), 2), dtype=np.float64)
    covariance = np.empty((len(times), 2, 2), dtype=np.float64)
    innovation = np.empty(len(times) - 1, dtype=np.float64)
    innovation_variance = np.empty(len(times) - 1, dtype=np.float64)
    gyro_roll[0] = complementary_roll[0] = initial_angle
    states[0], covariance[0] = kalman.state, kalman.covariance
    for k, interval in enumerate(intervals_s, start=1):
        rate, dt = float(gyro[k - 1, 0]), float(interval)
        integrated_angle = float(gyro_roll[k - 1]) + rate * dt
        if not math.isfinite(integrated_angle):
            raise ValueError("gyro integration exceeds the finite numerical range")
        gyro_roll[k] = integrated_angle
        kalman.predict(rate, dt)
        complementary.predict(rate, dt)
        complementary.update(float(tilt[k]), dt)
        innovation[k - 1], innovation_variance[k - 1] = kalman.update_wrapped_angle(float(tilt[k]))
        states[k], covariance[k] = kalman.state, kalman.covariance
        complementary_roll[k] = complementary.angle_rad
    return ReplayResult(
        time_us=times,
        elapsed_s=(times - times[0]).astype(np.float64) / 1_000_000.0,
        accel_roll_rad=tilt,
        gyro_roll_rad=gyro_roll,
        complementary_roll_rad=complementary_roll,
        kalman_state=states,
        kalman_covariance=covariance,
        innovation_rad=innovation,
        innovation_variance_rad2=innovation_variance,
    )
