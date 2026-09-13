# Gyroscope–accelerometer roll fusion

This experiment replaces a direct synthetic angle observation with tilt computed
from noisy accelerometer specific force. It compares raw accelerometer tilt,
gyro integration, a complementary filter, and the angle/bias Kalman reference.
The raw components are not yet used as an EKF observation.

## Specific force and tilt

Body axes point forward, right, and down. For pure roll with zero pitch:

```text
f_y = -g * sin(theta) + a_y + noise_y
f_z = -g * cos(theta) + a_z + noise_z
theta_acc = atan2(-f_y, -f_z)
```

Here `a_y, a_z` are translational acceleration of the IMU, resolved in body axes,
in m/s². They are prescribed sensor-input disturbances, not computed from a vehicle
trajectory. Rotation is about the IMU, so no rotational lever-arm acceleration is
modeled. Gravity is `g = 9.80665 m/s²`. Force is sampled at observation endpoints;
gyro measurements retain the earlier interval-mean convention.

An accelerometer measures specific force and cannot independently distinguish
translation from the gravity direction. [NXP AN3461](https://www.nxp.com/docs/en/application-note/AN3461.pdf)
describes this ambiguity and tilt sensing. Its later equations negate sensor
outputs; the signs above explicitly use unnegated specific force in Meridian's
forward-right-down convention.

Tilt is wrapped to `[-pi, pi)`. Invalid or near-zero y/z vectors (magnitude at
most 1e-9 m/s²) are rejected as numerically undefined. This threshold is not a
physical validity test. No gravity-magnitude gate or adaptive measurement rejection
is used in this experiment.

## Angle uncertainty and branch handling

Each component has independent Gaussian noise with standard deviation
`sigma_a = 0.2 m/s²` per observation. For `r² = f_y² + f_z²`, the local gradient
of tilt with respect to `[f_y, f_z]` is `[f_z/r², -f_y/r²]`. Under gravity-dominated
conditions and isotropic noise, first-order propagation gives:

```text
R_angle ≈ sigma_a² / g²
sigma_angle ≈ sigma_a / g      # radians
```

The Kalman reference uses this **fixed nominal R**, not a measured-norm-dependent
value. Noise after atan2 is only approximately Gaussian at small component noise.
The formula does not account for translational acceleration, calibration errors,
correlations, or delays.

Kalman prediction and Q remain the constant-bias model documented in
[the linear reference](linear-kalman.md). Its new angular update uses
`wrap(theta_acc - theta_predicted)` as the innovation, retaining the existing
Joseph covariance correction. The old unwrapped update remains available for
direct synthetic angles. Stored estimates stay unwrapped. A locally appropriate
branch must be identifiable; this method cannot recover an unknown number of
turns or resolve errors beyond half a turn reliably.

## Complementary baseline

Gyro prediction at each interval is `theta_predicted = theta + omega_measured * dt`.
At an accelerometer observation, with elapsed time `T_obs` since the previous
observation (or initialization for the first):

```text
alpha = exp(-T_obs / tau)
theta = theta_predicted + (1 - alpha) * wrap(theta_acc - theta_predicted)
```

The default time constant is `tau = 1 s`, chosen before evaluation. The same gyro
and tilt measurements feed both filters. The complementary filter has no bias
state or covariance. At constant bias b, regular observations, and perfect tilt,
its post-correction steady-state angle error is
`alpha * b * T_obs / (1 - alpha)`; correcting angle does not identify the bias.

## Paired scenarios and evaluation contract

Both scenarios retain 30 s of sinusoidal roll, ±20° at 0.1 Hz, 100 Hz gyro intervals,
constant gyro bias +0.5°/s, and independent gyro noise std 1°/s per sample. Accel
observations arrive every 10 gyro intervals (10 Hz), beginning at t = 0.1 s, after
prediction to that endpoint. All estimates start at the known true angle 0°.
The Kalman bias starts at 0°/s with std 1°/s; initial angle uncertainty is zero.

The nominal scenario has zero translation. The disturbed scenario adds
`a_y = +2 m/s², a_z = 0` for `12 <= t < 17 s`. Each pair shares its gyro and
accel noise realizations. Root seed 42 is displayed; seeds 0–19 form a fixed repeat
set. SeedSequence child 0 supplies the gyro PCG64 seed, child 1 the accelerometer
seed, as uint64 integers. Noise parameters and tau are unchanged between scenarios.

Before executing the new experiment, nominal per-run criteria were set to
**Kalman angle RMSE < 0.75°**, **absolute final bias error < 0.15°/s**, and
**complementary angle RMSE < 1°**. These are coarse regression checks for the default
configuration, applied separately to seed 42 and each seed 0–19. Custom simulation
settings do not inherit them. Changing only requested seeds applies the same limits
to that seed set. No nominal accuracy threshold or required ranking applies to the
disturbed case: it tests a known violation of the observation model.

For gyro, complementary, and Kalman, report unwrapped angle RMSE over all endpoints.
Also compare all methods at the same observation endpoints; raw tilt errors use
the principal angular difference. Report RMSE separately before, during, and after
the prescribed pulse window, with before `t < 12`, during `12 <= t < 17`, and
after `t >= 17`, plus final bias error and repeat-set summaries. No settling-time
claim is inferred from only an end-of-run error.

Export covariance and innovations as diagnostics. Gaussian model bands are not
calibrated accuracy guarantees, especially during the disturbance. The y/z force
magnitude is only a diagnostic: at level, +2 m/s² in y creates approximately
−11.53° apparent roll while changing the magnitude by only +2.06%. A norm near g
therefore does not establish a valid gravity observation. Bias contamination and
subsequent recovery are part of the reported limitations.

The later [vector EKF comparison](../results/ekf-comparison/README.md) reuses these
exact simulation streams and baselines. Its [model guide](vector-ekf.md) describes
the nonlinear correction from force components, including local equivalence to
this angle KF and the same translation ambiguity.
