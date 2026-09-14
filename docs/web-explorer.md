# Simulation results explorer

The explorer replays nine runs at seed 42: nominal and translation-disturbed
from the [vector EKF comparison](../results/ekf-comparison/README.md), plus
`initial_offset`, `initial_overconfident`, `accel_dropout`, `bias_ramp`,
`accel_noise_mismatch`, `timing_jitter` and `accel_delay` from the
[controlled suite](../results/controlled-scenarios/README.md). It compares
gyro integration, complementary fusion, angle KF and vector EKF against simulation
truth. Playback reads recorded results; it does not execute an estimator.

## Run locally

Install Node.js **22.12 or newer** and npm. This version was checked with Node
22.22.1 and npm 10.9.4 on Linux. From the repository root:

```sh
npm --prefix web ci
npm --prefix web run dev
```

Open the local URL printed by Vite. The default port is 5173; if it is occupied,
use `npm --prefix web run dev -- --port 5186`. The server listens on loopback.
Python and C++ are not required to view the included data. Dependencies, fonts and
the selected data are bundled locally; viewing does not require a runtime CDN.

To build the static application and serve that build locally:

```sh
npm --prefix web run build
npm --prefix web run preview
```

`web/dist/` contains the resulting site. Asset URLs are relative, so the build can
also be served under a subdirectory. Serve it over HTTP; opening `index.html`
directly with `file://` is not supported. No hosting provider, deployment workflow,
backend or database is configured.

## Use the view

- **Nominal** uses a known initial roll and gravity-based accelerometer measurements.
- **Translation disturbance** adds +2 m/s² along body y for `12 <= t < 17 s`.
  The shaded interval marks this injection.
- **Wrong initial angle** starts all four estimates at +60°, with true roll 0°.
  The KF/EKF initial angle standard deviation is 30°.
- **Overconfident start** uses the same 60° initial estimate and measurements,
  but declares zero initial angle variance. Bias uncertainty and gyro process
  noise create angle uncertainty during prediction; the angle is not frozen.
  Gyro and complementary trajectories are identical in the two initialization
  cases because those methods do not use this covariance.
- **Accelerometer loss** omits 50 arrivals for `12 <= t < 17 s`. Gyro predictions
  continue. The last correction before loss is at 11.9 s and the next at 17 s;
  the 5.1 s gap is not filled with invented measurements. The shaded interval
  means no accelerometer corrections, not a translation or missing estimates.
- **Changing gyro bias** raises true bias from 0.5 to 1.5°/s over 10–20 s,
  then keeps it at 1.5°/s. All 300 corrections remain available. The shaded
  interval marks the ramp, not a loss of measurements. The filters retain their
  constant-bias prediction model and zero bias process noise; corrections can
  still change the bias estimates, which lag behind the reference in this run.
- **Underestimated noise** increases simulated accelerometer component standard
  deviation from 0.2 to 0.6 m/s² throughout the run. The filters still assume
  0.2 m/s²: the actual noise variance is nine times the assumed variance. The
  same standardized noise draws are used; all 300 corrections remain available.
  This case has no interval band. Expand the details to compare simulated and
  assumed noise, which are listed separately for every scenario.
- **Irregular timing** varies 3,000 gyro intervals over 30 s, with accelerometer
  arrivals at every tenth endpoint. In this seed, gyro intervals span
  4.970950–14.900578 ms and correction gaps span 72.916319–126.373220 ms.
  All methods use actual durations. The nominal 100/10 Hz rates describe average
  counts over the run, not a uniform clock. No samples are resampled onto a grid.
- **Delayed accelerometer** acquires samples 100 ms before their regular
  arrivals. The first acquisition is at 0 s, applied at 0.1 s; the last is at
  29.9 s, applied at 30 s. Source times describe the injected delay but are
  never supplied to the filters. This case evaluates ignored delay; it does
  not implement compensation. Neither timing case has a localized event band.
- Use **Play**, **Pause**, the time slider, or the speed selector. Playback starts
  paused at 8 s, stops at 30 s, and pauses when the document becomes hidden.
  **Replay** starts again at zero. Selecting a scenario pauses playback and moves
  to 8 s for nominal, underestimated noise and both timing cases, zero for either
  initialization case, or inside the event for translation
  and loss (14 s in the selected data). **Inspect start**, **Inspect loss** and
  **Inspect disturbance** revisit those points. **Inspect recovery** moves to 17 s.
  The bias-ramp selection and **Inspect ramp** move to 15 s; **Inspect plateau**
  moves to 20 s, where the true bias stops increasing.
  **Inspect end** moves to 30 s for either initialization case, to distinguish
  an initial recovery transient from remaining error.
  Method selection, metric weighting and curve visibility are preserved between scenarios.
