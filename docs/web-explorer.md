# Simulation results explorer

The explorer replays seven runs at seed 42: nominal and translation-disturbed
from the [vector EKF comparison](../results/ekf-comparison/README.md), plus
`initial_offset`, `initial_overconfident`, `accel_dropout`, `bias_ramp` and
`accel_noise_mismatch` from the
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
- Use **Play**, **Pause**, the time slider, or the speed selector. Playback starts
  paused at 8 s, stops at 30 s, and pauses when the document becomes hidden.
  **Replay** starts again at zero. Selecting a scenario pauses playback and moves
  to 8 s for nominal and underestimated noise, zero for either initialization case, or inside the event for translation
  and loss (14 s in the selected data). **Inspect start**, **Inspect loss** and
  **Inspect disturbance** revisit those points. **Inspect recovery** moves to 17 s.
  The bias-ramp selection and **Inspect ramp** move to 15 s; **Inspect plateau**
  moves to 20 s, where the true bias stops increasing.
  **Inspect end** moves to 30 s for either initialization case, to distinguish
  an initial recovery transient from remaining error.
  Method selection and curve visibility are preserved between scenarios.
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
  accelerometer-correction time is shown below the roll plot; before 0.1 s the
  inspector explicitly reports that no correction has occurred yet.
- Read **Full-run error** for angle RMSE. The amber row marker identifies the
  inspected method, not the best score. Expand the experiment details for
  settings, display limitations and the JSON download. The score includes the
  initialization transient. A lower RMSE for the dropout case on this seed does
  not establish that losing observations improves estimation.

Estimated bias curves use steps and the cursor holds these estimates until the
next recorded correction. Roll and true bias are linearly interpolated between
displayed endpoints; the true bias ramp is continuous. The nominal and
translation runs retain their shared scales; the five controlled cases have their own
full-resolution domains. Scales can change on scenario selection, as stated beside
the plot, and always include hidden methods.

The inspector precedes the plots in document order: left on wide screens, above
on narrow screens. Color is supplemented by line styles and labels. The
application uses the graphite theme; appearance controls used during design
exploration are not part of this version.

## Data contract and reproduction

`web/public/data/roll-comparison.json` is a selected, versioned display artifact
produced by `meridian.web_scenarios`. It reads existing simulation exports and
never reads a drone log or reruns a filter. The original `meridian.web_export`
continues to support its schema-1 paired-only export independently.

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
| `schema_version`, `experiment`, `data_source` | Version 2, `roll_scenario_explorer`, `simulation`. |
| `config`, `seed` | Fixed motion/schedule/filter settings and selected seed. `bias_deg_s` is the baseline/initial true bias; the ramp event specifies its later change. `accel_noise_std_m_s2` is the assumed noise standard deviation; actual noise belongs to each scenario. |
| `columns` | The same nine ordered fields: time in seconds, roll in degrees, bias in degrees/second. |
| `display_stride`, `display_decimal_places` | Every fifth endpoint by default, augmented with required event/correction points; six-decimal angle/bias rounding. |
| `sources.paired`, `sources.controlled` | Experiment names, relative input filenames and SHA-256 fingerprints, with separate namespaces for the two `summary.json` files. Large stream seeds remain decimal strings. |
| `scenarios.<name>.source` | Key into `sources`; metrics and provenance belong to that experiment. |
| `scenarios.<name>.initialization` | Initial estimated roll/bias and their model standard deviations. True initial roll is zero for all seven cases. |
| `scenarios.<name>.accelerometer_noise` | `actual_std_m_s2` describes the simulated per-component noise (0.6 for noise mismatch, otherwise 0.2); `assumed_std_m_s2` remains 0.2. These are distribution settings, not measured sample statistics. |
| `scenarios.<name>.domains` | Full-resolution angle/bias extrema covering every method. The original pair retains shared extrema. |
| `scenarios.<name>.source_sample_count` | 3,001 original endpoints, including initialization. |
| `scenarios.<name>.scheduled_accel_count` | 300 scheduled arrivals. |
| `scenarios.<name>.correction_times_s` | All received correction times: 300 normally, 250 during the loss case. |
| `scenarios.<name>.event` | Null or a typed translation, accelerometer-loss or bias-ramp event. Translation/loss end times are exclusive. The ramp records start/end bias and slope; after its end the bias stays at its final level. Loss records omitted count and corrections before/after the gap. |
| `scenarios.<name>.metrics` | Full-run endpoint angle RMSE and signed final bias errors, retained from the source summary. |
| `scenarios.<name>.rows` | 901 retained endpoints for each run except accelerometer loss (852), at stride 5. |

