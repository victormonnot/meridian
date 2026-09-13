# Offline roll and gyro-bias replay

The replay compares gyro integration, complementary fusion and the existing
angle/bias Kalman filter on one explicitly selected DataFlash IMU segment.
It uses shared measurements and initialization, exports its assumptions, and
preserves the original recording. This is a Python offline experiment; the
accelerometer-vector EKF and C++ implementation remain pending.

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

All methods start at `z[0]`. The Kalman prior has zero estimated bias,
`P_angle = R`, `P_bias = sigma_initial_bias²`, and zero angle/bias cross covariance.
The first accelerometer tilt is used **once for initialization**, without another
correction at the same instant. Each subsequent row causes one prediction using
the preceding gyro and one correction using its current tilt. There are `N-1`
propagations and corrections for `N` rows.

The complementary gain is `1 - exp(-dt/tau)`. Kalman and complementary corrections
use the shortest wrapped innovation; their state angles stay unwrapped. A wrapped
measurement cannot recover a lost turn. The one-axis model assumes predominantly
roll motion and gravity-dominated specific force in a verified FRD body frame.
Body gyro x is not the general 3D Euler roll derivative.

## Declared tuning

| Setting | Default | Meaning |
| --- | --- | --- |
| `--gyro-noise-std-deg-s` | 1°/s | Effective standard deviation per held interval sample. |
| `--angle-noise-std-deg` | 2° | Effective tilt standard deviation; also initializes angle uncertainty. |
| `--initial-bias-std-deg-s` | 1°/s | Prior uncertainty of the initially zero bias estimate. |
| `--complementary-tau-s` | 1 s | Complementary correction time constant. |
| `--max-gap-s` | 0.2 s | Segmentation and replay interval limit. |

These values are illustrative settings fixed before inspecting replay outcomes.
They are not sensor noise measurements or a calibration. The Kalman core keeps
`Q = diag((sigma_g * dt)², 0)` and constant `R = sigma_angle²`; the bias has no
random walk. No physical disturbance rejection is implemented.

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
| `estimates.csv` | Timestamps, elapsed time, apparent tilt, all three estimates, KF bias and covariance. |
| `innovations.csv` | Pre-correction innovations, variances and squared normalized innovations at rows 1 to N−1. |
| `summary.json` | Source and artifact hashes, selected interval, tuning, initialization, decoder metadata, diagnostics and limitations. |
| `overview.png` | Principal angles, wrapped disagreement, estimated bias with model standard deviations, and normalized innovations. |

For a common plotting convention, all displayed angles are wrapped to [-180°, 180°)
with line breaks at the branch cut; estimator CSVs retain unwrapped states.
The covariance state order is `[roll_rad, bias_rad_s]`: diagonal units are rad²
and rad²/s², with rad²/s for the cross term.

The disagreement statistics use the shortest signed difference to accelerometer
tilt at each snapshot, including initialization, with equal weight per sample.
Their RMS is **not angle RMSE against ground truth**: both fusion methods already
use that tilt as input. Decreasing disagreement can simply mean following a
disturbed accelerometer more closely. The gyro angle change is relative to the
shared initial angle, using unwrapped endpoints, not necessarily physical drift. Estimated bias is a model
state, not a measured true bias or a calibration to apply to the vehicle.

Health counters and arming events in the summary describe the source, not a newly
verified bench protocol. Source hashing does not authenticate acquisition
conditions. Parser diagnostics and trailing bytes stay visible. Raw recordings
and historical replay outputs remain local; review any dataset before public
inclusion. Synthetic tests independently cover quadrature, known-bias recovery,
causality, wrapping, integer timing, gaps and complete binary-to-export execution.

A new documented bench acquisition with the drone disarmed and propellers removed
remains required. Use known static poses with reference uncertainty, then slow
manual roll with pauses. Keep Meridian outside the control loop. Without an
independent reference, report observed agreement and repeatability; neither
historical replay nor ArduPilot attitude establishes angle accuracy, embedded
performance or flight validation.