- Select an estimate to inspect its roll and signed error at the cursor. The
  solid neutral drone follows that estimate; the dashed outline follows truth.
  The rear view looks along forward +X in a forward-right-down body frame:
  positive roll lowers the vehicle's right side.
- Toggle a method in either legend to change its curve visibility. Selecting
  an inspected method makes it visible again. A later legend toggle can hide
  its curve while leaving its numeric inspector active. Truth remains visible.
- Move a pointer across either plot to inspect the same instant in both signals.
  Dragging pauses playback. Keyboard users can use the native time slider; roll
  and all three bias readings update without requiring a tooltip. The latest
  accelerometer-correction time is shown below the roll plot; before the first arrival the
  inspector explicitly reports that no correction has occurred yet.
- In the two timing cases, **Previous correction** and **Next correction**
  move to exact recorded arrivals and pause playback. They are disabled when
  no earlier/later correction exists. The readout shows acquisition time,
  application time, age at arrival and the gap since the previous correction.
  Time labels round to milliseconds; navigation retains full precision. The
  slider has a 1 ms step and may select an interpolated instant between arrivals.
- Read **Full-run error** for angle RMSE. The amber row marker identifies the
  inspected method, not the best score. Expand the experiment details for
  settings, display limitations and the JSON download. The score includes the
  initialization transient. A lower RMSE for the dropout case on this seed does
  not establish that losing observations improves estimation.
  **Weight errors by** selects **Sample count** (default) or **Elapsed time**.
  Equal endpoint weighting and duration weighting answer different questions
  when the sampling intervals vary; neither is computed from display rows.

In **Trajectories**, estimated bias curves use steps and the cursor holds these estimates until the
next recorded correction. Roll and true bias are linearly interpolated between
displayed endpoints; the true bias ramp is continuous. The nominal and
translation runs retain their shared scales; the seven controlled cases have their own
full-resolution domains. Scales can change on scenario selection, as stated beside
the plot, and always include hidden methods.

The inspector precedes the plots in document order: left on wide screens, above
on narrow screens. Color is supplemented by line styles and labels. The
application uses the graphite theme; appearance controls used during design
exploration are not part of this version.

## Diagnostics view

Select **Diagnostics** beside **Trajectories**. It follows the method selected in
the left inspector, independently of trajectory legend visibility. The shared
time slider, playback and previous/next correction controls work in both views;
correction controls appear for every scenario in Diagnostics. Switching views
pauses playback and retains time, method and scenario.

**Error and model uncertainty** selects roll or gyro bias. The solid line is
`estimate - truth`, using the unwrapped angle convention. The shaded band and
dotted bounds are ±2 model standard deviations about zero, not an empirical
accuracy guarantee. All 3,001 original endpoints are plotted as steps. The
inspector uses the latest endpoint at or before the cursor and shows that
endpoint's timestamp; it does not interpolate uncertainty toward a future update.
State/P values are recorded after prediction and any available correction.
At zero they are the initial state and covariance. Gyro integration and the
complementary filter provide angle error only, with no invented covariance or bias.

**Measurement corrections** switches between raw **Innovation** and **NIS**:

- For the angle KF, the innovation is measured tilt minus predicted roll, with
  the principal angular difference displayed in degrees. Each dot is one correction.
- For the vector EKF, dots show body-y and crosses body-z force innovations in
  m/s², computed from the prior gravity prediction. They are not angular errors.
- NIS is `vᵀ S⁻¹ v`: dimensionless, with one measurement dimension for the angle
  KF and two for the vector EKF. The vector calculation includes the S cross term.
  The dashed line is the ideal-model mean (1 or 2), not a rejection threshold.
  Neither individual spikes nor this one-run mean constitute a consistency test.

All innovations are plotted as isolated markers at their actual arrival times;
there is no line or fabricated value through the dropout. The highlighted last
innovation stays at its original plotted time. The readout explicitly names its
arrival, acquisition and elapsed time since arrival, and reports the prior
innovation standard deviation(s) from S. Before the first correction it reports
no innovation. These are pre-correction diagnostics; the state at the same time
is post-correction. The gyro and complementary baselines do not expose S or NIS.

Scales include every diagnostic sample and adapt to method/component/scenario.
Large initialization transients can compress later detail; there is no time-window
zoom or outlier clipping. All displayed diagnostic samples are available in the
download; complete covariance entries remain in the source CSVs.

Two examples in the selected seed 42 make the limits visible:

