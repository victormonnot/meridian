# Synchronized VIO investigation

The investigation view connects one retained execution to its camera images,
input IMU, estimated and reference poses, error samples and coverage. It consumes
evidence from that job's published attempt. It does not substitute qualification
results for an account's own execution.

## Time, frames and support

The common timeline uses the prepared recording's sensor timestamps. Absolute
nanoseconds cross the JSON boundary as decimal strings; relative seconds are
computed after integer subtraction. No clock offset is fitted. Camera frames use
the exact stereo schedule. IMU samples retain their original timestamps and
measurements without interpolation, resampling or estimated bias correction.
Displaying an input sample does not assert that the estimator processed it.

Each expected camera epoch records whether an estimate exists, whether the
reference supports association, and whether a valid error can be evaluated.
Initialization, interior gaps, trailing gaps and unsupported reference intervals
remain visible. Missing output is not diagnosed as a tracking failure: internal
tracking status is not exported by the maintained native adapter.
The reported initialization delay is the first estimated camera epoch minus the
first expected epoch. The separate native initialization event records the
estimator-reported startup timestamp and can precede the first exported camera
pose; these are distinct observations.

Poses rotate the IMU body frame into a world frame using Hamilton `xyzw` unit
quaternions. Raw estimates retain the estimator's local world; aligned estimates
use the independent reference world. Positions are in metres. Roll, pitch and
yaw are display quantities in degrees using `Rz(yaw) Ry(pitch) Rx(roll)` principal
values; they can wrap and are ambiguous at gimbal lock. Quaternion orientation
error remains the numerical metric. The view does not compare unaligned worlds
as if they were the same coordinate frame.

The two camera views serve original 512 × 512, 16-bit grayscale PNG files from
the prepared recording. Meridian does not rescale their pixels or annotate them
with invented feature tracks. Image file hashes are checked before delivery.
The dataset's attribution and license accompany the investigation provenance.

## Full-recording and interval scores

The [qualified evaluator](vio-reference.md) remains unchanged. Reference positions
use linear interpolation and orientations use shortest-arc SLERP, with a maximum
20 ms reference bracket and no extrapolation. An estimate must associate with a
distinct camera epoch within 1,000 ns. Estimates are not interpolated or filled
across gaps.

One global SE(3) alignment is fitted over all evaluated positions in the complete
recording, with no scale or time fit. It removes constant world rotation and
translation, including constant roll and pitch gauge. At least three non-collinear
associated positions are required; insufficient or degenerate support leaves
alignment and metrics unavailable.

Selecting a closed interval `[start_ns, end_ns]` reuses this same global alignment.
It does **not** fit a new alignment to the selection. Interval position RMSE,
mean and maximum error, and orientation RMSE, mean and maximum error use every
evaluated camera epoch inside those bounds, with equal sample weights.
Coverage uses all expected camera epochs in that interval, including missing
estimates and unsupported reference. Empty intervals and intervals with no
evaluated support have explicit statuses and null scores.
The interface snaps entered seconds to the nearest camera epochs and displays
their exact selected bounds. The API also accepts arbitrary closed nanosecond
bounds within the recording; neither path interpolates poses at a boundary.

The worker publishes full-resolution samples. Plot reduction is a rendering
choice only; the API computes interval metrics from the complete retained
artifact using a standard-library scorer. Its source hash must match the hash
recorded by the producing worker. The UI never calculates scores from reduced
plot vertices. A full-interval selection must reproduce the full-run metrics
within documented floating-point arithmetic tolerance.
The maintained reference comparison uses an absolute tolerance of `1e-12` in
each metric's reported unit (metres, radians or degrees); this checks arithmetic
agreement, not physical accuracy.

## Publication and access

New executions publish two additional files under the existing bounded manifest:

- `investigation.json`: complete camera support, raw/aligned/reference poses,
  errors, raw IMU samples, factual events, protocol and provenance.
- `image-index.json`: backend-only camera file identities, bound to the exact
  saved specification and prepared dataset manifest. Images are shared immutable
  inputs and are not duplicated per execution.

The independent evaluation container receives six explicit inputs: estimates,
reference, evaluation request, raw IMU, native status and investigation request.
It receives no camera image tree, network, database credentials or Docker socket.
Trajectory association and global alignment remain outside the HTTP process;
the API aggregates the resulting errors for a selected interval. The scientific
evaluator and the native estimator pins are unchanged.

The optional `compose.investigation.yaml` deployment overlay grants the API
read-only access to published artifacts and the two prepared camera directories.
The API has no NumPy, estimator executable, Docker access, scratch mount, raw IMU
tree or raw reference mount. The metadata-only stack remains usable without
provisioning a dataset. See the [deployment guide](workspace-deployment.md).

Every endpoint authenticates the current session and checks both project and job
ownership, the published attempt and retention before reading files. Sizes and
SHA-256 identities are verified; each path component is opened without following
symbolic links, and hard-linked or special files are rejected. Clients provide
camera names and integer frame indices, never filesystem paths. Responses use
`Cache-Control: no-store`; image blobs in the interface are revoked on navigation
or loss of access.

All routes are relative to `/api/v1/projects/{project_id}/jobs/{job_id}`:

| Method and path | Response |
| --- | --- |
| `GET /investigation` | The retained investigation artifact. |
| `GET /investigation/metrics?start_ns=…&end_ns=…` | Full-resolution interval support and scores, using unchanged global alignment. |
| `GET /investigation/images/{cam0\|cam1}/{frame_index}` | Verified original PNG for a zero-based camera index. |

Unknown or foreign resources return `404`, and missing authentication returns
`401`. An execution without investigation evidence returns `409`; expiry returns
`410` even before file collection runs. Missing or altered published files return
`503` without exposing host paths. Saved scalar job metrics remain after file
expiry. The existing seven-day file retention and 32 MiB per-attempt artifact
budget are unchanged.

## Failed and unavailable evidence

A failed execution can retain investigation of a structurally valid partial or
empty trajectory when bounded independent evaluation succeeds. Its job remains
failed, its evidence is labeled partial, and coverage uses the complete expected
camera schedule. This does not turn partial output into a successful result.
Malformed output, cancellation, missing files and expired retention have explicit
unavailable states. Executions created before this artifact format are not
silently backfilled.

Feature tracks, bias estimates, covariance, innovations and internal tracking
state are visibly unavailable because the pinned adapter does not export them.
Native process timing and memory describe the execution host. The prepared
recording is handheld; this view establishes no onboard, real-time or flight
validation. Comparing configurations and independent reproduction downloads are
separate increments.
