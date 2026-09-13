# Design and validation approach

Status: Python gyro integration and the linear angle/bias Kalman reference are
implemented, with tests, controlled simulation, and [reproducible comparisons](../results/linear-kalman/README.md).
The Kalman reference currently uses a direct synthetic angle observation.
Accelerometer fusion, the EKF, C++, bench replay, and the web interface are pending.

## Scope

Meridian's first study estimates roll angle and residual gyroscope bias from one
IMU. It combines reproducible simulation, Python reference estimators, a C++ EKF,
and offline replay of real bench measurements. Completion requires both simulation
evidence and a documented bench acquisition and replay.

The core will remain independent of any vehicle software. Full 3D attitude,
additional sensors, and integrations are possible extensions, not committed
features.

## Estimation model

The current baseline integrates gyro rates without estimating or subtracting bias.
Its simulator computes mean rates over intervals from an analytical roll trajectory.
All three conditions start at the known initial angle. The resulting ideal
reconstruction isolates sensor error; it does not validate numerical integration of
instantaneous samples or establish a convention for real logs. Internal units are
radians and seconds; exported measurement intervals and angle endpoints are explicit.

The Kalman state is `x = [theta, b]`, with roll `theta` in radians and gyro bias
`b` in radians per second. For motion about the longitudinal axis:

```text
omega_m = theta_dot + b + gyro_noise
theta_next = theta + dt * (omega_m - b) + process_noise_theta
b_next = b + process_noise_bias
F = [[1, -dt], [0, 1]]
```

The implemented reference assumes constant bias, independent interval-mean gyro
noise, and `Q = diag(sigma_g^2 * dt^2, 0)`. Bias uncertainty starts nonzero and is
updated through the angle–bias covariance; no bias random walk is modeled yet.
Slow variations are a later scenario. Per-sample variance and continuous-time
noise density are different quantities.

The linear Kalman filter uses a synthetic angle observation `z = theta + noise`.
The default experiment corrects at 10 Hz after predictions using 100 Hz gyro
intervals. The [implemented model](linear-kalman.md) details initialization, Q/R,
time ordering, and predeclared evaluation criteria. The next observation model
will use tilt derived from two accelerometer components, with an approximate
angular-noise model and explicit angle wrapping.

The planned EKF retains this propagation but directly observes accelerometer specific
force. Under a forward-right-down body frame, positive right-hand roll, zero
pitch, and negligible translational acceleration:

```text
h(theta, b) = [-g * sin(theta), -g * cos(theta)]  # predicted [f_y, f_z]
H(theta, b) = [[-g * cos(theta), 0], [g * sin(theta), 0]]
theta_acc = atan2(-f_y, -f_z)
```

The nonlinear observation justifies the EKF. Whether it improves on simpler
filters must be measured. Hardware signs and frames require physical pose checks
before using this model with logs.

## Assumptions and acquisition

Initial experiments use static poses and slow, predominantly single-axis motion.
The accelerometer must be sufficiently dominated by gravity; translation,
off-axis motion, and rotation around a point away from the IMU can violate this
assumption. A measured magnitude near `g` does not establish its validity.
Accelerometer calibration errors are outside the initial two-state model.

Acquire data with the drone disarmed and propellers removed. The estimator stays
outside the control loop. Onboard logging followed by offline extraction is the
preferred route, subject to checking the installed firmware and available streams.
Preserve original recordings and acquisition metadata locally; review datasets
before public inclusion.

Identify sensor instances, units, calibration and filtering, sample versus message
timestamps, clock behavior, gaps, duplicates, and relative sensor delay. Disarmed
recording and usable timing must be demonstrated before fixing the replay format.
Log timestamps alone do not prove simultaneous sampling.

## Validation

Compare gyro integration, accelerometer-only tilt, a complementary filter, the
linear KF, and the EKF using shared measurements and documented initialization
and tuning. Keep simulation truth separate from estimator inputs. Record seeds,
configuration, environment versions, and reproducible commands.

Grow scenarios from stationary and smooth motion to changing bias, poor initial
estimates, timing irregularities, missing observations, and acceleration
disturbances. Report angle RMSE, drift, and bias convergence over multiple seeds;
inspect covariance behavior and innovations. Set numerical criteria before
evaluating results. Check the EKF Jacobian and Python/C++ agreement independently.

For real data, use known static fixture angles with stated uncertainty if feasible.
Without an independent dynamic reference, report repeatability and agreement;
ArduPilot attitude is another estimate, not ground truth. Bench replay does not
establish embedded, real-time, or flight performance.

## Software boundaries and presentation

| Component | Responsibility |
| --- | --- |
| Models and estimators | State, prediction, correction, and covariance; no file or UI dependencies. |
| Simulation and data adapters | Generate measurements or extract logs; retain provenance and timing. |
| Experiments and evaluation | Replay, configuration, baselines, metrics, and result export. |
| Presentation | Plots and a lightweight web interface for comparison and time-based replay. |

The first implementation uses small Python modules: NumPy in the simulation and
integration core, Matplotlib in the experiment layer, and pytest for verification.
The CLI exports measurements and truth separately; only the experiment layer uses
truth for initialization and evaluation. No vehicle software or web framework is
required. Add C++ after the reference model is verified. Broader sample conventions
must account for unequal sensor rates and observed log semantics; the initial CSVs
are an experiment format, not a general sensor interchange specification.

The planned web interface will initially read exported results after useful
Python comparisons exist. Later interactive settings will invoke the same
simulation and estimator core. A roll indicator can complement the curves.
The UI stack and deployment are undecided.

## Next step and open decisions

Add simulated accelerometer specific force and tilt extraction, verify signs and
angle wrapping, then compare a complementary baseline and the linear reference on
shared measurements. In parallel, read back the installed flight-controller
configuration and prepare a short stationary bench recording. Open decisions
include firmware, streams, synchronization, the angle reference, real sensor noise,
and criteria for the extended scenarios. The current simulation parameters are
illustrative and have not been fitted to a physical IMU.
