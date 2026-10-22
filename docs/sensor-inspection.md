# Sensor inspection before estimation

An owned acquisition can be inspected without running an estimator. The Data
view links camera previews, all six IMU components, sample timing and independent
reference support to the original stream, segment and sample identities.
Inspection describes the available observations. It does not calibrate sensors,
synchronize clocks, establish VIO compatibility or produce trajectory errors.

## Evidence and availability

The isolated import child generates an immutable inspection alongside the
validated acquisition and its prepared snapshot. Publication, deletion, storage
accounting and backup use the same manifest and ownership boundary as the
[acquisition workflow](hosted-acquisitions.md). Image decoding remains outside
HTTP; the API serves verified derived JSON and generated grayscale previews.
The API image does not require Pillow or NumPy.

The interface distinguishes:

| Evidence | Meaning |
| --- | --- |
| Import validity | The supported package passed its structural and payload checks. |
| Inspection availability | This immutable version has a complete inspection artifact. |
| Calibration suitability | Not assessed by this method. |
| Estimator compatibility | Not assessed by inspection; [private-input VIO](private-vio.md) checks compatibility separately. |
| Reference support | Measured separately at camera timestamps, when clocks are comparable. |
| Estimate support | Not generated; an input inspection has no estimator output. |

Missing reference data do not invalidate sensor observations. Conversely,
present reference files do not imply complete evaluation coverage. The current
hosted admission profile still requires a supported preparation from increment
2.2; this inspection does not extend it to arbitrary camera-only or unsupported
stereo packages.

Existing acquisitions published before inspection was introduced remain
unchanged and report `not_generated`. Reimporting the original archive creates
a new inspected version. A known inspection resource-limit failure preserves
the valid acquisition and prepared snapshot, discards partial inspection files
and reports `inspection_resource_limit`. Corrupt inputs or unexpected failures
are not silently converted into available inspection.

## Time, intervals and discontinuities

Source timestamps are converted to integer nanoseconds within their own declared
clock domain. Absolute timestamps and sample IDs remain decimal strings in JSON;
the browser computes relative offsets without first converting an epoch to a
floating-point number. Original tick units, timestamp meaning, frames and
processing declarations remain visible.

Statistics are calculated on full-resolution observations inside one segment.
The reported average rate is `(sample_count - 1) / elapsed_seconds`, including
observed gaps. It is not a configured nominal sensor rate. Interval quantiles
use nearest ranks, and a cadence gap is an interval strictly greater than three
times the full segment's median interval for cameras and IMUs. Reference gaps
use the fixed 20 ms support limit instead, so every unsupported reference
bracket is visible independently of the recording's median cadence. The
reported `gap_policy` identifies the rule. The camera/IMU rule is a diagnostic
heuristic, not a sensor specification or proof that samples were lost.

Gap reports retain both endpoint timestamps and sample IDs. Adjacent declared
segments have a separate boundary classification; intervals never cross a reset.
Within-segment duplicate and backward timestamps are zero for accepted packages
because the importer already rejects those faults. Declared reset segments can
still be inspected separately; no repair, sorting or timestamp fitting occurs.

Cross-stream comparisons require the same clock ID and an unambiguous timeline.
Matching epoch labels or overlapping numerical timestamps do not establish
synchronization. Independent reset eras are not paired by segment name or
position. Explicit exact stereo pairing can use the validated declaration's
segment correspondence. Otherwise an ambiguous relationship is reported as
`not_comparable`.

## Sensor and reference support

For a declared stereo pair, exact pairing remains exact. The inspection method
also describes deterministic mutual unique nearest matching within an explicit
5 ms tolerance where the source declaration permits it; ties and unmatched
observations stay unmatched. It does not invent a stereo pair when none is
declared.

At each camera observation, IMU support requires an exact sample or an enclosing
same-segment bracket that is not a detected cadence gap. Reference support
requires an exact pose or an enclosing same-segment bracket no wider than
20 ms. The latter matches the unchanged historical room2 reference-support
audit. Supported, gap, outside-range and non-comparable outcomes stay distinct.
Sensor support, reference support and absent estimate support have separate
counts; none is substituted for another.

Room2 is an already-consumed regression recording. Inspecting its inputs does
not reopen a reserved evaluation or change its historical coverage failure.
No fitting, estimator execution, alignment or error scoring is performed here.

## IMU and image indicators

IMU angular velocity is shown in rad/s and specific force in m/s², with the
declared source frame. Unit conversion does not rotate axes, remove gravity,
apply bias/scale calibration or undo previous filtering. Component ranges,
variation and vector-norm evidence describe the observations; low variation
does not prove physical stationarity or a useful calibration recording.

Saturation is unknown because the current acquisition contract declares no
sensor measurement ranges. Large values cannot be labelled saturated without
those limits. Image brightness, endpoint fractions and edge contrast are
explicit preview-scale indicators. Dark or bright pixels are not a universal
exposure verdict, and edge contrast is not a calibrated physical blur metric.
The generated mono8 previews are bounded displays; the source image bytes and
their exact identities remain available separately.

## Display and resource bounds

The summary is at most 1 MiB. Stream chunks contain at most 256 observations and
are individually bounded to 1 MiB. Preview PNGs have a maximum 320-pixel edge
and 128 KiB file size. Inspection has a 512 MiB ceiling **inside** the existing
6 GiB result budget and 59,998-file ceiling; these maxima are not additional
storage reservations and need not all be attainable together.

Window checks read the original-resolution derived observations before display
reduction. A plot contains at most 1,200 points and preserves discontinuity
endpoints, support transitions and IMU extrema. When mandatory boundaries alone
exceed the display budget, the interface requests a narrower interval instead
of drawing a misleading continuous curve. A bounded list of reported gaps can
be explicitly truncated while its full count and interval statistics remain
available. Window JSON is capped at 2 MiB.

The API resolves the owning project and acquisition before every summary,
window or preview access. Derived files are selected through the immutable
publication manifest and hash-verified within their size bounds. No user-supplied
path is accepted as an image selector. Deletion and missing files produce
explicit unavailable states; an old descriptor alone does not make data readable.

## Reproduce synthetic cases

Generate a valid package with camera/IMU gaps and partial reference support:

```sh
mkdir -p outputs/inspection
python scripts/make-inspection-fixture.py outputs/inspection/gaps.zip
```

The fixture has 17 stereo pairs, 361 IMU samples and 70 reference poses. Its
reference supports 13 of the 17 camera epochs under the fixed 20 ms rule. The
IMU gap crosses a derived chunk boundary. `--no-reference`, `--reset` and
`--foreign-clock` create separate explicit cases. All images and measurements
are analytic synthetic data, including deliberately dark, bright and flat
patterns; none is a physical acquisition.

Upload the package through the project's Data view, wait for publication and
open **Inspect sensor data**. Select streams and segments, narrow the interval
and inspect the exact observations behind the reported gaps and support.
The source archive can be downloaded and its SHA-256 checked independently.

For a validated local acquisition, generate the same immutable evidence without
a server or estimator:

```sh
meridian-sensor-inspect path/to/acquisition outputs/inspection/local
```

The output directory must not already exist and must be outside the acquisition.

The [recorded regression and qualification evidence](../results/sensor-inspection/README.md)
separates synthetic checks, the consumed room2 inputs and operational verification.
