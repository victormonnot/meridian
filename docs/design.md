# Design and validation approach

Status: Python gyro integration, accelerometer tilt, complementary fusion, and the
angle/bias Kalman reference are implemented, with tests and
[paired nominal/disturbed simulations](../results/accelerometer-fusion/README.md).
The [vector EKF comparison](../results/ekf-comparison/README.md) now uses those
same simulated inputs with a nonlinear force observation and verified Jacobian.
The earlier direct synthetic angle experiment remains available.
An optional [DataFlash audit](dataflash-audit.md) now extracts IMU snapshots,
checks recorded units, and reports timing discontinuities and selected metadata.
[Offline replay](imu-replay.md) compares the estimators on one explicit log segment
with shared initialization and a previous-snapshot gyro hold. The
vector EKF has only been evaluated in simulation. Its real-log replay, C++, new
documented bench acquisition, and web interface are pending; historical replay is
not a completed bench validation.

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

The linear Kalman reference first used a synthetic angle observation
`z = theta + noise`. It now also accepts tilt derived from two accelerometer
components using principal angular innovations and an approximate noise model.
The experiments correct at 10 Hz after predictions using 100 Hz gyro intervals.
The [linear model](linear-kalman.md) and [accelerometer fusion model](accelerometer-fusion.md)
detail initialization, Q/R, time ordering, complementary gain, and predeclared criteria.

The Python EKF retains this propagation but directly observes accelerometer specific
force. Under a forward-right-down body frame, positive right-hand roll, zero
pitch, and negligible translational acceleration:

```text
h(theta, b) = [-g * sin(theta), -g * cos(theta)]  # predicted [f_y, f_z]
H(theta, b) = [[-g * cos(theta), 0], [g * sin(theta), 0]]
theta_acc = atan2(-f_y, -f_z)
```

The direct vector correction linearizes once at the prior, with
`R = sigma_accel^2 * I`, a joint gain solve, and Joseph covariance update.
The [vector model guide](vector-ekf.md) derives the observation and explains its
local relationship to the angle KF. Under isotropic noise and the same initial
covariance and schedule, the two covariance histories are mathematically equal,
even when translation produces different state errors. Their nominal accuracy
is similar in the selected simulation; neither resolves translation ambiguity.
The EKF does not normalize observations or reject physical model violations.
Hardware signs and frames require physical pose checks before using this model with logs.

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

The implemented audit retains integer `TimeUS` and original file order, analyzes
each IMU instance separately, and marks finite timing segments without filling
gaps. Its configurable 0.2 s gap threshold is an inspection policy. Logged frontend
snapshots use an explicit previous-sample hold for replay; the simulator's exact
interval-average convention is not transferred to hardware data. Audit statistics
do not identify physical noise or validate arming/pose conditions.

Replay starts all methods from the first selected tilt, with KF angle variance
equal to R and zero initial bias with nonzero default uncertainty. The first
measurement is not corrected twice. Later rows use actual integer timestamp
differences, then correct at the endpoint. Effective Q/R settings remain
illustrative; filtered snapshot correlations and unknown delays are not modeled.
The report distinguishes disagreement with fused tilt from independent accuracy.

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

The Python EKF's Jacobian is checked by centered finite differences; its correction
is checked against independent least-squares and tangent-projection calculations.
Tests retain the opposite-vector failure mode: a radial innovation can leave the
angle unchanged while covariance contracts. The comparison applies predeclared
nominal limits (angle RMSE < 0.75°, final absolute bias error < 0.15°/s) separately
to seed 42 and seeds 0–19. It imposes no superiority requirement or nominal accuracy
threshold on the disturbance. Joint EKF NIS has two measurement dimensions, versus
one for the angle KF; these diagnostics do not establish statistical consistency.

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

Read back the installed flight-controller configuration and acquire a short
stationary and manual-roll bench recording with documented poses and conditions.
Use the implemented audit and replay to inspect those measurements, with explicit
angle-reference uncertainty and noise assumptions. Historical logs do not replace
that acquisition. The next numerical work extends controlled scenarios for the
Python references, before implementing C++ and checking agreement on identical inputs.
Current exported comparisons can also inform a first web replay interface;
its implementation and stack still require design decisions. Real angle reference,
sensor noise, and extended-scenario criteria remain open. Current parameters have
not been fitted to a physical IMU.
