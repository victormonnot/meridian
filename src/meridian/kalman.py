"""Linear roll-angle and constant gyroscope-bias estimation."""

import math

import numpy as np
from numpy.typing import NDArray


class AngleBiasKalman:
    """Estimate unwrapped roll (rad) and constant gyro bias (rad/s).

    The state order is [angle, bias]. Initial uncertainties are independent
    standard deviations, so the initial covariance is diagonal. An exact known
    initial angle may use zero uncertainty; an unknown bias needs nonzero
    uncertainty to be corrected by angle observations.

    Gyro noise is independent between interval-mean rate samples. Its standard
    deviation is in rad/s per sample, not a continuous-time noise density:
    Q = diag([(gyro_noise_std_rad_s * dt_s)**2, 0]). Bias has no random walk.
    Angle observations are direct, unwrapped angles with independent noise,
    H = [1, 0], and R = angle_noise_std_rad**2. This is a one-axis model;
    accelerometer components and wrapped angles are not accepted observations.

    The caller owns timestamps and observation scheduling. Predict once with
    the rate over an interval, then update with an observation at its endpoint
    when one is available. State and covariance properties return copies.
    """

    def __init__(
        self,
        *,
        initial_angle_rad: float,
        initial_bias_rad_s: float,
        initial_angle_std_rad: float,
        initial_bias_std_rad_s: float,
        gyro_noise_std_rad_s: float,
        angle_noise_std_rad: float,
    ) -> None:
        if not math.isfinite(initial_angle_rad) or not math.isfinite(initial_bias_rad_s):
            raise ValueError("initial angle and bias must be finite")
        standard_deviations = {
            "initial_angle_std_rad": initial_angle_std_rad,
            "initial_bias_std_rad_s": initial_bias_std_rad_s,
            "gyro_noise_std_rad_s": gyro_noise_std_rad_s,
            "angle_noise_std_rad": angle_noise_std_rad,
        }
        variances = {}
        for name, value in standard_deviations.items():
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")
            variance = float(value) * float(value)
            if not math.isfinite(variance):
                raise ValueError(f"{name} must have a finite squared value")
            variances[name] = variance
        if variances["angle_noise_std_rad"] <= 0.0:
            raise ValueError("angle_noise_std_rad must have a strictly positive variance")

        self._state = np.array([initial_angle_rad, initial_bias_rad_s], dtype=np.float64)
        self._covariance = np.diag([
            variances["initial_angle_std_rad"], variances["initial_bias_std_rad_s"]
        ])
        self._gyro_noise_std_rad_s = float(gyro_noise_std_rad_s)
        self._angle_variance_rad2 = variances["angle_noise_std_rad"]

    @property
    def state(self) -> NDArray[np.float64]:
        """Current [angle_rad, bias_rad_s], without access to internal storage."""
        return self._state.copy()

    @property
    def covariance(self) -> NDArray[np.float64]:
        """Current covariance; diagonal units are rad² and (rad/s)²."""
        return self._covariance.copy()

    def predict(self, rate_rad_s: float, dt_s: float) -> None:
        """Propagate through one interval using its mean measured gyro rate."""
        if not math.isfinite(rate_rad_s):
            raise ValueError("rate_rad_s must be finite")
        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError("dt_s must be finite and positive")

        transition = np.array([[1.0, -dt_s], [0.0, 1.0]])
        state = self._state.copy()
        with np.errstate(over="ignore", invalid="ignore"):
            state[0] += dt_s * (rate_rad_s - state[1])
            covariance = transition @ self._covariance @ transition.T
            angle_noise_std = self._gyro_noise_std_rad_s * dt_s
            covariance[0, 0] += angle_noise_std * angle_noise_std
        if not np.all(np.isfinite(state)) or not np.all(np.isfinite(covariance)):
            raise ValueError("prediction exceeds the finite numerical range")
        self._state = state
        self._covariance = 0.5 * covariance + 0.5 * covariance.T

    def update(self, angle_rad: float) -> tuple[float, float]:
        """Correct at the current time; return prior innovation and its variance.

        innovation = observed angle - predicted angle, in radians. Its variance
        S = P[0, 0] + R is in rad². Neither residual nor state angle is wrapped.
        A Joseph covariance update preserves the covariance's numerical form.
        """
        if not math.isfinite(angle_rad):
            raise ValueError("angle_rad must be finite")
        with np.errstate(over="ignore", invalid="ignore"):
            innovation = float(angle_rad - self._state[0])
            innovation_variance = float(self._covariance[0, 0] + self._angle_variance_rad2)
        if not math.isfinite(innovation) or not math.isfinite(innovation_variance):
            raise ValueError("innovation exceeds the finite numerical range")

        gain = self._covariance[:, 0] / innovation_variance
        correction = np.eye(2)
        correction[:, 0] -= gain  # I - K H, where H = [1, 0].
        with np.errstate(over="ignore", invalid="ignore"):
            state = self._state + gain * innovation
            covariance = (
                correction @ self._covariance @ correction.T
                + self._angle_variance_rad2 * np.outer(gain, gain)
            )
        if not np.all(np.isfinite(state)) or not np.all(np.isfinite(covariance)):
            raise ValueError("correction exceeds the finite numerical range")
        self._state = state
        self._covariance = 0.5 * covariance + 0.5 * covariance.T
        return innovation, innovation_variance
