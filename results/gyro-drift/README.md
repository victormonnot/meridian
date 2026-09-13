# Gyroscope integration drift

This controlled simulation measures how constant bias and independent rate noise
accumulate when integrating a one-axis gyroscope. It compares three sensor
conditions on the same trajectory, with the same known initial angle. No filter
correction or bias estimation is applied.

![Roll, interval-average rates, and angle errors](overview.png)

## Model and sample convention

Positive roll follows the right-hand rule about the forward body axis. The model
assumes pure roll; a gyro's body-x rate is not generally the roll-angle derivative
in arbitrary 3D motion. All computations and CSV values use radians and seconds;
plots and reported errors use degrees.

For endpoints `t[k]`, the simulator defines:

```text
theta(t) = A * sin(2 * pi * f * t)
dt[k] = t[k+1] - t[k]
omega_mean[k] = (theta(t[k+1]) - theta(t[k])) / dt[k]
omega_measured[k] = omega_mean[k] + b + epsilon[k]
epsilon[k] ~ Normal(0, sigma_rate^2), independently across intervals
```

These are **mean rates over intervals**, not instantaneous samples of the
analytical derivative. This deliberately removes numerical quadrature error from
the ideal case. It is an idealized measurement model, not an established sampling
convention for a physical IMU or flight-controller log.

The integrator receives timestamps, rates, and an initial angle:

```text
theta_hat[0] = theta(0) = 0
theta_hat[k+1] = theta_hat[k] + omega_measured[k] * dt[k]
```

There are `N` rate samples and `N+1` angle endpoints. The integrator uses actual
interval durations, returns unwrapped angles, and has no access to the true bias
or subsequent true angles. Known initialization is a shared experimental condition,
not an initialization algorithm. The simulator uses a uniform grid; the integrator
also has independent tests with irregular intervals.

## Configuration and measured result

| Parameter | Value |
| --- | --- |
| Duration / interval rate | 30 s / 100 Hz (3,000 intervals) |
| Roll amplitude / frequency | 20° / 0.1 Hz |
| Constant bias | 0.5°/s |
| Noise standard deviation | 1°/s per interval-mean rate sample |
| Random generator / seed | NumPy `Generator(PCG64)` / 42 |
| Environment | Python 3.12.3, NumPy 2.5.3, Matplotlib 3.11.2, Linux |

These parameters are illustrative, not calibrated measurements from a drone.
Noise standard deviation is specified per rate sample; it is not a continuous-time
noise density. Holding it fixed while changing the interval rate changes the
integrated noise statistics.

| Gyro condition | Final signed error (°) | Angle RMSE (°) | Maximum absolute error (°) |
| --- | ---: | ---: | ---: |
| Ideal | < 1e-10 in magnitude | < 1e-10 | < 1e-10 |
| Bias only | 15.000000 | 8.660976 | 15.000000 |
| Bias + noise | 14.234313 | 8.018184 | 14.234313 |

Error is `estimate - truth`. RMSE is the unweighted root mean square over all
3,001 angle endpoints, including initialization. Full precision metrics and
configuration are stored in [summary.json](summary.json).

The bias-only final error matches `b * T = 0.5 * 30 = 15°`. The ideal error is
limited to floating-point roundoff because of the interval-mean construction.
For independent zero-mean rate noise, the final noise contribution has theoretical
standard deviation `sigma_rate * sqrt(sum(dt[k]^2))`, here **0.547723°**. This is
a prediction from the assumed noise model, not a measured uncertainty estimate
from the integrator.

For seed 42, integrated noise happens to offset some positive bias, leaving
14.234313° final error. This single realization does not demonstrate that adding
noise improves an estimator. In a separate stationary run with the same settings
and seed, the same drift metrics are obtained: in this construction the error
depends on sensor error and elapsed time, not on the chosen true motion.

## Reproduce and inspect

Use the Python 3.12 environment setup in the [repository README](../../README.md),
then run from the repository root:

```sh
python -m meridian.gyro_drift --output outputs/gyro-drift
python -m meridian.gyro_drift --amplitude-deg 0 --output outputs/gyro-drift-stationary
python -m pytest -q
```

Choose unused output directories. Each run exports:

| File | Contents |
| --- | --- |
| `measurements.csv` | Interval start/end times and the three gyro rate streams, in s and rad/s. |
| `truth.csv` | Endpoint times and true roll, in s and rad. |
| `estimates.csv` | Endpoint times and the three integrated angles, in s and rad. |
| `summary.json` | Configuration, sample semantics, metrics, analytical predictions, and environment versions. |
| `overview.png` | Angle, rate, and error plots; rate samples appear at interval midpoints. |

The selected figure and summary are copied from the default run without manual
changes. CSVs can be regenerated and are kept with local runs under `outputs/`.
Two full default CLI runs produced byte-identical CSV, JSON, and PNG files in the
listed environment. Tests also check analytical constant-bias drift, irregular
interval integration, random-state isolation, export alignment, invalid input,
and refusal to overwrite existing results.

Use [requirements.lock](../../requirements.lock) to recreate the tested dependency
versions. NumPy documents limits on random-stream compatibility across versions
and environments; a seed alone is not a universal reproducibility guarantee.
See the [NumPy compatibility policy](https://numpy.org/doc/stable/reference/random/compatibility.html).
Cross-platform bitwise identity, including rendered figures, has not been tested.

## Limits and continuation

This experiment verifies a gyro-only baseline under a controlled sensor model.
It does not estimate bias or uncertainty and does not contain accelerometer data,
Kalman filtering, bias variation, temperature effects, correlated noise, timing
jitter in simulated measurements, or a 3D attitude model. The numerical tests do
not establish physical IMU accuracy or statistical performance across many seeds.

Next, add an independent noisy angle observation and the linear Kalman reference.
Real drone acquisition and replay remain required for the full study; log timing,
units, calibration, and sample semantics must be checked before adaptation. No
embedded, real-time, or flight validation is claimed.
