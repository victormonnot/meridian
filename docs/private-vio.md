# VIO on private prepared acquisitions

The `openvins-private-stereo-v1` execution profile binds an owned, immutable
[input revision](sensor-calibration.md) to an experiment and a durable job.
The existing `openvins-tumvi-room1-v1` profile retains its original input pins,
configuration, online calibration policy and evaluation thresholds.

## Input identity and admission

A private experiment selects an exact prepared revision and a camera interval
within one reset segment. The saved specification records the acquisition,
published preparation, calibration and clock identities, stream selection and
integer nanosecond bounds. A later artifact or preparation cannot change an
existing experiment revision or job.

Admission checks the supported sensor model separately from import validity:

- Two synchronized global-shutter cameras with exposure-midpoint timestamps,
  exact timestamp pairs, supported monochrome PNG encoding and matching supplied
  pinhole calibration. Camera calibration processing must be declared
  `not_applied`; an unspecified rectification model is not inferred.
- One instantaneous IMU stream in rad/s and m/s², with known calibration
  processing, comparable prepared sample timestamps and enough samples to
  bracket the selected camera observations.
- Directed transforms from the prepared IMU frame into each camera, supported
  distortion coefficients and positive continuous IMU noise parameters.
- Retained, owned prepared files whose identities agree with the frozen revision.

Unsupported semantics, unresolved clock relationships, reset crossings,
incompatible geometry or missing files produce admission reasons. DataFlash
snapshots are not relabeled as instantaneous IMU measurements. A reference
trajectory is optional and is never an initialization input.

The API checks the bounded camera/IMU CSV timelines without decoding images.
Before execution, the worker verifies the complete retained publication and
stages the selected observations. Each prepared camera index is limited to
10,000 rows and each IMU/reference index to 100,000 rows before interval
selection. Camera dimensions are limited to 1,048,576 declared pixels; each
selected PNG must be at most 2 MiB.

## Browser selection and API

Create an experiment, select **Private prepared input**, choose a retained
revision and check compatibility. The interval uses exact decimal nanosecond
strings; changing the input, feature budget or interval requires a new check.
Saving freezes the assessed specification. Running still submits the saved
experiment version through the existing job endpoint.

| Route under `/api/v1/projects/{project}` | Behavior |
| --- | --- |
| `GET /execution-inputs` | List owned ready input revisions and the private profile. |
| `POST /execution-inputs/assess` | Check `{schema_version: 1, input_revision_id, parameters?, interval?}` and return explicit eligibility reasons or a frozen specification. |

The interval is `{segment_id, first_ns, last_ns}` with inclusive, exact camera
sample epochs in nonnegative integer nanoseconds. Both stereo cameras and the
IMU must use one matching reset segment and the same prepared clock.
An eligible check is neither a physical calibration certificate nor a guaranteed
successful initialization. Admission runs again when saving and queuing; the
worker verifies identities and payloads before every attempt.

## Fixed supplied calibration

Private execution disables online refinement of camera intrinsics, camera–IMU
extrinsics and time offset. Any supported measurement or time correction is
already represented by the selected preparation; the initial runtime time
offset is zero. The native estimator still estimates its navigation state and
IMU biases. Its internal states are not exported as a new calibration artifact.

The profile uses the maintained OpenVINS revision, adapter and bounded runtime.
It generates configuration from validated structured fields, not uploaded YAML.
The generic profile disables the room1 image masks and declares its fixed gravity
magnitude of 9.80766 m/s². These are profile assumptions, not measurements of an
arbitrary user's rig or location. Supplied isotropic continuous noise parameters
describe the prepared measurements; no automatic covariance propagation or
noise identification is performed. A preparation that applies nonidentity IMU
gains is therefore refused by this initial isotropic-noise profile. Supported
bias subtraction and direct IMU rotation remain explicit preparation operations.

Stereo pairing, initialization, IMU bracketing and the end-of-stream policy are
explicit in the saved specification. Integer timestamps are retained until the
native adapter subtracts the sensor origin and converts relative time to seconds.
No wall-clock pacing, interpolation of missing camera observations or automatic
time-offset fit is introduced.

## Estimation and optional evaluation

Successful estimation means the maintained native execution completed with valid
state output. It does not establish trajectory accuracy. Reference availability
and evaluation support are reported separately.

A comparable reference has one segment, the prepared IMU body frame and the
same prepared clock; unsupported frame, clock or segment relationships remain
explicitly unavailable. With sufficient matched poses, evaluation uses the
existing global SE(3) alignment with scale fixed to one. Interpolation is bounded
by the declared reference gap limit, without reference extrapolation or fitting
a time offset. Metrics describe only the evaluated support; partial coverage
remains visible.

Without a usable reference, accuracy metrics and accuracy qualification remain
unavailable. Investigation displays the raw estimator trajectory and attitude
in its own local frame, with IMU data, camera images and execution events. It
does not overlay an unaligned reference or fill missing errors with zeros.
An invalid declared reference payload is an integrity failure, not evidence of
reference absence.

## Execution, retention and access

Private jobs reuse the global compute slot, bounded retries, attempt fencing,
cancellation and atomic result publication. The trusted worker resolves the
owned revision. Its native child receives the selected sensor CSVs, camera
image directories from that exact revision and generated configuration. The
image directories can include observations outside the selected interval;
the staged CSVs select which images are consumed. Evaluation references, database
credentials and the Docker socket are excluded. Evaluation runs separately.

Queued and active jobs protect their required revision files against deletion.
Completed job results remain retained under the existing job policy. Deleting
input files makes future execution and associated image reads unavailable while
preserving the saved result metadata and retained investigation evidence.
Every image route resolves the job's exact owned input and publication inventory.

Backup format 3 retains private input publications, job metadata, the exact job
dependency and published result files together. Recovery checks must cover both
reference-backed and reference-free results. This remains an operator-run backup
and restoration workflow, without a scheduled backup service.

Calibration comparisons and complete portable private-input exports remain
separate increments. Private jobs expose that boundary explicitly; they cannot
use a room1 export recipe that reacquires unrelated public inputs.

## Reproducible regression input

The [fixture generator](../scripts/make-private-vio-fixture.py) packages the
predeclared first 30 seconds of the already consumed room1 recording. It verifies
the pinned original archive, retains selected image bytes and decimal sensor
observations, and includes adjacent IMU/reference samples for bracketing:

```sh
python scripts/make-private-vio-fixture.py imported-room1 new-private-vio-fixture
```

Upload `room1-first30s.zip`, import `calibration.json`, then prepare two revisions
with the same supplied calibration and reference inclusion enabled/disabled.
The generator's manifest records the original recording identity, exact crop,
package and calibration hashes, and expected observation counts. Removing the
reference is a contract test on the same recording, not an independent accuracy
trial. No new reserved recording or physical acquisition is required by this
regression; physical bench and flight validation remain unclaimed.

The [recorded qualification](../results/private-vio/README.md) reports actual local
and HTTPS runs, optional-reference behavior, fault checks and isolated backup
restoration, together with the observed failures and limits.
