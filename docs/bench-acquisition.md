# Prepare a roll bench recording

This procedure prepares the documented acquisition still needed for Meridian's
single-axis study. It is a plan, not a completed experiment or a hardware
validation. The drone stays **disarmed, with propellers removed**, and Meridian
runs offline, outside the flight-control loop.

Copy the [session template](templates/bench-session.md) into a new directory under
`outputs/`, for example `outputs/bench-session/session.md`. Fill it as the session
progresses. Keep the original recording, configuration read-back and reference
notes there or in another local archive; `outputs/` is ignored by Git. Use paths
relative to the session record where possible. Review files before any public
inclusion.

## Before recording

1. Record the controller, IMU mounting, firmware version and current configuration.
   Save a configuration read-back, including logging, sensor orientation,
   calibration and filtering settings. Historical parameters do not establish the
   current setup. This guide does not prescribe firmware parameter values.
2. Make a short disarmed test recording and run the [DataFlash audit](dataflash-audit.md).
   Confirm that it contains supported `IMU` snapshots, unit definitions and usable
   timestamps. Separate `ACC`/`GYR` streams and high-rate batches are not supported
   by the current reader. Record parser diagnostics, trailing bytes, sensor health
   information and any interruptions. Missing arming events do not prove a
   disarmed session; record the observed state directly.
3. Check the body-frame convention with known static poses. Meridian assumes
   forward-right-down axes, positive right-hand roll about body x, and
   `theta_acc = atan2(-f_y, -f_z)`. Under gravity alone, level gives approximately
   `[f_y, f_z] = [0, -g]`; a small positive roll gives negative `f_y`. Record how
   fixture angles map to these axes and signs. The replay does not rotate or
   recalibrate the measurements.
4. Choose an independent static angle reference, such as a measured fixture or
   separate inclinometer. Record its zero, alignment, resolution, uncertainty and
   the basis for that uncertainty. Resolution alone is not an accuracy bound.
   Accelerometer tilt and ArduPilot attitude are not independent references for
   this comparison. If no reference is available, label the session as an
   agreement/repeatability study.

## Define the sequence, then acquire it

Before viewing filter outputs, write down the intended static poses, their order,
hold durations, repetitions and the rule for excluding settling periods. Include
a return to the starting pose to inspect repeatability. Choose angles and durations
that the fixture and reference can support; this guide sets no accuracy threshold
or universal pose values.

Record the actual reference reading at each hold, including deviations from the
plan. Between holds, use slow, predominantly roll motion with pauses and minimize
translation and off-axis motion. Rotation about a point away from the IMU can
also add acceleration. A force magnitude near `g` does not prove that the gravity
model is valid. Without a synchronized independent dynamic reference, the moving
parts cannot establish angle accuracy.

Record how each hold is associated with the log clock, for example through a
documented event or a synchronized observation. Preserve integer `TimeUS` bounds
in microseconds, the IMU instance and the timing uncertainty. Wall-clock notes
alone are not a mapping to `TimeUS`. If the correspondence is uncertain, mark it
as unresolved instead of choosing windows from the smallest filter errors.

## Audit and replay

Use the Python environment and optional decoder from the [audit guide](dataflash-audit.md).
For a session directory containing a local `recording.bin`, start with:

```sh
python -m meridian.dataflash_audit outputs/bench-session/recording.bin \
  --gap-threshold-s 0.2 --output outputs/bench-session/audit
```

Inspect `summary.json` and `imu.csv` before selecting an instance and segment.
Record the source SHA-256, the selection and its reason, including excluded
segments and unsuccessful attempts. Choose from acquisition conditions, timing
and health evidence before comparing estimator outcomes. A finite segment alone
does not certify healthy sensors or a valid gravity model.

Then run the [offline replay](imu-replay.md), replacing the example IDs with those
selected from the audit:

```sh
python -m meridian.imu_replay outputs/bench-session/recording.bin \
  --instance 0 --segment 0 --max-gap-s 0.2 \
  --output outputs/bench-session/replay
```

Audit and replay must use the same gap threshold; changing it can change segment
IDs. Both commands require new output directories. Record the command and all
tuning values before interpreting the result. Existing defaults are illustrative,
not measured sensor noise. Preserve the first result if later runs change tuning.

## What the session can establish

The replay selects a whole audited segment and initializes from its first tilt.
It does not ingest fixture angles, select static pose windows or compute accuracy
against an external reference. Its reported disagreement with accelerometer tilt
is not reference-angle error.

For a separate static comparison, retain each reference reading and uncertainty,
the corresponding integer timestamp window and its inclusion rule. Do not bridge
segment boundaries or treat settling/motion as a static hold. Report all planned
poses and repeats, including failed or unusable intervals and their reasons.
Record the calculation method if an external analysis is added; do not present
it as an existing Meridian output.

Keep unknown timing, calibration, noise and reference limitations explicit.
This acquisition can support a documented offline bench study; it does not
establish onboard, real-time or flight performance.
