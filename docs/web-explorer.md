# Simulation results explorer

The explorer replays the nominal and translation-disturbed runs from the
[vector EKF comparison](../results/ekf-comparison/README.md), seed 42. It compares
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

- Select **Nominal** or **Translation disturbance**. The latter adds +2 m/s²
  along body y for `12 <= t < 17 s`; the shaded interval marks this injection.
- Use **Play**, **Pause**, the time slider, or the speed selector. Playback starts
  paused at 8 s, stops at 30 s, and pauses when the document becomes hidden.
  **Replay** starts again at zero. **Inspect disturbance** moves to 14 s.
- Select an estimate to inspect its roll and signed error at the cursor. The
  solid neutral drone follows that estimate; the dashed outline follows truth.
  The rear view looks along forward +X in a forward-right-down body frame:
  positive roll lowers the vehicle's right side.
- Toggle a method in either legend to change its curve visibility. Selecting
  an inspected method makes it visible again. A later legend toggle can hide
  its curve while leaving its numeric inspector active. Truth remains visible.
- Move a pointer across either plot to inspect the same instant in both signals.
  Dragging pauses playback. Keyboard users can use the native time slider; roll
  and all three bias readings update without requiring a tooltip.
- Read **Full-run error** for angle RMSE. The amber row marker identifies the
  inspected method, not the best score. Expand the experiment details for
  settings, display limitations and the JSON download.

The inspector precedes the plots in document order: left on wide screens, above
on narrow screens. Color is supplemented by line styles and labels. The
application uses the graphite theme; appearance controls used during design
exploration are not part of this version.

## Data contract and reproduction

`web/public/data/roll-comparison.json` is a selected, versioned display artifact
generated from public experiment code. The exporter reads only `summary.json`
and, for each scenario, `truth.csv`, `estimates.csv`, and `ekf_estimates.csv`.
It does not read a drone log or rerun any filter.

With the Python environment described in the repository README:

```sh
python -m meridian.ekf_experiment --output outputs/web-source
python -m meridian.web_export outputs/web-source --output outputs/web-display/roll-comparison.json
```

Use new output locations; both commands refuse existing destinations. Compare
the generated JSON with `web/public/data/roll-comparison.json` before deliberately
replacing a selected artifact. With the pinned Python environment and default
settings, the included artifact is byte reproducible. Changing the input run or
stride requires updating the corresponding selected-data checks and descriptions.

| Field | Meaning |
| --- | --- |
| `schema_version`, `experiment`, `data_source` | Version 1 of this presentation contract, `vector_ekf_comparison`, `simulation`. |
| `config`, `seed` | Original simulation settings and selected seed. |
| `columns` | Ordered row fields; time in seconds, roll in degrees, bias in degrees/second. |
| `display_stride`, `display_decimal_places` | Every fifth endpoint by default, with six-decimal angle/bias rounding. |
| `domains` | Min/max over original full-resolution records, shared across scenarios. |
| `provenance` | Relative input filenames and SHA-256 hashes, timing, initialization, gravity and paired noise. Large stream seeds are decimal strings to preserve uint64 precision in JavaScript. |
| `scenarios.<name>.source_sample_count` | Original endpoint count, including initialization: 3,001 here. |
| `scenarios.<name>.disturbance` | Null for nominal; start/end times, body axis and injected acceleration otherwise. End time is exclusive. |
| `scenarios.<name>.metrics` | Original full-run angle RMSE and signed final bias error. |
| `scenarios.<name>.rows` | 601 display endpoints per selected run, including 0, 12, 17 and 30 s. |

The exporter checks required fields, finite values, increasing and exactly aligned
timestamps, the source schedule, paired truth/gyro series, and agreement between
full-resolution records and summary RMSE/final bias metrics before writing. It
also compares bias/motion truth and initial states against the declared
configuration. Metric agreement uses `1e-10` absolute and relative tolerance in
degrees or degrees/second
to allow arithmetic ordering differences. Source paths remain relative to the run;
the artifact contains no machine directory or wall-clock generation timestamp.
SHA-256 fingerprints identify source bytes; they are not authenticity signatures.

Decimation preserves the initial/final endpoints and the first endpoint at or after
each pulse boundary, including when another stride is requested. It is a display
reduction, without an anti-alias filter. The browser linearly interpolates between
those retained records for the cursor and drone. Short transients and correction
jumps can be smoothed or missed. RMSE is retained from **all 3,001 original
endpoints**, using unwrapped angle errors; it is not computed from displayed points.
This contract is specific to the paired experiment, not a general sensor format.

## Implementation and checks

| Location | Responsibility |
| --- | --- |
| `src/meridian/web_export.py` | Validate existing run artifacts, convert display units and export deterministic JSON. |
| `web/src/data.js` | Browser contract checks, display interpolation and playback time arithmetic. |
| `web/src/chart.js` | D3 scales, paths, shared cursor and pointer inspection. |
| `web/src/main.js` | Load data and connect scenario selection, playback, legends and readings. |
| `web/src/style.css`, `web/index.html` | Appearance, semantic structure and responsive layout. |

JavaScript ES modules keep the presentation small. [Vite](https://vite.dev/guide/)
handles local development and the static build; [D3](https://d3js.org/getting-started)
provides scales, axes and SVG paths. Versions are pinned in `web/package-lock.json`.
Fonts are packaged with the application. Runtime dependency notices are included
in `web/public/third-party-notices.txt` and copied into the build.

```sh
python -m pytest -q tests/test_web_export.py tests/test_ekf_experiment.py
npm --prefix web test
npm --prefix web run build
```

Python checks cover the exporter against short paired experiments, input rejection,
units, provenance, metric preservation and deterministic output. JavaScript checks
cover the shipped artifact against the public summary, malformed records,
interpolation and playback boundaries. Source review and local HTTP checks
complement these tests. They do not constitute automated browser interaction,
cross-device rendering or accessibility validation.

Only the two selected Python simulation runs are displayed. There is no live
hardware connection, real-log browser replay, parameter tuning, C++ execution in
the browser, or statistical-consistency view. The similar nominal KF/EKF RMSE
does not imply superiority; the translation case demonstrates a shared physical
model limitation. New documented bench acquisition and real EKF replay remain
separate work.