| Case and instant | EKF roll error | Model roll standard deviation |
| --- | ---: | ---: |
| Translation, 14 s | −6.425734° | 0.221833° |
| Overconfident start, 30 s | −8.005958° | 0.203705° |

The nominal and translation cases have identical covariance histories despite
their different errors. During accelerometer loss, model angle standard deviation
rises from 0.228687° at 11.9 s to 0.423325° at 16.99 s, then falls to 0.398316°
after the 17 s correction. The first EKF innovation in the wrong-initial-angle
case has NIS 719.589703: the full-run mean includes this startup transient.

## Data contract and reproduction

`web/public/data/roll-comparison.json` is a selected, versioned display artifact
produced by `meridian.web_scenarios`. It reads existing simulation exports and
never reads a drone log or reruns a filter. The original `meridian.web_export`
continues to support its schema-1 paired-only export independently.
The combined artifact now uses schema 4, adding full endpoint diagnostics and
pre-correction innovation records to the explicit timing and two RMSE bases.
Earlier combined artifacts must be regenerated for the current browser contract.

With the Python environment described in the repository README:

```sh
python -m meridian.ekf_experiment --output outputs/web-paired-source
python -m meridian.stress_experiment --output outputs/web-controlled-source
python -m meridian.web_scenarios outputs/web-paired-source outputs/web-controlled-source --output outputs/web-display/roll-comparison.json
```

Use new output locations; the commands refuse existing destinations. Compare the
JSON with `web/public/data/roll-comparison.json` before deliberately replacing the
selected artifact. With the pinned environment and default settings, it is byte
reproducible. Changing the selected runs or display stride requires updating the
selected-data checks and descriptions.

| Field | Meaning |
| --- | --- |
| `schema_version`, `experiment`, `data_source` | Version 4, `roll_scenario_explorer`, `simulation`. |
| `config`, `seed` | Motion, nominal rates and filter settings, and selected seed. `sample_rate_hz` and `observation_every` describe nominal scheduling; irregular times belong to each scenario. `bias_deg_s` is initial true bias; the ramp specifies its later change. `accel_noise_std_m_s2` is assumed noise; actual noise belongs to each scenario. |
| `columns` | The same nine ordered fields: time in seconds, roll in degrees, bias in degrees/second. |
| `display_stride`, `display_decimal_places` | Every fifth endpoint by default, augmented with required event/correction points; six-decimal angle/bias rounding. |
| `sources.paired`, `sources.controlled` | Experiment names, relative input filenames and SHA-256 fingerprints, with separate namespaces for the two `summary.json` files. Large stream seeds remain decimal strings. |
| `scenarios.<name>.source` | Key into `sources`; metrics and provenance belong to that experiment. |
| `scenarios.<name>.initialization` | Initial estimated roll/bias and their model standard deviations. True initial roll is zero for all nine cases. |
| `scenarios.<name>.accelerometer_noise` | `actual_std_m_s2` describes the simulated per-component noise (0.6 for noise mismatch, otherwise 0.2); `assumed_std_m_s2` remains 0.2. These are distribution settings, not measured sample statistics. |
| `scenarios.<name>.domains` | Full-resolution angle/bias extrema covering every method. The original pair retains shared extrema. |
| `scenarios.<name>.source_sample_count` | 3,001 original endpoints, including initialization. |
| `scenarios.<name>.scheduled_accel_count` | 300 scheduled arrivals. |
| `scenarios.<name>.correction_times_s` | All received correction times: 300 normally, 250 during the loss case. |
| `scenarios.<name>.timing` | Uniform/irregular kind, full-resolution gyro interval min/max, declared `accel_delay_s`, and acquisition/predecessor arrays aligned with `correction_times_s`. |
| `timing.correction_sample_times_s` | Physical acquisition times from simulation provenance, distinct from arrival when delayed. |
| `timing.correction_predecessor_times_s` | Actual gyro endpoint immediately before each correction; not an assumed 10 ms subtraction. Each is retained in `rows`. |
| `scenarios.<name>.event` | Null or a typed translation, accelerometer-loss or bias-ramp event. Translation/loss end times are exclusive. The ramp records start/end bias and slope; after its end the bias stays at its final level. Loss records omitted count and corrections before/after the gap. |
| `scenarios.<name>.metrics` | Endpoint `rmse_deg`, duration-weighted `time_weighted_rmse_deg` and signed final bias errors. Existing endpoint/bias metrics remain unchanged; see weighting provenance below. |
| `scenarios.<name>.rows` | 901 retained endpoints for each run except accelerometer loss (852), at stride 5. |
| `scenarios.<name>.diagnostics.states` | All 3,001 endpoints. The first nine columns match `columns`; four further columns hold KF/EKF roll standard deviations (°), then KF/EKF bias standard deviations (°/s). `state_columns` names all 13. Six-decimal display rounding, with unrounded timestamps. |
| `diagnostics.state_phase`, `diagnostics.innovation_phase` | `endpoint_after_available_correction` and `prior_before_correction`; these are different stages at the same arrival. |
| `diagnostics.kalman` | Dimension 1; all arrival/innovation (°)/S (°²)/NIS rows plus mean NIS. |
| `diagnostics.ekf` | Dimension 2; all arrival/y/z innovation (m/s²)/S yy,yz,zz (m²/s⁴)/NIS rows plus mean NIS. |