For the paired source, the adapter reuses the existing checks on `truth.csv`,
`estimates.csv`, `ekf_estimates.csv` and the summary, and also reads correction
timestamps from `ekf_innovations.csv`. For each selected controlled case it reads
truth, gyro interval truth, estimates with initial covariance, gyro/accelerometer measurements,
`observation_schedule_truth.csv` and both innovation timestamp columns. These
filenames and source hashes are included in the JSON; machine paths and generation
wall-clock times are absent. Hashes identify bytes, not authenticity.

The controlled-source checks require the declared scenario definitions and fixed
shared settings, aligned 0–30 s endpoints, sinusoidal roll and the declared bias law,
correct initial states/covariances, available measurements and actual correction
schedules. They verify gyro integration, shared controlled-case inputs, summary
correction counts/gaps, RMSE and final bias errors. Metrics are recomputed from
full-resolution records before rounding, with `1e-10` absolute/relative metric
tolerance. Both initialization cases use their erroneous initial estimate, not truth.

For the ramp, endpoint truth is `b(t) = 0.5 + 0.1 * clip(t - 10, 0, 10)` in
degrees/second. Gyro measurements use the mean bias over each interval. For
example, the mean over 14.99–15 s is 0.9995°/s, while the endpoint bias is 1°/s.
The adapter checks interval means against this piecewise-linear law; every knot
lies on the fixed endpoint grid. Controlled cases share gyro noise after removal
of true mean rate and mean bias, rather than sharing raw gyro values when the
bias changes. Accelerometer checks remove the gravity vector at the sample time
and divide by each case's actual component standard deviation. Those standardized
residuals match on available arrivals, including when raw noise is scaled by three.

At 30 s the recorded EKF bias estimate is about 0.934711°/s against 1.5°/s truth,
a signed error of −0.565289°/s. Its full-run angle RMSE is 1.523883°. These
describe the selected mismatch case; the filter has not been retuned and no
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

Roll interpolation still smooths a correction over its preceding 10 ms interval;
the export is not a before/after event trace. Display reduction has no anti-alias
filter and can hide short transients. RMSE uses all 3,001 original endpoints and
unwrapped angle errors, not displayed points. Neither these runs nor their ranking
establish general robustness. The display contract covers this explicit selection,
not arbitrary logs, irregular-time scenarios or delayed-measurement compensation.

## Implementation and checks

| Location | Responsibility |
| --- | --- |
| `src/meridian/web_export.py` | Preserve the original paired simulation adapter. |
| `src/meridian/web_scenarios.py` | Combine two checked source schemas, retain correction timing and export deterministic schema-2 JSON. |
| `web/src/data.js` | Browser contract checks, interpolated roll/true bias, held estimated biases, latest correction and playback arithmetic. |
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
points, source metrics and deterministic output.
JavaScript checks compare the shipped artifact with both public summaries and
fingerprints, and verify malformed-record rejection, initial confidence, actual/assumed noise, held bias
through loss/recovery, decimal-time roundoff and playback boundaries. Source review and local HTTP checks
complement these tests. They do not constitute automated browser interaction,
cross-device rendering or accessibility validation.

Only the seven selected Python simulation runs are displayed. There is no live
hardware connection, real-log browser replay, parameter tuning, C++ execution in
the browser, or statistical-consistency view. The similar nominal KF/EKF RMSE
does not imply superiority; the translation case demonstrates a shared physical
model limitation. New documented bench acquisition and real EKF replay remain
separate work.
