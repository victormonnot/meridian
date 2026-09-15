"""Roll/bias EKF with a gravity observation and optional bias random walk."""

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


def _validate_gravity(gravity_m_s2: float) -> None:
    if not math.isfinite(gravity_m_s2) or gravity_m_s2 <= 0.0:
        raise ValueError("gravity_m_s2 must be finite and positive")


def gravity_observation(
    angle_rad: float, gravity_m_s2: float = 9.80665
) -> NDArray[np.float64]:
    """Return predicted [f_y, f_z] specific force (m/s²) for pure roll.

    Body axes are forward/right/down. With no translational acceleration,
    a level sensor measures [0, -g]; positive roll makes f_y negative.
    The gyro bias does not appear in this observation function.
    """
    if not math.isfinite(angle_rad):
        raise ValueError("angle_rad must be finite")
    _validate_gravity(gravity_m_s2)
    return np.array([
        -gravity_m_s2 * math.sin(angle_rad),
        -gravity_m_s2 * math.cos(angle_rad),
    ], dtype=np.float64)


def gravity_jacobian(
    angle_rad: float, gravity_m_s2: float = 9.80665
) -> NDArray[np.float64]:
    """Return d[f_y, f_z]/d[angle_rad, bias_rad_s], evaluated at the prior.

    The first column is [-g*cos(angle), g*sin(angle)]; the bias column is
    zero. Bias corrections require angle/bias cross-covariance from prediction.
    """
    if not math.isfinite(angle_rad):
        raise ValueError("angle_rad must be finite")
    _validate_gravity(gravity_m_s2)
    return np.array([
        [-gravity_m_s2 * math.cos(angle_rad), 0.0],
        [gravity_m_s2 * math.sin(angle_rad), 0.0],
    ], dtype=np.float64)


