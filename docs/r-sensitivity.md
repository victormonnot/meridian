# Accelerometer covariance sensitivity protocol

Protocol version 1, fixed on 2026-09-15 before evaluating this experiment.
This study varies only the vector EKF's assumed accelerometer covariance on
shared simulated measurements. It does not choose a new project default or
identify the noise of a physical IMU.

## Fixed comparison

Use two existing cases from the [controlled suite](controlled-scenarios.md):

| Case | Generated component noise standard deviation | Other changes |
| --- | --- | --- |
| `nominal` | 0.2 m/s² | None. |
| `accel_noise_mismatch` | 0.6 m/s² | None; the name is inherited from the fixed-tuning suite. |

For each case and seed, generate the measurements once. Run the unchanged
`AngleBiasEKF` with each assumed component standard deviation below:

| Assumed standard deviation | R diagonal, in (m/s²)² | Variance relative to the default |
| --- | --- | --- |
| 0.1 m/s² | 0.01 | 0.25 |
| 0.2 m/s² (reference) | 0.04 | 1 |
| 0.6 m/s² | 0.36 | 9 |

`R = sigma_assumed² I₂`: these are standard deviations, not direct R values.
The grid is intentionally small and declared in advance. Keep all settings in
the final evaluation; there is no adaptive search, selected winner, or automatic
retuning. In the second case, 0.6 is the matched assumption; 0.2 remains the
comparison reference, not a matched-noise label.

All runs use 30 s of pure roll, 20° amplitude, 0.1 Hz, 3,000 interval-mean gyro
samples at 100 Hz and 300 accelerometer observations at 10 Hz. Predictions precede
corrections at endpoints. Use the existing forward/right/down frame and
`h(theta) = [-g sin(theta), -g cos(theta)]`, with g = 9.80665 m/s².
Generated gyro bias is constant at +0.5°/s and gyro noise standard deviation is
1°/s per interval mean. The EKF assumes that same gyro noise and
`Q = diag(sigma_gyro² dt², 0)`. Initialize angle and bias at zero, angle standard
deviation at zero (known initial angle), bias standard deviation at 1°/s and
cross-covariance at zero. Changing R must not change P0, Q, timestamps or inputs.

Retain gyro integration and the complementary filter (tau = 1 s) as fixed angle
baselines on the same measurements and initialization. They do not estimate bias
or have a covariance/NIS diagnostic. The angle KF is already compared in the
earlier reports and is not retuned here. No C++ algorithm or browser code changes
are part of this experiment.

## Exploration and final evaluation

- Exploration: root seeds **0–19**, already used by the public controlled suite.
- Final evaluation: root seeds **1000–1019**, reserved before these runs and not
  used to develop or test this study before the protocol and code checks are fixed.
- Seed **42** may be used for implementation regression checks; it is not in
  either reported aggregate. Tests must not inspect final-evaluation outcomes.

Use the existing SeedSequence child streams and PCG64 generator. Within a case,
all R settings receive the exact same arrays. Across the two cases at a given
root seed, gyro inputs are identical and standardized accelerometer noise draws
are paired; only their amplitude changes. The cases are therefore not independent
replications. Never pool cases or exploration/final phases into one score.

Run exploration first and inspect it without changing this grid, the metrics or
the reporting rules. Then run the final phase separately. Publish every per-seed
score from both phases, including unfavorable results. Once final results have
been viewed, these seeds are no longer an untouched evaluation set for future
tuning. Any later revised study needs a disclosed protocol and new evaluation
seeds. The dated local protocol is not an externally registered experiment.

## Metrics fixed before execution

For each 30 s run, include all 3,001 post-prediction/post-correction endpoints,
including initialization. Report unwrapped angle RMSE in degrees, bias RMSE in
degrees/s, final signed errors, maximum absolute errors, and angle/bias RMSE for
the fixed closed window **25 <= t <= 30 s** (501 endpoints). No filter restarts at
the window boundary. Also retain the existing trapezoidal duration-weighted angle
RMSE, which approximates integration across correction jumps.

For the EKF, retain the mean joint NIS over all 300 corrections:
`NIS = innovation.T solve(S, innovation)`, using the full 2 x 2 prior innovation
covariance. Its dimension is two. A value near two is a model reference under
appropriate assumptions, not a pass/fail criterion or proof of consistency.
The initial bias error is fixed rather than sampled from P0, and startup is
included. Do not treat these correlated temporal values as independent trials.

Aggregate each metric over seeds with the arithmetic mean and observed min–max.
For every alternative R, compute **per-seed metric minus that seed's default-R
metric** before aggregating. For RMSE, negative differences mean smaller observed
error; report counts of negative/zero/positive differences. NIS differences have
no better/worse interpretation. Ranges describe these 20 runs and are not
confidence intervals. No required ranking or new physical accuracy threshold is
introduced. Numerical checks require finite outputs and symmetric PSD state
covariance within 1e-12 in stored SI units; innovation covariance must be positive
definite. Keep existing reference tests separate from this descriptive study.

## Reproduction and artifacts

From the pinned Python environment, use separate new directories:

```sh
python -m meridian.r_sensitivity --phase exploration --output outputs/r-exploration
python -m meridian.r_sensitivity --phase evaluation --output outputs/r-evaluation
```

Each phase exports its protocol settings, stream seeds, measurement fingerprints,
all per-seed scores and paired differences in JSON, plus a comparison figure.
It also exports full-resolution measurements, separate truth, estimates,
covariance and innovations for the first seed in that phase (0 or 1000), selected
in advance for reproducible inspection. Metrics always use full-resolution runs.
Existing output directories are refused. The [result report](../results/r-sensitivity/README.md)
records both phases, commands, environment and interpretation.

This study varies white-noise assumptions within the same simulation family. It
does not test a new motion distribution, translation, timing mismatch, varying
bias or physical calibration. Those limitations do not disappear if a larger R
reduces an error on these runs. The [translation report](../results/ekf-comparison/README.md)
already documents a gravity-model violation. Real bench acquisition and replay
with a stated reference remain required; no embedded or flight validation follows.
