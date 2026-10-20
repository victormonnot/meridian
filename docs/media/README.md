# Interface screenshots

## Independent experiment exports

[`exports.png`](exports.png) captures the deployed HTTPS application on
2026-10-20 in Chromium at a 1440 × 1200 viewport, light theme, scrolled to
Independent reproduction. Release `20261020-16ef0733a9c8` ran the actual
100-point room1 configuration through the queue, isolated OpenVINS runtime and
separate evaluator. The view shows its metrics and the verified 681,618-byte
download. No pixels, values or execution states were edited. The account,
project and execution are disposable QA resources.

To reproduce, run a saved 100-point configuration, open its completed Results
entry, scroll to **Independent reproduction** and select **Download experiment**.
See the [export contract](../vio-exports.md) and
[validation report](../../results/exports/README.md).

The numerical results derive from the **TUM VI Benchmark**, David Schubert,
Thore Goll, Nikolaus Demmel, Vladyslav Usenko, Joerg Stueckler and Daniel Cremers
(2018), under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), using
the [documented preparation and evaluation](../vio-reference.md). This view
contains derived metrics, not camera imagery or drone-flight validation.

## Saved VIO comparisons

[`comparisons.png`](comparisons.png) captures the deployed HTTPS application on
2026-10-19 in Chromium at a 1440 × 1400 viewport, light theme, scrolled to
Configuration difference. Release `20261019-0169017b1bec` executed the actual
200-point baseline and 100-point variant through the private queue, native
OpenVINS runtime and separate evaluator. The view shows their full-recording
own-support and common-support scores. No pixels, values or execution states
were edited; the account and project are disposable QA resources.

To reproduce, run both saved configurations, open **Results → New comparison**,
select their exact execution identities, save and scroll to Configuration
difference. The snapshot retains the original full-recording alignments and
does not rank the configurations. See the [comparison contract](../vio-comparisons.md)
and [validation report](../../results/comparisons/README.md).

The numerical results derive from the **TUM VI Benchmark**, David Schubert,
Thore Goll, Nikolaus Demmel, Vladyslav Usenko, Joerg Stueckler and Daniel Cremers
(2018), under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), using
the [documented preparation and evaluation](../vio-reference.md). This view
contains derived metrics, not camera imagery or drone-flight validation.

## Synchronized VIO investigation

[`investigation.png`](investigation.png) captures the deployed HTTPS application
on 2026-10-18 in Chromium at a 1440 × 1400 viewport, light theme, scrolled to the
shared timeline. Release `20261018-8230e773705d` ran a new baseline through the
real queue, native OpenVINS container and independent evaluator. The selected
camera epoch is 1010 at 50.502 seconds; metrics use the full recording. The
fictional QA account and its private project were created for this check and
removed afterward. No pixels, values or execution states were edited.

To reproduce, launch a saved 200-point baseline and open its investigation,
select frame 1010, keep the full interval and scroll to the timeline. The view
continues below the viewport with error, support, attitude and IMU plots. See the
[investigation contract](../vio-investigation.md) and
[validation report](../../results/investigation/README.md).

Camera imagery is from the **TUM VI Benchmark**, David Schubert, Thore Goll,
Nikolaus Demmel, Vladyslav Usenko, Joerg Stueckler and Daniel Cremers (2018),
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Original mono16
PNG frames are displayed by Chromium without Meridian pixel transformations.
This is a handheld benchmark execution, not drone-flight validation.

## Hosted VIO execution

[`jobs.png`](jobs.png) captures the deployed HTTPS application on 2026-10-18 in
Chromium, with a 1440 × 1000 viewport, light theme and a full-page capture. The
fictional QA account owns `Hosted VIO validation` and `Room1 baseline`. The
200-point version ran through the real PostgreSQL queue, isolated OpenVINS runtime
and separate evaluator. The displayed metrics come from the published job;
no pixels, values or job states were edited. The exact QA account and its data
are disposable; their identifiers in the image do not identify a real engineer.

To reproduce, create a private project and experiment on the provisioned
workspace, launch the saved 200-point version and open its completed Results
entry. See the [job contract](../hosted-vio-jobs.md) and
[validation report](../../results/jobs/README.md). This capture documents the
observed reference execution, not drone-flight performance.

## Versioned experiments

[`experiments.png`](experiments.png) captures the real local application on
2026-10-17 in Chromium, with a 1440 × 1000 viewport, light theme and a full-page
capture. The fictional account `vio-reviewer` owns `Indoor VIO validation` and
the `Stereo feature budget` experiment. Versions one and two were saved through
the API with budgets 200 and 400. The comparison shows their exact parameter
difference. The page was reloaded before capture; no image pixels or values were
edited, and no estimator execution or result is represented.

To reproduce, create a private project and experiment, save a different supported
feature budget, and compare versions one and two. See the
[experiment contract](../versioned-experiments.md). This capture documents the
interface, not numerical performance or hosted availability.

## Private project workspace

[`workspace.png`](workspace.png) captures the real local application on
2026-10-17 in Chromium, with a 1440 × 1000 viewport and a full-page capture.
The dark-theme view uses a fictional account (`vio-engineer`) and project
(`Indoor VIO validation`) created through the application. The recording metadata,
calibration conventions and reference coverage come from the qualified TUM-VI
catalog. No project experiment or result is represented, and no pixels or values
were edited.

To reproduce, start the [workspace](../hosted-workspace.md), create an account
and a project using TUM-VI room1, then open its Data view. This capture documents
the interface; it does not independently establish hosted service availability.

## Original simulation explorer

[`explorer.png`](explorer.png) is a capture of Meridian's web explorer on
2026-10-10, using Chromium at a 1440 × 900 desktop viewport, normal browser zoom
and the dark theme. The capture shows the Nominal simulation at 8 seconds with
Vector EKF selected. The page continues below the visible area.

The interface reads the repository's saved simulation results. This is not a
live sensor feed or a flight recording. No values, curves or interface elements
were changed for the image.

To reproduce the view, follow the [explorer guide](../web-explorer.md), choose
Nominal and Vector EKF, leave playback paused at 8 seconds and scroll to the top.
The [comparison report](../../results/ekf-comparison/README.md) describes the
underlying experiment.
