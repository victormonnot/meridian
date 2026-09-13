# Accelerometer-vector EKF comparison

The two-state Python EKF estimates roll and constant gyroscope bias directly from
accelerometer y/z specific force. It uses the same simulated measurements,
initialization, and prediction noise as gyro integration, complementary fusion,
and the existing angle Kalman filter. The nonlinear observation changes the
correction; it does not add states or resolve translational acceleration.

![Nominal and disturbed vector EKF comparison](overview.png)

## Reproduce

Use the Python 3.12 environment from the [project README](../../README.md), then:

```sh
python -m meridian.ekf_experiment --output outputs/ekf-comparison
python -m pytest -q
```

The equivalent installed command is `meridian-ekf-comparison`. Always choose a
new output directory. The selected [summary](summary.json) and figure were copied
unchanged from an executed default run. Two runs, via the module and installed
command, produced **20 byte-identical artifacts** with Python 3.12.3, NumPy 2.5.3,
and Matplotlib 3.11.2 on Linux. Exact bytes across other environments are not promised.
The public test suite passed **374 tests** at this result snapshot.

## Evaluation contract

The [existing paired scenarios](../accelerometer-fusion/README.md) are unchanged:
30 s of pure roll, ±20° at 0.1 Hz; 100 Hz interval-mean gyro rates with constant
bias +0.5°/s and independent noise std 1°/s per interval; 10 Hz accelerometer
observations with independent y/z noise std 0.2 m/s² per component. The disturbed
case adds +2 m/s² body-y translation for `12 <= t < 17 s`. Each nominal/disturbed
pair shares the same gyro and accelerometer noise. Seed 42 is displayed; seeds
0–19 are a fixed additional repeat set, each evaluated in both conditions.

All methods start at the known angle 0°. Both Kalman filters start with zero bias,
zero angle variance, bias std 1°/s, and zero cross-covariance. Both assume constant
bias and `Q = diag(sigma_g² * dt², 0)`. The vector EKF uses fixed
`R = sigma_a² * I_2`; the angle KF uses the nominal first-order variance
`R_angle = (sigma_a/g)²`. Neither rejects physically inconsistent measurements.
Prediction precedes each endpoint correction; the first correction is at 0.1 s.

Before execution, nominal default criteria were set to **EKF roll RMSE < 0.75°**
and **final absolute bias error < 0.15°/s**, separately for seed 42 and every
seed 0–19. All passed. These are regression limits for this configuration; no
requirement to outperform another filter or pass a nominal accuracy limit during
the disturbance is imposed. Custom physical/noise settings disable these limits;
changing only the requested seeds retains them for the requested seed set.

## Observed results

Roll RMSE over all 3,001 endpoints, including the known initial angle:

| Estimator | Nominal, seed 42 (deg) | Translation pulse, seed 42 (deg) |
| --- | ---: | ---: |
| Gyro integration | 8.853980 | 8.853980 |
| Complementary | 0.659617 | 3.877226 |
| Angle KF | 0.353940 | 3.878906 |
| Vector EKF | 0.353101 | 3.831214 |

The EKF's nominal final bias is **0.500910°/s**, against the true 0.5°/s.
The two Kalman formulations produce almost overlapping nominal trajectories.
This agrees with their analytical relationship: the vector correction uses
`(norm(z)/g) * sin(theta_acc - theta_predicted)` where the angle KF uses the
wrapped angular difference. They are close for small prediction error and
magnitude near g; the result is not evidence of general EKF superiority.

During the pulse, the EKF bias estimate rises as high as **1.116250°/s** although
the true bias remains 0.5°/s. Its final bias returns to **0.509933°/s**, but that
final value hides substantial transient error. After the pulse (`t >= 17 s`),
roll RMSE is **3.640057°** for the EKF, **3.653398°** for the angle KF, and
**1.879181°** for the complementary filter. All windows and observation-epoch
metrics are retained in the JSON; no settling-time claim is inferred.

Additional seeds 0–19, excluding displayed seed 42:

| Metric | Angle KF mean / worst | Vector EKF mean / worst |
| --- | ---: | ---: |
| Nominal roll RMSE (deg) | 0.245127 / 0.327128 | 0.244815 / 0.327756 |
| Nominal final absolute bias error (deg/s) | 0.011717 / 0.046627 | 0.011721 / 0.046816 |
| Disturbed roll RMSE (deg) | 4.038472 / 4.167322 | 3.990478 / 4.117733 |

Mean and worst values describe this fixed repeat set, not population confidence
bounds. Small differences do not establish a reliable performance ranking.

## Covariance and innovations

With isotropic component noise, the EKF covariance recursion is mathematically
identical to the angle KF's under these settings, including during translation.
The largest difference among their stored SI covariance entries is
`5.42e-20` for each seed-42 scenario. The bias uncertainty contracts even while
the estimate moves away from truth. These are model covariances, not calibrated
physical uncertainty bounds.

The vector innovation has two dimensions; the angle innovation has one.
Mean joint EKF NIS is **1.966964** nominal and **11.242083** disturbed; scalar
angle-KF means are **1.021764** and **9.299290**. Their local Gaussian reference
means are 2 and 1, respectively. The figure plots NIS divided by its dimension
and uses independent vertical scales. This is a model diagnostic, not an
accuracy ranking, statistical consistency test, or automatic rejection mechanism.

Only the tangential vector innovation corrects the state. A radial mismatch
contributes to NIS without correcting angle. Tests explicitly show that zero
force and an exactly opposite gravity vector can leave an erroneous state
unchanged while covariance contracts. The [model guide](../../docs/vector-ekf.md)
derives these identities and the local convergence limitation.

## Exports and verification

Each scenario retains the seven [baseline CSV exports](../accelerometer-fusion/README.md)
unchanged and adds two files:

| File | Contents |
| --- | --- |
| `ekf_estimates.csv` | Endpoint time, unwrapped roll (rad), bias (rad/s), covariance entries (rad², rad²/s, rad²/s²). |
| `ekf_innovations.csv` | Observation time, prior y/z innovations (m/s²), S entries (m²/s⁴), joint NIS. |

The baseline `estimates.csv` and `innovations.csv` retain their original meaning;
the EKF has separate files because its innovation units and dimension differ.
Measurements, simulated truth, and prescribed disturbance remain separate.
State records follow any correction at their endpoint, otherwise prediction.
RMSE uses unwrapped state-minus-truth differences; raw tilt metrics use principal
differences at the 300 observation epochs.

Core tests compare the Jacobian with finite differences and corrections with
independent least-squares and tangent-projection calculations. Experiment tests
check unchanged baseline inputs/results, sparse update timing, paired-noise
causality, covariance equality, joint NIS, and reproducible exports. The full
simulation correction was also checked against the scalar-equivalent formulation.

The experiment assumes known initialization, constant bias, pure roll, white
component noise, and exact interval-mean rates. There is no complete translation
dynamics model, lever arm, time delay, calibration error, or physical gate.
The later [controlled suite](../controlled-scenarios/README.md) evaluates additional
initialization/bias/timing scenarios without changing this result snapshot.
The later [C++ agreement study](../cpp-parity/README.md) reuses these inputs and
the controlled cases without changing this snapshot.
This vector EKF has **only been evaluated in simulation**. Existing historical
IMU replay uses the earlier filters. New documented bench acquisition and replay
remain required; no embedded, real-time, or flight validation is claimed.
