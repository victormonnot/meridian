# Interface screenshots

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
