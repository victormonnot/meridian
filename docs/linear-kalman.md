# Linear angle and gyroscope-bias Kalman reference

This experiment adds a direct noisy angle observation to the gyro integration
baseline. The observation is synthetic; accelerometer signals and their physical
limitations will be introduced separately.

## State, prediction, and correction

The state is `x = [theta, b]`: unwrapped roll in rad and constant gyro bias in
rad/s. A rate `u[k]` is the mean over `[t[k], t[k+1]]`. With
`dt = t[k+1] - t[k]`, prediction is:

```text
x_minus = F x_plus + B u[k]
F = [[1, -dt], [0, 1]]
B = [dt, 0]
P_minus = F P_plus F.T + Q
```

Gyro noise is independent across intervals, with per-sample standard deviation
`sigma_g` in rad/s. Integrating its error multiplies it by `dt`, so:

```text
Q = [[sigma_g^2 * dt^2, 0], [0, 0]]
```

The bias is exactly constant in this model: `Q[1,1] = 0` does not mean that its
value is known. Initial bias uncertainty is carried by `P[1,1]` and propagates
through `F`. No continuous-time gyro noise density or bias random walk is used.

At an observation endpoint, the measurement is `z = theta + v`, with independent
Gaussian noise of standard deviation `sigma_z` in rad:

```text
H = [1, 0]
R = sigma_z^2
innovation = z - H x_minus
S = H P_minus H.T + R
K = P_minus H.T / S
x_plus = x_minus + K * innovation
P_plus = (I - K H) P_minus (I - K H).T + K R K.T
```

The final equation uses the Joseph covariance form; the implementation also removes
roundoff asymmetry. The observation is scalar, so division by `S` replaces a matrix
inverse. These are prediction and correction operations of the linear Kalman
filter; see [Sanjay Lall's Stanford EE363 notes](https://ee363.stanford.edu/lectures/021_kf.pdf)
for the general recursion and its statistical assumptions.

Prediction creates an angle–bias cross-covariance. It allows an angle innovation
to correct bias, even though `H` does not directly measure it. For a positive bias,
an angle estimate that advances too fast tends to produce negative innovations;
the negative bias gain then raises the bias estimate. Repeated observations over
elapsed time supply the information needed to separate angle and constant bias.

Sinusoidal motion does not make this filter nonlinear: its transition and
observation are linear in the state; the gyro input carries the changing motion.
No angle wrapping is applied in this direct-angle experiment.

## Timing and initialization

The default run uses the existing 30 s, 100 Hz roll simulation (20° amplitude,
0.1 Hz). It generates one angle observation every 10 gyro intervals: 10 Hz,
at 0.1, 0.2, ..., 30 s. A prediction always consumes the interval ending at an
observation timestamp **before** that observation is applied. Between observations,
the filter only predicts. No observation is interpolated, repeated, or used early.

Gyro integration and the Kalman filter share the same measured gyro stream and
known initial angle, `theta_hat(0) = 0`. The filter starts with `b_hat(0) = 0`
and `P0 = diag(0, (1°/s)^2)`, converted to SI units. The exact initial angle is an
experimental condition; initialization from an unknown pose is not evaluated.

True bias is +0.5°/s. Gyro noise standard deviation is 1°/s per interval mean;
angle noise standard deviation is 2° per observation. The filter's noise parameters
match the simulator by design, without fitting to the evaluated results. They are
illustrative and have not been identified from a physical sensor.

Each root seed spawns two NumPy SeedSequence children. Each child supplies a uint64
integer seed to PCG64: child 0 for gyro, child 1 for angle. These separate streams
avoid reusing gyro noise as angle noise. Their seeds are exported. This stream
scheme differs from the earlier gyro-drift experiment; comparisons here use the
same newly generated gyro data for both methods.

## Evaluation contract

Before running the new experiment, the following regression criteria were selected
for the default configuration: **Kalman angle RMSE < 0.75° over all endpoints** and
**absolute final bias error < 0.15°/s**. They are coarse checks for this controlled
scenario, not hardware accuracy requirements or probabilistic guarantees. Apply
them to the displayed seed 42 and separately to every seed in the fixed evaluation
set 0–19, without changing noise or initialization parameters after observing it.
Custom configurations report metrics but do not inherit these acceptance claims.
Changing only `--seed` or `--validation-seeds` applies the same thresholds to the
requested seeds; only the default command reproduces the full declared protocol.

Report all-endpoint angle RMSE for gyro integration and Kalman, including the
known initialization. Also report RMSE for both estimates and raw angle measurements
on the same observation timestamps, plus final angle error, final bias error, and
the mean/worst results across the fixed seed set. Raw angle observations are not
an estimate at every gyro endpoint.

Export state covariance at each endpoint and the innovation and its predicted
variance at actual corrections. Bands drawn from `P` and normalized innovations
are model diagnostics. True bias is fixed across repeated runs rather than drawn
from the initial prior; empirical coverage is not interpreted as calibrated
posterior probability. Bias changes, correlated noise, delayed observations, real
sampling conventions, and acceleration disturbances are outside this experiment.
