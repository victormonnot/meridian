# Controlled scenario evaluation

This suite evaluates the existing gyro, complementary, angle KF, and vector EKF
without changing their algorithms or tuning. Each case isolates an assumption
about initialization, bias, measurement noise, availability, or timing. It is a
bounded simulation study, not a claim of general robustness or sensor calibration.
The earlier [translation experiment](../results/ekf-comparison/README.md) remains
available and is not duplicated here.

## Contract declared before evaluation

All cases use 30 s of sinusoidal pure roll, amplitude 20°, frequency 0.1 Hz,
3,000 gyro intervals, and 300 scheduled accelerometer arrivals. The nominal
gyro spacing is 0.01 s; accel is scheduled every tenth endpoint, first at 0.1 s
in uniform-timestamp cases. Jitter changes both gyro and arrival times.
Gyro rates are exact interval means plus mean bias and independent noise with
std 1°/s per interval. The force model and FRD convention are unchanged.

| Case | Change from nominal | Question |
| --- | --- | --- |
| `nominal` | None: angle 0°, angle std 0°, true bias +0.5°/s, accel std 0.2 m/s². | Does the new runner reproduce the existing reference? |
| `initial_offset` | All estimates start at +60°; both Kalman angle stds are 30°. | How does the local correction recover from a wrong, uncertain angle? |
| `initial_overconfident` | Same +60° estimate but Kalman angle stds are 0°. | What happens when a wrong initial angle is declared exact? |
| `bias_ramp` | True bias rises linearly from +0.5 to +1.5°/s during 10–20 s. | How do constant-bias filters respond to a changing bias? |
| `accel_dropout` | Omit corrections for arrivals with `12 <= t < 17 s`. | What happens during prediction alone and when observations return? |
| `timing_jitter` | Positive independent interval weights uniform in [0.5, 1.5], normalized to total 30 s. | Does propagation respect actual intervals and observation spacing? |
| `accel_noise_mismatch` | Actual accel std 0.6 m/s²; assumed std remains 0.2. | How do errors and model diagnostics respond to underestimated noise? |
| `accel_delay` | Force sampled 0.1 s before its reported arrival; no compensation. | What does an unmodeled sensor delay do to the estimates? |

The two initialization cases have identical measurements and differ only in
declared angle uncertainty. Relative to nominal, the uncertain case changes both
initial estimate and uncertainty. All methods share the initial estimate, gyro,
and usable force observations within each case. Gyro/complementary have no P.

Both Kalman filters retain initial bias 0°/s, bias std 1°/s, zero initial
cross-covariance, constant-bias dynamics, and `Q = diag(sigma_g² * dt², 0)`.
EKF `R = 0.2² I` in (m/s²)², angle-KF `R_angle = (0.2/g)²` in rad²;
complementary tau is 1 s. There is no bias random walk, rejection gate, adaptive
noise, delay compensation, or tuning after observing a case's results.

Nominal per-run regression criteria are **roll RMSE < 0.75°** and **final absolute
bias error < 0.15°/s**, separately for both Kalman filters. Apply them to displayed
seed 42 and each repeat seed 0–19. Only the nominal case receives accuracy
pass/fail labels. Other cases receive numerical-contract checks and descriptive
metrics, with no required ranking, convergence time, or stress accuracy threshold.

Numerical contracts require finite outputs, symmetric positive-semidefinite
covariances, and KF/EKF covariance agreement. An absolute tolerance of `1e-12`
on stored SI matrix entries/eigenvalues accommodates roundoff. These numerical
checks are separate from the nominal accuracy criteria and are not physical
uncertainty tolerances.

## Truth, noise pairing, and time

For the bias ramp, instantaneous truth is `b(t) = 0.5 + 0.1 * clip(t-10, 0, 10)`
in deg/s. Its primitive is
`B(t) = 0.5*t + 0.05*((t-10)_+² - (t-20)_+²)` in degrees.
Each gyro interval receives `(B(t_end)-B(t_start))/dt`, including intervals
crossing a ramp boundary. Endpoint bias truth is retained separately; the final
bias reference is 1.5°/s. Total integrated bias over 30 s is 30°.

The root SeedSequence spawns gyro, accelerometer, and jitter streams in that order.
The first two reproduce the existing experiment. All scheduled accelerometer
noise is generated before the dropout mask is applied, so removing observations
does not shift later noise samples. Noise-mismatch observations reuse the same
standard-normal draws at three times their amplitude. Jitter changes the signal's
sampling times; the random draws remain paired by index, not by physical time.

Jittered interval durations are the raw weights divided by their sample mean
and multiplied by 0.01 s. Their mean is fixed to 0.01 s; bounds are not guaranteed
to be exactly 5–15 ms after normalization. Rates use endpoint angle differences
divided by actual dt. Keeping gyro noise std fixed per interval means its integrated
variance is `sigma_g² * sum(dt²)`, which changes with jitter. This is not a
continuous white-noise density model or an identified hardware sample convention.

Prediction always precedes an available correction at its reported endpoint.
During dropout, gyros continue and force observations are omitted without filling
or later replay. The complementary gain uses time since the previous accepted
correction: the return at 17 s follows the last update at 11.9 s, a 5.1 s gap.
Its stronger return correction is part of the unchanged baseline behavior.

In the delay case, the first arrival at 0.1 s contains a sample from 0 s, so no
negative-time padding is needed. The filter receives only the reported arrival
and force; the true sample time is exported separately as simulation provenance.
Correcting at arrival deliberately violates the synchronous observation model.
This is not an implementation of delayed-measurement fusion.

## Metrics and interpretation

Retain ordinary endpoint RMSE for direct comparison with earlier results.
Also report `sqrt(trapezoid(error², time) / duration)` as an approximate
duration-weighted RMSE, so irregular timing does not silently change the meaning
of an average. The trapezoid rule connects post-update endpoint errors; it is not
an exact integral of the continuous estimator error across corrections.
Errors use unwrapped roll minus truth, including the initial endpoint.

Report signed final and maximum absolute roll error, plus RMSE over the fixed last five seconds
(`t >= 25 s`). For both Kalman filters, compare bias with instantaneous endpoint
truth and report signed final and maximum absolute bias error. Preserve P, innovations, and NIS
at their actual epochs. These diagnostics do not establish calibrated confidence
or statistical consistency. Raw vector/scalar NIS have two/one dimensions.

Nominal, ramp, noise-mismatch, delay, and the overconfident-initialization case
share the same P0, Q, R, and update schedule, so they must share each filter's
covariance history despite different errors. With isotropic R, the two Kalman
covariance histories also agree within each case. A narrow covariance under
model mismatch must not be presented as an accuracy guarantee.

Known limitations of the earlier study remain: pure roll, no lever arm or complete
vehicle dynamics, no physical sensor identification, no general convergence proof.
New documented bench acquisition and replay remain required when hardware is
available; this suite does not establish embedded, real-time, or flight performance.
