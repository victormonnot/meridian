# Accelerometer-vector roll and bias EKF

The Python EKF estimates the same two states as the [angle Kalman reference](linear-kalman.md):
unwrapped roll `theta` in rad and gyro bias `b` in rad/s, constant by default. Its observation
uses two accelerometer components directly. The sine/cosine observation makes the
model nonlinear; the prediction remains linear. This implementation is evaluated
in simulation and used by the [offline IMU replay](imu-replay.md), with a declared
previous-snapshot hold and shared initialization. It has not been embedded in a
flight controller or validated against a physical angle reference.

## Coordinates and prediction

Body axes point forward, right, and down. Positive roll is a right-handed rotation
about the forward axis. With zero pitch and no translational acceleration, a level
sensor measures `[f_y, f_z] = [0, -g]`. The model uses `g = 9.80665 m/s²` by default.
Rotation is about the IMU; lever-arm acceleration is absent. General 3D motion,
translation, calibration errors, and vibration are outside this observation model.

For an interval-mean gyro measurement `u` over an interval of duration `dt`:

```text
x = [theta, b]
x_minus = F x_plus + [dt, 0] * u
F = [[1, -dt], [0, 1]]
P_minus = F P_plus F.T + Q
Q = [[sigma_g² * dt², 0], [0, 0]]
```

`sigma_g` is the independent gyro-noise standard deviation in rad/s **per interval
mean**, not a continuous-time noise density. This is the existing simulation's
sampling convention, not an assumption about arbitrary logged gyro snapshots.
The default bias has no random walk. Initial standard deviations define a diagonal covariance;
zero initial angle uncertainty is permitted when that angle is known by construction.

The optional `bias_random_walk_std_rad_s_per_sqrt_s` (default zero) adds a
continuous bias diffusion covariance:

```text
Q_bias = sigma_b² * [[dt³/3, -dt²/2], [-dt²/2, dt]]
Q_total = Q + Q_bias
```

Here sigma_b is in (rad/s)/sqrt(s), unlike the per-interval sigma_g. Predicted
mean bias stays unchanged; observations can adjust it as uncertainty grows.
The [bias model and comparison protocol](bias-random-walk.md) derive the coupled
terms and distinguish a random-walk prior from the deterministic ramp used to
evaluate tracking. The scalar angle KF and earlier experiments retain sigma_b=0.

## Vector correction

At the observation endpoint, specific force `z = [f_y, f_z]` has units m/s²:

```text
h(x) = [-g * sin(theta), -g * cos(theta)]
H = [[-g * cos(theta), 0], [g * sin(theta), 0]]
R = sigma_a² * I_2

innovation = z - h(x_minus)
S = H P_minus H.T + R
C = P_minus H.T
solve S K.T = C.T
x_plus = x_minus + K * innovation
P_plus = (I - K H) P_minus (I - K H).T + K R K.T
```

`sigma_a` is the independent, equal component-noise standard deviation in m/s²
per observation. `R` stays fixed. Both `h` and `H` use the same prior state, and
both components enter one joint correction. There is no sequential relinearization
or iterated update. The code solves a linear system instead of constructing `S`'s
inverse and uses the Joseph covariance form, followed by removal of roundoff
asymmetry. Returned innovation and `S` describe the prior, before correction.

The Jacobian's bias column is zero. Prediction creates angle–bias cross-covariance,
which lets subsequent force observations correct bias. A zero initial bias
uncertainty prevents that correction when sigma_b=0. Positive bias diffusion
creates new uncertainty and cross-covariance even from an initially known bias.

The state angle stays unwrapped. Periodicity of `h` allows a local correction
across ±pi without explicitly wrapping the innovation, but it cannot recover an
unknown number of complete turns. The update neither normalizes the measurement
nor rejects it based on magnitude. It accepts any finite vector of shape `(2,)`,
including zero force. Numerical acceptance does not establish physical validity.

## Relationship to the angle Kalman filter