For the paired source, the adapter reuses the existing checks on `truth.csv`,
`estimates.csv`, `ekf_estimates.csv` and the summary, and also reads correction
timestamps from `ekf_innovations.csv`. For each selected controlled case it reads
truth, gyro interval truth, estimates with initial covariance, gyro/accelerometer measurements,
`observation_schedule_truth.csv` and both innovation records. Diagnostics also
read all endpoint P entries and the paired `innovations.csv` scalar records. These
filenames and source hashes are included in the JSON; machine paths and generation
wall-clock times are absent. Hashes identify bytes, not authenticity.

The controlled-source checks require the declared scenario definitions and fixed
shared settings, aligned 0–30 s endpoints, sinusoidal roll and the declared bias law,
correct initial states/covariances, available measurements and actual correction
schedules. Uniform clocks are checked against the fixed grid; irregular clocks
must contain 3,000 positive nonuniform intervals spanning 30 s within the
normalized-weight bounds. Scheduled arrivals must match every tenth endpoint.
They verify gyro integration, shared standardized noise, summary
interval ranges/correction counts/gaps, both RMSE bases and final bias errors. Metrics are recomputed from
full-resolution records before rounding, with `1e-10` absolute/relative metric
tolerance. Both initialization cases use their erroneous initial estimate, not truth.

`web_diagnostics` checks full symmetric 2×2 P using nonnegative diagonals and
the cross term, and requires positive scalar S/positive-definite vector S.
It computes standard deviations as `sqrt(P_ii)` before converting to degrees,
never by rounding SI variance first. Every diagnostic trajectory endpoint used
in the reduced view must match that view. Innovation times must match all actual
corrections. NIS is recomputed from full-precision v/S and checked against recorded
values (absolute/relative tolerance `1e-10`); the paired scalar CSV has no NIS
column, so that NIS is derived. All means are checked against the original
summaries. Display innovation/S/NIS values retain source precision. These are
record-coherence checks, not a new execution or independent validation of a filter.

For the ramp, endpoint truth is `b(t) = 0.5 + 0.1 * clip(t - 10, 0, 10)` in
degrees/second. Gyro measurements use the mean bias over each interval. For
example, the mean over 14.99–15 s is 0.9995°/s, while the endpoint bias is 1°/s.
The adapter checks interval means against this piecewise-linear law; every knot
lies on the fixed endpoint grid. Controlled cases share gyro noise after removal
of true mean rate and mean bias, rather than sharing raw gyro values when the
bias changes. Accelerometer checks remove the gravity vector at the sample time
and divide by each case's actual component standard deviation. Those standardized
residuals match by scheduled draw order, retaining only available observations,
including when raw noise is scaled by three or acquisition times change.

For irregular timing the generator normalizes 3,000 positive `U(0.5, 1.5)` weights
to 30 s using its separate PCG64 timing stream. The adapter reads the resulting
timestamps without regenerating them. Noise remains 1°/s per gyro interval mean;
it is not a continuous-time noise density. The first arrival is
0.09331262108751584 s and its preceding endpoint is 0.08557741789562699 s.

Duration-weighted RMSE is `sqrt(sum((e[i]^2 + e[i+1]^2) * dt[i] / 2) / 30)`.
It integrates squared endpoint errors with the trapezoid rule, not the exact
continuous error across correction jumps. Controlled values are checked against
their source summary; for the original nominal/translation pair this additional
metric is computed by the adapter from original unrounded CSVs. Each source's
`time_weighted_metric_basis` records that distinction. The paired summary does
not claim to contain the added metric.

| Timing case, seed 42 | EKF endpoint RMSE (°) | EKF duration-weighted RMSE (°) |
| --- | ---: | ---: |
| Irregular timing | 0.354618 | 0.355209 |
| Delayed accelerometer | 0.855216 | 0.855358 |

