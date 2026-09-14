# Historical IMU replay with the vector EKF

This Python offline replay compares four estimators on one previously audited
DataFlash segment. It demonstrates reproducible processing of recorded sensor
snapshots. There is no independent roll or gyro-bias reference: disagreement
with the accelerometer input is a diagnostic, not measured estimation accuracy.
A new documented bench acquisition remains required.

## Recording and selection

The source is an 8,127,996-byte historical recording, identified by SHA-256
`47b6476bc77cc1ccd9c1248a31eeaeec8866c77062c176127411192d84987191`.
Instance 0, segment 5 at a 0.2 s gap threshold was retained from the earlier
audit/replay, before inspecting EKF results. Selection was not optimized for
filter agreement.

| Selection | Value |
| --- | ---: |
| IMU snapshots | 4,957 |
| Predictions and corrections per filter | 4,956 |
| Original IMU record indices, inclusive | 2,416–7,372 |
| First / last TimeUS | 133,685,070 / 331,954,050 µs |
| Duration | 198.268980 s |
| Interval min / median / max | 38.377 / 39.995 / 69.170 ms |

Recorded units are rad/s and m/s². Each prediction holds the previous gyro x
snapshot over the actual integer timestamp difference. This is a quadrature
assumption for logged frontend snapshots, not the simulator's exact interval-mean
convention. Each correction uses the current accelerometer; no missing interval
is filled, and no delay compensation or additional calibration is applied.

The decoder reports no parser diagnostics and 37 trailing unparsed bytes.
Available health/arming metadata does not establish a complete, disarmed bench
protocol or the currently installed firmware. No new acquisition occurred.
The original recording and detailed CSV/PNG outputs remain local; this report
contains selected aggregate results, not a publicly bundled real dataset.

## Fixed comparison settings

All four estimates begin at the first apparent tilt, −0.716461429°. Both Kalman
filters start with zero bias, angle standard deviation 2°, bias standard
deviation 1°/s and zero cross covariance. The first accelerometer observation is
used only for initialization. Gyro noise is 1°/s per held sample, with
`Q = diag((sigma_g * dt)², 0)`; complementary `tau = 1 s`.

The angle KF uses `R = radians(2)²`. The vector EKF uses raw body y/z force,
`g = 9.80665 m/s²` and isotropic component standard deviation
`g * radians(2) = 0.342316662 m/s²`. This matches local angular information at
nominal gravity while preserving the earlier tuning. No parameter was fitted
to this recording's agreement curves. These settings are illustrative, not
identified sensor noise.

## Observed diagnostics

Differences below are shortest signed angle differences to apparent
accelerometer tilt; RMS weights each snapshot equally, including initialization.
The EKF uses the same accelerometer information as the scalar fusion methods.

| Method | RMS difference to tilt (°) | Final difference to tilt (°) |
| --- | ---: | ---: |
| Gyro integration | 2.094872 | +4.511564 |
| Complementary | 0.029020 | +0.040267 |
| Angle KF | 0.028221 | +0.034587 |
| Vector EKF | 0.028430 | +0.034846 |

The gyro estimate changes by +4.492442° from initialization. The EKF's final
bias estimate is +0.022828°/s, versus +0.022829°/s for the angle KF. Neither is
a measured true bias or a calibration to apply to the vehicle. KF/EKF roll
estimates differ by at most 0.000512° on this segment; this close agreement does
not establish physical accuracy or superiority of either method.

Final model standard deviations are 0.282860° for angle and 0.014349°/s for bias
in both Kalman filters. Their covariance histories agree to roundoff (maximum
absolute stored-entry difference 1.63 × 10⁻¹⁹ in their respective SI units), as
expected from matched isotropic tuning and the shared schedule. Covariance
agreement does not validate confidence against an independent reference.

| Innovation diagnostic | Angle KF | Vector EKF |
| --- | ---: | ---: |
| Measurement dimension | 1 | 2 |
| Mean NIS | 0.000203343 | 0.077627186 |

Vector innovation component RMS is 0.004125 m/s² in y and 0.095288 m/s² in z.
The selected 3D force norm ranges from 9.655693 to 9.775256 m/s², below the
configured gravity magnitude. A tangent/radial decomposition at each prior
gives mean NIS contributions of 0.000202314 and 0.077424872 respectively.
The radial force discrepancy dominates the vector NIS; the scalar tilt discards
force magnitude. These values do not isolate calibration, translation or other
physical causes, and the two NIS dimensions are not an accuracy ranking.
Small innovations relative to assumed noise also do not establish statistical
consistency, especially with correlated, filtered sensor snapshots.

## Reproduction and checks

With private access to the source matching the fingerprint, follow the
[audit and replay guide](../../docs/imu-replay.md) and run:

```sh
python -m meridian.imu_replay /path/to/recording.bin \
  --instance 0 --segment 5 --output outputs/replay-vector
meridian-imu-replay /path/to/recording.bin \
  --instance 0 --segment 5 --output outputs/replay-vector-repeat
python -m pytest -q tests/test_replay_core.py tests/test_replay_ekf.py tests/test_imu_replay.py
```

The recording is not distributed, so reproducing this particular table requires
separate access to it. Public tests use synthetic measurements and binary
DataFlash fixtures; they run independently of the historical file.

Two executions, through the module and installed CLI, produced all seven files
byte for byte identically in the pinned Python 3.12/Linux environment. Source
hashing before/after confirmed preservation. The original `measurements.csv`,
`estimates.csv` and `innovations.csv` remain byte-identical to the earlier
three-method replay. Separate EKF CSVs preserve the original layouts; the
summary is now schema 2.

Independent tangent/radial correction and rank-one covariance calculations
also checked all 4,956 recorded updates: maximum absolute state difference
5.03 × 10⁻¹⁷ in rad or rad/s and NIS difference 3.30 × 10⁻¹⁵.
All exported state covariances were positive semidefinite and innovation
covariances positive definite. These checks establish numerical agreement of
the replay calculations, not correctness of the physical observation model.

This Python experiment does not execute C++. A separate
[historical Python/C++ comparison](../imu-cpp-parity/README.md) now checks the
same segment and links its operations back to these snapshot results.
The web explorer still contains simulations only. Known
static poses, reference uncertainty and slow manual roll in a new documented
acquisition remain necessary, with the drone disarmed, propellers removed and
Meridian outside the control loop. No embedded, real-time or flight validation
is claimed.