For a nonzero observation, let `r = norm(z)`,
`theta_acc = atan2(-f_y, -f_z)`, and `delta = theta_acc - theta_minus`.
Isotropic component noise gives an exact identity for this local EKF correction:

```text
R_angle = sigma_a² / g²
K_angle = P_minus[:, 0] / (P_minus[0, 0] + R_angle)
effective_angular_innovation = (r / g) * sin(delta)
x_plus = x_minus + K_angle * effective_angular_innovation
```

The angle Kalman filter instead corrects with the wrapped angular difference
`delta`. Near a correct prediction with `r ≈ g`, these corrections are close.
They can differ for larger errors or changed magnitudes. Direct vector processing
does not remove translation ambiguity or guarantee better accuracy.

The covariance recursion is **identical** to the angle Kalman filter's when that
filter uses fixed `R_angle = sigma_a²/g²`, the same initial covariance, prediction
noise, and update schedule. This follows from the constant norm `g` of the
Jacobian's angle column and isotropic `R`. It holds even when the state trajectories
differ or a disturbance violates the gravity model, up to floating-point roundoff.
Consequently, equal or narrow covariance bands do not demonstrate equal or small
actual errors.

For the current scalar KF implementation, matching the prediction noise requires
the vector EKF's bias diffusion to be zero. Do not compare those covariance
histories as equal after enabling diffusion only in the vector EKF.

## Innovations and local failure modes

The vector normalized innovation squared is `NIS = innovation.T * solve(S, innovation)`.
It has two observation dimensions; the scalar angle filter has one. Under a
consistent local Gaussian model, their reference mean values are 2 and 1,
respectively. Dividing NIS by its dimension aids plotting but does not make it an
accuracy score or erase their different sensitivity to force magnitude. Local
linearization, model mismatch, and the fixed true bias across repeated simulations
limit statistical consistency claims.

The innovation has a tangential part `r * sin(delta)` and a radial part
`r * cos(delta) - g`. Only the tangential part changes the state. The radial part
still contributes to vector NIS. A purely radial observation, including zero force
or a gravity vector exactly opposite the prediction, can therefore produce a large
NIS with no angle or bias correction while the covariance contracts. An exact
half-turn initialization error can stall. This EKF is a local estimator with no
global convergence guarantee or physical rejection gate.

## Implementation and verification

[`ekf.py`](../src/meridian/ekf.py) exposes `gravity_observation`,
`gravity_jacobian`, and `AngleBiasEKF`. The filter's `predict(rate_rad_s, dt_s)`
and `update(force_yz_m_s2)` methods have no file or plotting dependencies. The caller
owns timestamps: predict to an observation endpoint before correcting with its
force vector, and only predict between observations. State and covariance accessors
return copies; invalid input or a numerically failed operation leaves both unchanged.

[`test_ekf_core.py`](../tests/test_ekf_core.py) checks independently specified static
poses, centered finite differences of both state columns, a whitened least-squares
posterior, the scalar tangent-projection identity, covariance parity, and the radial
failure modes. It also checks prediction parity, bias observability, numerical
symmetry/positive semidefiniteness, and input/failure handling.

[`ekf_experiment.py`](../src/meridian/ekf_experiment.py) compares this EKF with the
existing gyro, complementary, and angle Kalman baselines on shared measurements.
The [comparison report](../results/ekf-comparison/README.md) records the reproducible
command, evaluation contract, results, and limitations. The new estimator uses the
existing nominal and prescribed-translation scenarios. The later
[controlled suite](controlled-scenarios.md) evaluates additional initialization,
bias, availability, noise, and timing assumptions with this core unchanged.
Broader robustness claims and physical acquisition remain separate work.

The [C++ port](cpp-ekf.md) exposes the same state and update model through a
separate C++17/Eigen API. Its [agreement report](../results/cpp-parity/README.md)
compares the initial state and every prediction/correction, including full P and
S matrices and prior innovations. Both implementations retain the local model
limitations described above.