For the bias-ramp case, at 30 s the recorded EKF bias estimate is about
0.934711°/s against 1.5°/s truth,
a signed error of −0.565289°/s. Its full-run angle RMSE is 1.523883°. These
describe the selected bias-ramp case; the filter has not been retuned and no
nominal accuracy threshold or general superiority claim applies to this run.

With zero declared initial angle variance, the EKF ends at a roll error of
−8.005958° and an estimated bias of 2.777863°/s against 0.5°/s truth. Its full-run
angle RMSE is 20.230017°, versus 3.573630° for the same wrong initial angle with
30° declared standard deviation. A brief crossing of truth is not convergence;
the erroneous initial state can be partly absorbed into the estimated gyro bias.
In the noise-mismatch case the EKF RMSE is 1.039988°, versus 0.353101° in the
nominal case. These values are from seed 42 with unchanged algorithms and tuning;
the [controlled report](../results/controlled-scenarios/README.md) includes repeats.

Every correction endpoint and its immediately preceding endpoint is retained,
including at a nondefault display stride. Event boundaries and initial/final
endpoints are also retained. Estimated bias changes outside recorded corrections are rejected.
This allows step curves and held bias readings without an apparent correction
before the observation arrives; for example the dropout bias stays constant
through 16.99 s and changes at 17 s. Time lookup tolerates `1e-10` s roundoff
between decimal controls and binary timestamps.

In Trajectories, roll interpolation still smooths a correction over its preceding gyro interval
(10 ms in uniform cases, the recorded duration in the irregular case);
the export is not a before/after event trace. Display reduction has no anti-alias
filter and can hide short transients. RMSE uses all 3,001 original endpoints and
unwrapped angle errors, not displayed points. Neither these runs nor their ranking
establish general robustness. The display contract covers this explicit selection,
not arbitrary logs or delayed-measurement compensation.

## Implementation and checks

| Location | Responsibility |
| --- | --- |
| `src/meridian/web_export.py` | Preserve the original paired simulation adapter. |
| `src/meridian/web_scenarios.py` | Combine two checked sources, retain timing/metrics and export deterministic schema-4 JSON. |
| `src/meridian/web_diagnostics.py` | Read complete endpoint covariance and pre-correction innovations, check covariance/NIS and attach diagnostics. |
| `web/src/data.js` | Browser contract checks, interpolation/held biases, correction navigation and playback arithmetic. |
| `web/src/diagnostics.js` | Diagnostic validation, held endpoint selection, errors and timestamped latest innovations. |
| `web/src/diagnostic-chart.js` | Error/model bands as steps, discrete innovation/NIS markers and synchronized cursor. |
| `web/src/chart.js` | D3 scales, paths, shared cursor and pointer inspection. |
| `web/src/main.js` | Load data and connect scenario selection, playback, legends and readings. |
| `web/src/style.css`, `web/index.html` | Appearance, semantic structure and responsive layout. |

JavaScript ES modules keep the presentation small. [Vite](https://vite.dev/guide/)
handles local development and the static build; [D3](https://d3js.org/getting-started)
provides scales, axes and SVG paths. Versions are pinned in `web/package-lock.json`.
Fonts are packaged with the application. Runtime dependency notices are included
in `web/public/third-party-notices.txt` and copied into the build.

```sh
python -m pytest -q tests/test_web_export.py tests/test_web_scenarios.py
npm --prefix web test
npm --prefix web run build
```

Python checks cover both source adapters, input rejection, units, initialization,
availability, bias truth and interval means, shared standardized noise, retained correction
points, actual acquisition/arrival times, unrounded weighted metrics, covariance/S
validity, diagnostic units/NIS and deterministic output.
JavaScript checks compare the shipped artifact with both public summaries and
fingerprints, and verify malformed-record rejection, initial confidence, actual/assumed noise, held bias
through loss/recovery, irregular correction navigation, delay provenance, decimal-time roundoff
and playback boundaries. Diagnostic checks cover held full endpoints, absent
dropout innovations, model uncertainty versus actual error, unavailable baseline
diagnostics and malformed records. Source review and local HTTP checks
complement these tests. They do not constitute automated browser interaction,
cross-device rendering or accessibility validation.

Only the nine selected Python simulation runs are displayed. There is no live
hardware connection, real-log browser replay, parameter tuning, C++ execution in
the browser, or formal statistical-consistency evaluation. The similar nominal KF/EKF RMSE
does not imply superiority; the translation case demonstrates a shared physical
model limitation. New documented bench acquisition and real EKF replay remain
separate work.
