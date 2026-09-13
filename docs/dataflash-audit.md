# DataFlash IMU audit

The offline audit extracts logged IMU snapshots and checks their units, timing,
sensor instances, and available health metadata before estimator replay. It
preserves the original source and never connects to a flight controller.
It does not estimate angle error, tune sensor noise, or certify bench conditions.

## Reproduce an audit

Use the Python 3.12 environment from the README, then install the optional reader:

```sh
python -m pip install -r requirements-dataflash.lock -e '.[dataflash]'
python -m meridian.dataflash_audit /path/to/recording.bin --output outputs/dataflash-audit
python -m pytest -q
```

`pymavlink` is optional; simulation and numerical cores do not depend on it.
The additional lock pins the tested decoder and its dependencies. Reader tests
construct small DataFlash binary fixtures, with format and unit definitions;
they require the optional dependency and are skipped if it is absent. No private
recording is required to run the test suite.

Each run requires a new output directory and writes:

| File | Contents |
| --- | --- |
| `imu.csv` | IMU observations in file order, original integer microseconds, instance and segment IDs, gyro rad/s and specific force m/s². |
| `summary.json` | Source SHA-256 and size, decoder/environment versions, checked format, selected historical parameters, arming events, health counters, timing and descriptive statistics. |
| `overview.png` | Three-axis gyro/accel, 3D force magnitude and principal accelerometer tilt; curves break at segment boundaries. |

Source filenames, absolute paths, board serial numbers, GPS, arbitrary text
messages, and complete parameter dumps are not exported. Selected firmware
version banners are retained. Exported measurements still require review before
publication. Generated runs under `outputs/` remain ignored; no real recording is
bundled with this repository.

## Input contract

The reader supports binary DataFlash `IMU` messages with `TimeUS`, `I`, `GyrX/Y/Z`
and `AccX/Y/Z`. It checks numeric formats and the recording's `FMTU`, `UNIT` and
`MULT` definitions: timestamps must represent microseconds, gyro rad/s, and
acceleration m/s². Missing or conflicting definitions and changing IMU schemas
are refused. Unsupported logs need an explicit adapter change, rather than an
assumed unit conversion. Separate `ACC`/`GYR` and high-rate `ISBH`/`ISBD` samples
are not imported; batch headers are counted only.

The [ArduPilot IMU field reference](https://ardupilot.org/copter/docs/logmessages.html#imu)
defines fields and units. `GHz` and `AHz` describe sensor measurement rates;
compute the observed logging cadence from `TimeUS` differences. Inspect the
recorded firmware's logging implementation before interpreting snapshot timing,
calibration, filtering, or sensor orientation. The audit cannot infer those
semantics from field names alone. In the inspected historical Copter revision,
IMU values were calibrated and filtered frontend outputs, with logger timestamps.
They were not interval-average rates like Meridian's simulated input. A common
message timestamp does not establish simultaneous physical sampling or matched
gyro/accelerometer filter delays.

The reader retains observations instead of sorting, merging instances,
interpolating, subtracting a fitted bias, or reapplying board calibration.
SHA-256 identifies the bytes inspected; it does not authenticate acquisition
conditions or prove that the download was complete. Parser diagnostic counts and
unparsed trailing-byte counts remain in the report. Zero diagnostics do not
certify an undamaged recording. The decoder can recover records around damaged
bytes, so its output is an audit input, not a validated replay dataset.

## Timing and descriptive statistics

Timing is analyzed separately for each IMU instance in original file order.
Duplicate and backward intervals are counted. A new segment begins after a
nonfinite sensor record, a nonpositive interval, or an interval strictly greater
than the configurable gap threshold, **0.2 s by default**:

```sh
python -m meridian.dataflash_audit /path/to/recording.bin \
  --gap-threshold-s 0.1 --output outputs/dataflash-audit-stricter
```

This default is an inspection choice for roughly 25 Hz logs, not a sensor timing
specification or an estimator acceptance limit. Shorter missing intervals can
remain within a segment. All original timestamps remain available in the CSV.
Segment IDs are local to each instance; combine `instance` and `segment_id`.
Nonfinite records retain their CSV row and get an empty segment ID. No integration
occurs across any interval. `within_segment_duration_s` sums endpoint durations;
it is not a percentage of recovered physical samples.

Component means and population standard deviations use finite values, without
time weighting. They summarize observed signals including motion, filtering,
drift, and calibration effects; they are not identified bias or white-noise
parameters. Accelerometer tilt is `atan2(-f_y, -f_z)` under the candidate FRD body
convention. Its principal-angle minimum/maximum and count are reported, with no
arithmetic angle mean/std across the branch cut. Plot lines also break across
±180°. The numerical near-zero `y/z` guard is not a physical validity test.

`GH/AH` and `EG/EA`, when available, are summarized as logged health/error
observations. Finite timing segments do not certify healthy sensors. Arming
events are reported without reconstructing an assumed initial state. No `ARM`
or arming-related `EV` record does not establish that the recording was disarmed.
Historical logging parameters do not identify the controller's current settings.

## Next step toward replay

Associate a selected recording with an acquisition note: hardware and firmware,
disarmed state, propellers removed, mounting/axes, static poses or prescribed
roll motion, clock behavior, and any known interruptions. Read back the current
controller configuration for a new acquisition. Keep Meridian outside the
control loop.

Choose contiguous intervals and explicitly define how logged rate snapshots
approximate interval motion before invoking an estimator. Initialization,
calibration, residual noise, delay assumptions, and behavior at gaps require
their own replay configuration. ArduPilot attitude can provide an estimate for
comparison, not independent ground truth. Use a static angle reference with
stated uncertainty where possible. Without it, report agreement and repeatability
instead of accuracy. Current audit plots are measurement inspection, not filter
replay or embedded/flight validation.
