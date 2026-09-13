"""Fixed, paired one-axis sensor scenarios with separate truth and availability."""

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

from meridian.simulation import RollTruth, sample_accelerometer, simulate_roll


DURATION_S = 30.0
INTERVAL_COUNT = 3000
OBSERVATION_EVERY = 10
GRAVITY_M_S2 = 9.80665
GYRO_NOISE_STD_RAD_S = float(np.deg2rad(1.0))
BASE_BIAS_RAD_S = float(np.deg2rad(0.5))
BIAS_SLOPE_RAD_S2 = float(np.deg2rad(0.1))


@dataclass(frozen=True)
class Scenario:
    """One controlled change to fixed simulation and initialization settings.

    Initial angles and their model standard deviations are filter inputs, not
    truth changes. Sensor standard deviation is per component and observation.
    Delay is sample-to-arrival time; delayed observations remain tagged with
    both times so a runner can deliberately test an ignored delay.
    """

    name: str
    description: str
    initial_angle_deg: float = 0.0
    initial_angle_std_deg: float = 0.0
    bias_ramp: bool = False
    accel_dropout: bool = False
    timing_jitter: bool = False
    accel_noise_std_m_s2: float = 0.2
    accel_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a nonempty string")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError("description must be a nonempty string")
        for name in ("initial_angle_deg", "initial_angle_std_deg", "accel_noise_std_m_s2", "accel_delay_s"):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
                raise ValueError(f"{name} must be a finite number")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if name != "initial_angle_deg" and value < 0.0:
                raise ValueError(f"{name} must be nonnegative")
        for name in ("bias_ramp", "accel_dropout", "timing_jitter"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")


SCENARIOS = (
    Scenario("nominal", "Known initial roll, constant bias, matched noise, and uniform timestamps."),
    Scenario("initial_offset", "Initial roll is 60 deg with a declared 30 deg model standard deviation.",
             initial_angle_deg=60.0, initial_angle_std_deg=30.0),
    Scenario("initial_overconfident", "Initial roll is 60 deg with zero initial angle covariance.",
             initial_angle_deg=60.0),
    Scenario("bias_ramp", "True bias rises from 0.5 to 1.5 deg/s over 10 to 20 s.", bias_ramp=True),
    Scenario("accel_dropout", "Accelerometer observations are unavailable for 12 <= arrival time < 17 s.",
             accel_dropout=True),
    Scenario("timing_jitter", "Positive interval durations vary while preserving 3000 intervals over 30 s.",
             timing_jitter=True),
    Scenario("accel_noise_mismatch", "Actual acceleration noise is 0.6 m/s^2; nominal filter tuning is unchanged.",
             accel_noise_std_m_s2=0.6),
    Scenario("accel_delay", "Accelerometer samples arrive 0.1 s after acquisition; the runner ignores that delay.",
             accel_delay_s=0.1),
)


@dataclass(frozen=True)
class ScenarioData:
    """Generated arrays in SI units, with N intervals and M accelerometer rows.

    truth and bias_rad_s use N + 1 endpoints; interval_bias_rad_s contains the
    true mean bias contributing to each gyro rate. observation_indices index
    arrival endpoints, while sample_time_s describes physical acquisition.
    force_yz_m_s2 retains all M generated rows; only rows marked available may
    reach an estimator. Frozen fields do not make the contained arrays read-only.
    """

    truth: RollTruth
    gyro_rate_rad_s: NDArray[np.float64]
    bias_rad_s: NDArray[np.float64]
    interval_bias_rad_s: NDArray[np.float64]
    observation_indices: NDArray[np.int64]
    sample_time_s: NDArray[np.float64]
    force_yz_m_s2: NDArray[np.float64]
    available: NDArray[np.bool_]
    stream_seeds: dict[str, int]


def _time_array(time_s: ArrayLike) -> NDArray[np.float64]:
    times = np.asarray(time_s, dtype=np.float64)
    if times.size == 0 or not np.all(np.isfinite(times)) or np.any(times < 0.0):
        raise ValueError("times must be nonempty, finite, and nonnegative")
    return times


def bias_at_time(time_s: ArrayLike, *, bias_ramp: bool = False) -> NDArray[np.float64]:
    """Evaluate true instantaneous bias in rad/s; the ramp is active from 10 to 20 s."""
    times = _time_array(time_s)
    if bias_ramp:
        return BASE_BIAS_RAD_S + BIAS_SLOPE_RAD_S2 * np.clip(times - 10.0, 0.0, 10.0)
    return np.full_like(times, BASE_BIAS_RAD_S)


def bias_interval_mean(
    start_s: ArrayLike, end_s: ArrayLike, *, bias_ramp: bool = False,
) -> NDArray[np.float64]:
    """Return exact mean bias in rad/s, including intervals crossing ramp boundaries.

    Integrate b(t) = b0 + slope * ((t - 10)_+ - (t - 20)_+), then divide
    each integral difference by its actual interval duration.
    """
    starts, ends = np.broadcast_arrays(_time_array(start_s), _time_array(end_s))
    if np.any(ends <= starts):
        raise ValueError("each interval must have end_s > start_s")
    if not bias_ramp:
        return np.full_like(starts, BASE_BIAS_RAD_S)

    def integral(times: NDArray[np.float64]) -> NDArray[np.float64]:
        return BASE_BIAS_RAD_S * times + 0.5 * BIAS_SLOPE_RAD_S2 * (
            np.maximum(times - 10.0, 0.0)**2 - np.maximum(times - 20.0, 0.0)**2
        )

    means = (integral(ends) - integral(starts)) / (ends - starts)
    # Preserve exact constant sections and therefore exactly paired pre-ramp data.
    means = np.where(ends <= 10.0, BASE_BIAS_RAD_S, means)
    return np.where(starts >= 20.0, BASE_BIAS_RAD_S + 10.0 * BIAS_SLOPE_RAD_S2, means)


def generate_scenario(scenario: Scenario, seed: int) -> ScenarioData:
    """Generate matched local PCG64 streams without tuning or running estimators.

    The first two SeedSequence children match the existing acceleration fusion
    experiment. The third controls timing jitter only. Gyro noise is independent
    with a 1 deg/s standard deviation per interval mean, even for irregular
    intervals; this deliberately does not model a continuous noise density.
    """
    if not isinstance(scenario, Scenario):
        raise ValueError("scenario must be a Scenario")
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    streams = {
        name: int(child.generate_state(1, dtype=np.uint64)[0])
        for name, child in zip(("gyro", "accelerometer", "timing"), np.random.SeedSequence(seed).spawn(3))
    }
    truth = simulate_roll()
    if scenario.timing_jitter:
        rng = np.random.Generator(np.random.PCG64(streams["timing"]))
        weights = rng.uniform(0.5, 1.5, INTERVAL_COUNT)
        times = np.concatenate(([0.0], np.cumsum(weights * (DURATION_S / np.sum(weights)))))
        times[-1] = DURATION_S
        angles = np.deg2rad(20.0) * np.sin(2.0 * np.pi * 0.1 * times)
        truth = RollTruth(times, angles, np.diff(angles) / np.diff(times))
    times = truth.time_s
    bias = bias_at_time(times, bias_ramp=scenario.bias_ramp)
    interval_bias = bias_interval_mean(times[:-1], times[1:], bias_ramp=scenario.bias_ramp)
    gyro_rng = np.random.Generator(np.random.PCG64(streams["gyro"]))
    gyro = truth.interval_rate_rad_s + interval_bias + gyro_rng.normal(
        0.0, GYRO_NOISE_STD_RAD_S, size=INTERVAL_COUNT,
    )
    indices = np.arange(OBSERVATION_EVERY, len(times), OBSERVATION_EVERY)
    arrival_time = times[indices]
    sample_time = arrival_time - scenario.accel_delay_s
    if np.any(sample_time < 0.0):
        raise ValueError("accel_delay_s places an acquisition before the simulation begins")
    sample_roll = np.deg2rad(20.0) * np.sin(2.0 * np.pi * 0.1 * sample_time)
    force = sample_accelerometer(
        sample_roll, noise_std_m_s2=scenario.accel_noise_std_m_s2,
        seed=streams["accelerometer"], gravity_m_s2=GRAVITY_M_S2,
    )
    available = np.ones(len(indices), dtype=np.bool_)
    if scenario.accel_dropout:
        available[(arrival_time >= 12.0) & (arrival_time < 17.0)] = False
    return ScenarioData(truth, gyro, bias, interval_bias, indices, sample_time, force, available, streams)
