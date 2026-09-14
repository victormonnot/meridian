# Offline roll and gyro-bias replay

The replay compares gyro integration, complementary fusion, the angle/bias
Kalman filter and the accelerometer-vector EKF on one explicitly selected DataFlash IMU segment.
It uses shared measurements and initialization, exports its assumptions, and
preserves the original recording. This is a Python offline experiment; the
separate C++ core is not executed by this runner. A
[historical replay report](../results/imu-replay/README.md) records the selected
real-data results and their limits.
The separate [Python/C++ comparison driver](imu-cpp-parity.md) retains this
replay and checks the native EKF after every prediction and correction.

## Run

Install the optional decoder in the README's Python 3.12 environment, audit the
source, and inspect its per-instance segment IDs before choosing an interval:

```sh
python -m pip install -r requirements-dataflash.lock -e '.[dataflash]'
python -m meridian.dataflash_audit /path/to/recording.bin --output outputs/audit-run
python -m meridian.imu_replay /path/to/recording.bin \
  --instance 0 --segment 0 --output outputs/replay-run
python -m pytest -q
```

The example IDs are placeholders for a selection from your audit. Selection is
explicit; the tool does not find the segment that gives the best filter outcome.
The default 0.2 s maximum gap must match the audit threshold. If changing it,
rerun the audit with `--gap-threshold-s` and the replay with `--max-gap-s` at the
same value; segment IDs can change. A segment boundary always creates a separate
experiment with a fresh initialization. No estimate is carried across a gap.

Original integer `TimeUS` values, sensor instance and IMU record indices identify
the selected measurements. The reader checks recorded units and retains file
order. Nonfinite values, non-increasing times, over-threshold intervals and
numerically undefined accelerometer tilt are refused by the numerical core.
Finite segments are not a health, arming or gravity-validity certificate.
Shorter missing intervals can remain inside a segment.

## Sampling and initialization

For successive logged timestamps, integer subtraction precedes conversion:

```text
dt[k] = (TimeUS[k] - TimeUS[k-1]) / 1e6
rate_for_interval[k] = gyro_x[k-1]
theta_predicted[k] = theta[k-1] + dt[k] * (rate_for_interval[k] - bias[k-1])
z[k] = atan2(-accel_y[k], -accel_z[k])
```

Holding the previous snapshot is causal rectangular quadrature, an approximation
of motion between logged samples. It does not make those snapshots exact interval
means like the simulator. The last gyro snapshot is exported but has no following
interval and is unused in propagation. Logged gyro and accel times may include
different sensor/filter delays. No interpolation, delay compensation, further
calibration or board rotation is applied.

All four methods start at `z[0]`. Both Kalman priors have zero estimated bias,
`P_angle = sigma_angle²`, `P_bias = sigma_initial_bias²`, and zero angle/bias cross covariance.
The first accelerometer tilt is used **once for initialization**, without another
correction at the same instant. Each subsequent row causes one prediction using
the preceding gyro and one correction. The scalar filters use the current tilt;
the EKF uses the current, unnormalized `[accel_y, accel_z]` in m/s² with
`h(theta) = [-g sin(theta), -g cos(theta)]`. There are `N-1` propagations and
corrections per filter for `N` rows. Undefined tilt still rejects the whole
comparative replay, even though the standalone EKF accepts finite zero-force input.

The complementary gain is `1 - exp(-dt/tau)`. Angle KF and complementary corrections
use the shortest wrapped innovation; their state angles stay unwrapped. A wrapped
measurement cannot recover a lost turn. The one-axis model assumes predominantly
roll motion and gravity-dominated specific force in a verified FRD body frame.
Body gyro x is not the general 3D Euler roll derivative.

## Declared tuning

| Setting | Default | Meaning |
| --- | --- | --- |
| `--gyro-noise-std-deg-s` | 1°/s | Effective standard deviation per held interval sample. |
| `--angle-noise-std-deg` | 2° | Effective tilt standard deviation; also initializes angle uncertainty. |
| Derived EKF component noise | 0.342316662 m/s² | `g * radians(angle_noise_std_deg)`; isotropic y/z noise. |
| `--initial-bias-std-deg-s` | 1°/s | Prior uncertainty of the initially zero bias estimate. |
| `--complementary-tau-s` | 1 s | Complementary correction time constant. |
| `--max-gap-s` | 0.2 s | Segmentation and replay interval limit. |

These values are illustrative settings fixed before inspecting replay outcomes.
They are not sensor noise measurements or a calibration. Both Kalman filters keep
`Q = diag((sigma_g * dt)², 0)`; the bias has no random walk. The angle KF uses
constant `R_angle = sigma_angle²`. With fixed `g = 9.80665 m/s²`, the EKF uses
`R_force = (g * sigma_angle)² I`, default diagonal 0.117180697 (m/s²)².
The explicit SI values and dimensions are exported in `observation_models`.
There is no independent force-noise tuning control in this comparison.

