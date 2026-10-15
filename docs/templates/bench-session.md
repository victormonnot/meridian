# Bench session record

Copy this blank form to a new local session directory under `outputs/`.
Replace placeholders with observations; use **unknown** or **not available**
instead of guessing. This template is not evidence that a recording took place.

## Session and setup

- Session ID, date, local time and timezone:
- Objective: referenced static comparison / agreement and repeatability:
- Controller and IMU mounting / body-axis mapping:
- Firmware version and configuration read-back file:
- Logging stream, orientation, calibration and filtering settings:
- Disarmed state observed before and during recording:
- Propellers removed:
- Meridian offline and outside the control loop:
- Power, support, environment and known interruptions:
- Static pose checks of sensor axes and roll sign:

## Independent reference

- Instrument or fixture and independence from the onboard IMU/attitude:
- Zero definition and alignment to the body roll axis:
- Resolution (degrees):
- Reference uncertainty (degrees), meaning and justification:
- Mounting/repositioning uncertainty and any unquantified sources:
- Reference evidence file(s), or reason no reference is available:

## Plan fixed before inspecting estimator results

- Intended pose sequence and repetitions:
- Hold durations and settling exclusion rule:
- Slow-motion sequence between holds:
- Method for mapping observed holds to the log clock:
- Inclusion/exclusion rules and any predeclared evaluation criteria:
- Planned replay tuning and reason (defaults are illustrative):

| Planned hold / repeat | Target roll (deg) | Hold duration (s) | Settling rule |
| --- | --- | --- | --- |
| Fill before acquisition | | | |

## Actual observations and clock mapping

Use the verified body-frame roll sign. `TimeUS` bounds are original integer
microseconds, not wall-clock times or elapsed seconds. Record mapping evidence
and timing uncertainty; leave bounds unresolved if the mapping is unknown.
Reference windows are notes for separate analysis, not replay CLI inputs.

| Hold / repeat | Reference roll (deg) | Reference uncertainty (deg) | IMU instance / segment | Start TimeUS (us) | End TimeUS (us) | Mapping evidence / timing uncertainty |
| --- | --- | --- | --- | --- | --- | --- |
| Fill from observations | | | | | | |

- Motion, off-axis movement, translation and deviations from the plan:
- Failed recordings, unusable holds and exclusions with reasons:

## Source and audit

- Original recording path (relative to this record if possible):
- Source SHA-256 and byte size from the audit:
- Meridian Git revision and any local code changes:
- Python / decoder versions and environment record:
- Exact audit command, output directory and gap threshold (seconds):
- Supported IMU schema and recorded units checked:
- Instances, cadence, duplicate/backward timestamps and gaps:
- Parser diagnostics, trailing bytes, health and arming observations:
- Selected instance / segment and reason, before filter comparison:
- Audit summary and measurement CSV paths:

## Replay and conclusions

- Exact replay command and new output directory:
- Gap threshold matching the audit (seconds):
- Gyro noise std (deg/s), angle noise std (deg), initial bias std (deg/s), complementary tau (s):
- Replay summary path (includes initialization, derived EKF noise and artifact hashes):
- Static analysis method/output, if performed separately:
- Findings across all planned holds and repeats:
- Agreement-only findings where no independent reference exists:
- Unresolved reference, timing, calibration and noise limitations:
- Next action and reason:

The replay's tilt-disagreement statistics are not independent angle accuracy.
Keep this completed record and source files local until reviewed for publication.
