# Gyroscope bias random walk: model and comparison protocol

The vector EKF accepts an optional bias diffusion intensity. Zero retains the
constant-bias reference; a positive value allows uncertainty to grow between
measurements so subsequent observations can track a changing bias. The angle
Kalman reference and existing experiment defaults remain constant-bias models.

## Model and units

Let `sigma_b` have units **(rad/s)/sqrt(s)** and `q_b = sigma_b²` have units
rad²/s³. Model the unknown bias increment as `db = sigma_b dW`. This is a prior
model for uncertainty, not an assertion that a real sensor follows Brownian motion.
Its bias increment variance over dt is `q_b dt`; sigma_b is not a deterministic
bias slope in rad/s². Mean-state prediction remains:

```text
theta_next = theta + dt * (gyro_mean - bias)
bias_next = bias
F = [[1, -dt], [0, 1]]
P_next = F P F.T + Q_gyro + Q_bias
Q_gyro = [[sigma_g² dt², 0], [0, 0]]
Q_bias = q_b * [[dt³/3, -dt²/2], [-dt²/2, dt]]
```

The bias disturbance also integrates into angle during the interval. For the
continuous matrix `A = [[0,-1],[0,0]]` and bias injection `L = [0,1].T`,
`exp(A u)L = [-u,1].T`. Integrating
`q_b * [-u,1].T [-u,1]` from zero to dt gives the displayed Q_bias. The negative
cross term follows from subtracting bias from the gyro rate. Its entries have
units rad², rad²/s and rad²/s². Adding only `q_b dt` to Pbb would omit the coupled
angle uncertainty for this continuous model. This applies the exact linear-SDE
discretization described in Särkkä and Solin,
[Applied Stochastic Differential Equations, chapter 6](https://users.aalto.fi/~asolin/sde-book/sde-book.pdf#page=89);
the subtraction convention determines the sign used here.

The existing sigma_g is still a standard deviation **per interval-mean gyro
sample**. Its variance scales with dt², not dt; it has not been converted into
a continuous density. Bias and gyro disturbances are assumed independent.
The gravity-vector observation, fixed isotropic R and Joseph correction are unchanged.

Python keyword and C++ `EkfConfig` field:
`bias_random_walk_std_rad_s_per_sqrt_s`, default **0.0**. The C++ replay accepts
the optional flag `--bias-random-walk-std-rad-s-per-sqrt-s`. Existing required
options and event CSV protocol version 1 remain supported. Invalid, negative,
nonfinite or squared-overflow intensities are refused; failed predictions leave
state and covariance unchanged. At zero, the additional computation is skipped
to preserve the original numerical path.

## Protocol fixed before execution

Version 1, declared 2026-09-15. Compare **0, 0.03 and 0.1 (deg/s)/sqrt(s)**,
converted to SI before construction. Their bias-increment standard deviations
over one second are respectively 0, 0.03 and 0.1 deg/s. Keep every setting;
no search, selection, default retuning or new accuracy pass/fail threshold.

Use two existing [controlled cases](controlled-scenarios.md):

| Case | True bias | Purpose |
| --- | --- | --- |
| `nominal` | Constant +0.5 deg/s. | Quantify the cost of allowing a change that is absent. |
| `bias_ramp` | +0.5 to +1.5 deg/s over 10–20 s, then constant. | Measure tracking of a deterministic model mismatch and subsequent settling. |

The ramp is not a realization of the random-walk model. Its slope is not supplied
to the filter, and the experiment does not calibrate sigma_b from that slope.
Both cases use 30 s of sinusoidal pure roll (20°, 0.1 Hz), 3,000 gyro intervals
and 300 force observations. Generated and assumed gyro noise is 1 deg/s per
interval; generated and assumed force component noise is 0.2 m/s², so R stays
0.04 I₂. Initial state is [0,0], angle uncertainty zero, bias uncertainty 1 deg/s,
with zero cross-covariance. The only filter parameter varied is sigma_b.

Generate once per case/root seed and pass exactly the same serialized input
operations to all settings and both languages. Within each seed the two cases
share accelerometer inputs and gyro noise draws; gyro measurements differ by
the imposed mean bias after the ramp begins. Do not pool paired cases as
independent trials. Truth and instantaneous bias are reserved for scoring.

- Exploration: seeds **0–19**, already familiar from earlier experiments.
- Final evaluation: seeds **2000–2019**, reserved before development checks and
  experiment execution; 1000–1019 were consumed by the R study and are not reused.
- Implementation checks may use **42** or exploratory **0**; no final outcomes
  enter tests or parameter choices. First inspect exploration, then run final
  evaluation without changing this protocol. Once viewed, final seeds are consumed.

Keep all per-seed metrics, paired differences against sigma_b=0, arithmetic
means and observed min–max separately by case and phase. Negative RMSE differences
mean smaller observed error; ranges are not confidence intervals. No universal
winner is declared. The local dated protocol is not an external registration.

## Metrics and numerical evidence

Use unwrapped error and all 3,001 post-prediction/post-correction endpoints,
including initialization. Report full-run angle and bias RMSE, signed final errors,
maximum absolute errors and the existing trapezoidal time-weighted angle RMSE.
Also report angle/bias RMSE on closed windows **10–20 s** (1,001 endpoints) and
**25–30 s** (501 endpoints), without restarting the estimator at window edges.

For the last window, report mean bias error and population temporal standard
deviation of bias error (ddof=0) around that mean. This centered fluctuation measure
can include residual settling and correlated errors; it is not white sensor noise
identification. The full bias RMSE includes the initial -0.5 deg/s error.
Joint mean NIS uses the full prior 2 x 2 S at all 300 corrections. It is a
diagnostic, not a score to minimize or a consistency certificate, particularly
under the deterministic ramp and deliberately chosen diffusion intensities.

Check finite outputs, symmetric PSD state covariances (1e-12 SI tolerance), and
positive-definite innovation covariance. Compare Python and C++ after **every
serialized operation** for every reported setting/seed/case using the unchanged
[per-quantity agreement tolerances](cpp-ekf.md). Port agreement establishes
implementation agreement, not physical accuracy. Numerical and comparison
failures abort the run rather than quietly dropping a seed.

## Reproduce

Build the current C++ replay, then use new output directories:

```sh
cmake -S . -B build/cpp -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build/cpp --parallel 2
python -m meridian.bias_experiment --binary build/cpp/meridian_ekf_replay --phase exploration --output outputs/bias-exploration
python -m meridian.bias_experiment --binary build/cpp/meridian_ekf_replay --phase evaluation --output outputs/bias-evaluation
```

Each phase retains all scores and agreement summaries, inputs/protocol/source
fingerprints and environment metadata, with figures and full traces for its
predeclared first seed (0 or 2000). The [result report](../results/bias-random-walk/README.md)
contains the selected summaries and figures. No physical acquisition or sensor
noise identification is implied; documented bench acquisition remains required.