This mapping makes the models locally comparable at nominal gravity:
`|dh/dtheta|² / sigma_accel² = 1 / sigma_angle²`. With shared initial P, Q and
timestamps, their covariance histories agree up to roundoff even when the
states differ. The mapping neither identifies physical sensor noise nor scales
it with the measured force magnitude. Forces are not normalized, and no physical
disturbance rejection or additional calibration is implemented.

Filtering, subsampling, temporal correlation, initialization correlation and
unmodeled acceleration can violate the assumed independent noise model. A
previous-sample hold avoids reusing endpoints in adjacent trapezoidal averages;
it does not restore independence of the actual sensor noise. Covariance shrinkage
and small normalized innovations do not establish physical accuracy or calibrated
confidence. Tuning should be revisited with a documented acquisition and suitable
noise analysis; this replay does not fit parameters to its own agreement curves.

## Outputs and interpretation

Every run requires a new directory. Files under `outputs/` are ignored by Git.

| File | Contents |
| --- | --- |
| `measurements.csv` | Selected original time/instance/record indices and six sensor components in SI units. |
| `estimates.csv` | Original three-method layout: timestamps, elapsed time, apparent tilt, gyro/complementary/angle KF states, KF bias and covariance. |
| `innovations.csv` | Original scalar KF layout: pre-correction innovations, variances and squared normalized innovations at rows 1 to N−1. |
| `ekf_estimates.csv` | N post-correction states (initial state at row 0), integer timestamps, elapsed time and full symmetric EKF covariance entries. |
| `ekf_innovations.csv` | N−1 prior y/z innovations, full symmetric S entries and joint NIS at integer correction timestamps. |
| `summary.json` | Schema 2: source/artifact hashes, selected interval, tuning, observation models/dimensions, initialization, phases, decoder metadata, diagnostics and limitations. |
| `overview.png` | Four principal-angle/disagreement traces, both bias estimates with model standard deviations, and separate scalar/vector NIS panels. |

The three existing CSV layouts are preserved. Schema 2 adds the separate EKF
files and metadata; summaries should be read according to their schema version.
The original scalar `mean_squared_normalized_innovation` field remains scalar;
`ekf_mean_normalized_innovation_squared` reports the joint vector value.

For a common plotting convention, all displayed angles are wrapped to [-180°, 180°)
with line breaks at the branch cut; estimator CSVs retain unwrapped states.
The covariance state order is `[roll_rad, bias_rad_s]`: diagonal units are rad²
and rad²/s², with rad²/s for the cross term.
Vector innovations are in m/s²; S entries are in (m/s²)². Joint NIS is
`vᵀ solve(S, v)`, dimensionless, including the off-diagonal S term. The scalar
measurement dimension is 1 and the vector dimension is 2; their raw NIS values
are not an accuracy ranking. A radial force discrepancy can contribute vector
NIS without changing the apparent tilt or correcting the angle. The figures use
separate NIS panels and scales, without an acceptance threshold.

The disagreement statistics use the shortest signed difference to accelerometer
tilt at each snapshot, including initialization, with equal weight per sample.
Their RMS is **not angle RMSE against ground truth**: all three fusion methods already
use the same accelerometer information, directly or through tilt. Decreasing disagreement can simply mean following a
disturbed accelerometer more closely. The gyro angle change is relative to the
shared initial angle, using unwrapped endpoints, not necessarily physical drift. Estimated bias is a model
state, not a measured true bias or a calibration to apply to the vehicle.

Health counters and arming events in the summary describe the source, not a newly
verified bench protocol. Source hashing does not authenticate acquisition
conditions. Parser diagnostics and trailing bytes stay visible. Raw recordings
and historical replay outputs remain local; review any dataset before public
inclusion. Synthetic tests independently cover quadrature, known-bias recovery,
causality, wrapping, integer timing, gaps and complete binary-to-export execution.
Vector replay tests add independent tangent/radial and information-form solutions,
retained radial mismatch, covariance equivalence, initial observation use and
integer-origin invariance. Tests require no historical recording. Compare all
seven output files from two runs in separate new directories to check reproducibility.

A new documented bench acquisition with the drone disarmed and propellers removed
remains required. Use known static poses with reference uncertainty, then slow
manual roll with pauses. Keep Meridian outside the control loop. Without an
independent reference, report observed agreement and repeatability; neither
historical replay nor ArduPilot attitude establishes angle accuracy, embedded
performance or flight validation.