class AngleBiasEKF:
    """Estimate unwrapped roll (rad) and gyro bias (rad/s).

    State order is [angle, bias]. Prediction uses independent interval-mean
    gyro noise with Q = diag([(gyro_noise_std_rad_s * dt_s)**2, 0]). Its
    standard deviation is per sample, not a continuous-time noise density.
    Initial uncertainties are independent; a known initial angle may have
    zero uncertainty. Bias is constant by default. A positive
    bias_random_walk_std_rad_s_per_sqrt_s adds continuous bias diffusion:
    Q_bias = sigma_b^2 * [[dt^3/3, -dt^2/2], [-dt^2/2, dt]]. Its units are
    (rad/s)/sqrt(s), unlike the gyro's per-interval standard deviation. Mean
    bias stays unchanged during prediction; no drift slope is predicted.

    The observation is the unnormalized body y/z specific-force vector:
    h = [-g*sin(angle), -g*cos(angle)]. R = accel_noise_std_m_s2**2 * I
    assumes independent, equal-variance component noise in m/s². Each update
    linearizes once at the prior and jointly corrects with both components.
    A Joseph covariance update preserves the covariance's numerical form.

    This is a local, pure-roll gravity model: no translation, other rotation
    axes, adaptive noise, norm rejection, or iterated correction is modeled.
    Finite zero-force observations are accepted as model-mismatched data.
    Radial innovations can reduce covariance without correcting the angle;
    an exactly opposite gravity vector can leave the initial error unchanged.
    Convergence from an arbitrary initial angle is not guaranteed, and vector
    observations cannot recover an unknown number of complete turns.

    The caller owns timestamps and observation scheduling. Predict with the
    interval's measured mean rate and update at its endpoint when available.
    State/covariance properties return copies; failed operations are atomic.
    """

    def __init__(
        self,
        *,
        initial_angle_rad: float,
        initial_bias_rad_s: float,
        initial_angle_std_rad: float,
        initial_bias_std_rad_s: float,
        gyro_noise_std_rad_s: float,
        accel_noise_std_m_s2: float,
        gravity_m_s2: float = 9.80665,
        bias_random_walk_std_rad_s_per_sqrt_s: float = 0.0,
    ) -> None:
        if not math.isfinite(initial_angle_rad) or not math.isfinite(initial_bias_rad_s):
            raise ValueError("initial angle and bias must be finite")
        _validate_gravity(gravity_m_s2)
        standard_deviations = {
            "initial_angle_std_rad": initial_angle_std_rad,
            "initial_bias_std_rad_s": initial_bias_std_rad_s,
            "gyro_noise_std_rad_s": gyro_noise_std_rad_s,
            "accel_noise_std_m_s2": accel_noise_std_m_s2,
            "bias_random_walk_std_rad_s_per_sqrt_s": bias_random_walk_std_rad_s_per_sqrt_s,
        }
        variances = {}
        for name, value in standard_deviations.items():
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")
            variance = float(value) * float(value)
            if not math.isfinite(variance):
                raise ValueError(f"{name} must have a finite squared value")
            variances[name] = variance
        if variances["accel_noise_std_m_s2"] <= 0.0:
            raise ValueError("accel_noise_std_m_s2 must have a strictly positive variance")

        self._state = np.array([initial_angle_rad, initial_bias_rad_s], dtype=np.float64)
        self._covariance = np.diag([
            variances["initial_angle_std_rad"], variances["initial_bias_std_rad_s"]
        ])
        self._gyro_noise_std_rad_s = float(gyro_noise_std_rad_s)
        self._bias_random_walk_variance = variances["bias_random_walk_std_rad_s_per_sqrt_s"]
        self._accel_covariance = np.eye(2) * variances["accel_noise_std_m_s2"]
        self._gravity_m_s2 = float(gravity_m_s2)

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
            if self._bias_random_walk_variance > 0.0:
                # Integrate the same bias disturbance into angle and bias;
                # the negative cross term follows from subtracting gyro bias.
                bias_variance = self._bias_random_walk_variance * dt_s
                cross_scale = bias_variance * dt_s
                covariance[0, 0] += cross_scale * dt_s / 3.0
                covariance[0, 1] -= cross_scale / 2.0
                covariance[1, 0] -= cross_scale / 2.0
                covariance[1, 1] += bias_variance
        if not np.all(np.isfinite(state)) or not np.all(np.isfinite(covariance)):
            raise ValueError("prediction exceeds the finite numerical range")
        self._state = state
        self._covariance = 0.5 * covariance + 0.5 * covariance.T

    def update(
        self, force_yz_m_s2: ArrayLike
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Correct with [f_y, f_z]; return prior innovation (m/s²) and S.

        S = H P H.T + R is a 2 x 2 covariance in (m/s²)². Both h and H
        use the same prior angle; the update does not normalize measurements
        or wrap the state. Inputs and returned diagnostics own no internal
        storage. A finite vector need not be consistent with the gravity model.
        """
        observation = np.asarray(force_yz_m_s2, dtype=np.float64)
        if observation.shape != (2,) or not np.all(np.isfinite(observation)):
            raise ValueError("force_yz_m_s2 must be a finite vector with shape (2,)")

        expected = gravity_observation(self._state[0], self._gravity_m_s2)
        jacobian = gravity_jacobian(self._state[0], self._gravity_m_s2)
        with np.errstate(over="ignore", invalid="ignore"):
            innovation = observation - expected
            cross_covariance = self._covariance @ jacobian.T
            innovation_covariance = jacobian @ cross_covariance + self._accel_covariance
        if not all(np.all(np.isfinite(value)) for value in (
            innovation, cross_covariance, innovation_covariance
        )):
            raise ValueError("innovation exceeds the finite numerical range")
        innovation_covariance = (
            0.5 * innovation_covariance + 0.5 * innovation_covariance.T
        )
        try:
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                gain = np.linalg.solve(innovation_covariance, cross_covariance.T).T
        except np.linalg.LinAlgError as error:
            raise ValueError("innovation covariance is numerically singular") from error
        with np.errstate(over="ignore", invalid="ignore"):
            state = self._state + gain @ innovation
            correction = np.eye(2) - gain @ jacobian
            covariance = (
                correction @ self._covariance @ correction.T
                + gain @ self._accel_covariance @ gain.T
            )
        if not np.all(np.isfinite(state)) or not np.all(np.isfinite(covariance)):
            raise ValueError("correction exceeds the finite numerical range")
        self._state = state
        self._covariance = 0.5 * covariance + 0.5 * covariance.T
        return innovation, innovation_covariance
