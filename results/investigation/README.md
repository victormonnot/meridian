# Synchronized investigation validation

Checked on **2026-10-18**. Increment 1.5 connects a retained execution to original
stereo images, input IMU, poses, support and full-resolution interval metrics.
The [investigation contract](../../docs/vio-investigation.md) defines timestamps,
coordinate frames, access, retention and metric semantics. The native estimator,
dataset, configuration specification and qualified evaluator remain unchanged.

## Automated evidence

The complete numerical/core suite passes **1069 tests**, with C++ integration
enabled and no skipped tests. Independent scientific fixtures cover known global
rotations, scale defects, missing initial/interior/trailing poses, unsupported
reference brackets, exact nanoseconds and IMU offsets, empty partial trajectories,
degenerate alignment and malformed inputs. Euler display fixtures include
180-degree rotations and gimbal lock. Interval fixtures retain the recording's
global alignment and full camera support rather than refitting the selection.

Adapter checks include the real isolated-evaluator entry point, explicit input
mount construction, validated partial evidence after native failure fixtures,
and cancellation or lease loss during that evaluation. Fabricated investigation
files and malformed trajectories are rejected. A complete investigation cannot
be published as partial evidence after a later validation failure. These failure
fixtures do not claim that the maintained recording naturally failed to initialize.

The complete backend suite passes **113 tests on PostgreSQL**, with no migration
drift. New checks exercise authentication and project/job ownership for JSON,
interval scores and images; exact timestamps; closed-interval support; immutable
scorer identity; expiry before file collection; deletion; old or unavailable jobs;
partial evidence; and altered or incorrectly bound manifests. They reject
oversized files, path substitution, symbolic links, hard links, special files,
duplicate JSON keys and nonfinite JSON constants. Responses do not disclose
private filesystem paths.

The frontend passes **212 Node tests**, its recorded-study export check and the
production build. New fixtures check precise timestamps, missing-data and angle
breaks, extrema-preserving display reduction, stale request suppression, image
access and navigation. UI scores come from the server's full-resolution scorer.

## Native and isolated-evaluation probes

A [complete new native execution](native-probe.json) generated 18 validated artifacts totaling
11,182,520 bytes, within the existing 32 MiB limit. Its investigation contained
all 2,821 camera epochs and 28,122 original IMU samples, including the samples
outside the camera interval. The separate image index contained both camera
identities without duplicating image bytes per job.

The trajectory SHA-256 remained
`d2a59ed328807d54af0a930006b7c2e850998c2dc7d6f89debd1c195ca4ed764`,
identical to the qualified baseline. Full-interval ATE RMSE was
`0.0612163695742259` m and orientation RMSE was `1.3255413925517476` degrees.
The artifact retained 2,689 estimates, 2,624 evaluated epochs, 132 initial missing
poses and 65 epochs without supported reference. The standalone native execution
took 46.1558 seconds on its local host; this is not total job latency or a
real-time guarantee.

A separate, explicitly [synthetic empty-trajectory fixture](synthetic-empty-probe.json) ran through the actual
isolated evaluation container. It preserved all 2,821 expected camera epochs and
28,122 IMU inputs, zero estimates, zero estimate coverage and null metrics, with
global alignment unavailable. This validates the partial-evidence path without
claiming an actual native initialization failure on room1.
Actual child inspection confirmed six read-only input files and one output
directory, without an image tree, network, database secrets or Docker socket.
The existing evaluator limits remained 0.5 CPU, 512 MiB and 64 processes. The
[source comparison](compute-sources.json) checked all eight compute/scientific
files in the probe image and the local application's worker against the workspace.

## Browser and deployment checks

The final local browser run passes 29 investigation checks and 49 workspace
regression checks. These cover an actual new native job, the linked cursor and
closed interval, exact camera timestamps, original PNG decoding, stale-response
suppression, cancelled-result availability, ownership and session expiry. Desktop
and 390 px views have no horizontal overflow, unhandled JavaScript errors or CSP
violations. The first local run stopped on a browser-harness focus interaction
after session cookies were cleared; correcting that harness and repeating the
full run produced the 29-check pass without changing the application.

An additional [synthetic browser fixture](synthetic-browser.json) passes 11
checks on the actual local application. It renders the independently evaluated
empty trajectory as a failed partial result, with input IMU, original images,
reference support and unavailable global alignment. Setting only this fixture's
expiry timestamp into the past produces HTTP 410 immediately, while failed job
metadata remains accessible. This checks expiry handling, not seven elapsed
days of retention. Harness session initialization and an assertion that confused
access expiry with asynchronous physical-file collection were corrected before
the final pass; neither required a product change.

The deployed HTTPS release passes the same **29 investigation and 49 regression
checks**, with normal TLS validation. The [check record](checks.json) includes
the exact assertions, response identity, sample counts and observed load times.
The [new hosted execution](hosted-run.json) published 18 artifacts, whose sizes
and hashes were verified after copying them from the service. Its trajectory
hash and full-interval numerical metrics match the qualified baseline.
The hosted job took 227.675 seconds from start to terminal publication, including
supervision and evaluation; the native estimator itself took 74.564 seconds.
The 18 published files total 11,182,569 bytes. These are host observations, not
embedded execution measurements.

The investigation JSON response contains 8,386,280 bytes. The first successful
full response took approximately 392 ms locally and 991 ms over HTTPS; three
observations ranged from 378–392 ms locally and 893–1,086 ms remotely. These
measure elapsed time between Playwright request callbacks, not a controlled
network benchmark. Peak API cgroup memory since container creation, including
startup and the browser checks, was 175,247,360 bytes locally and 171,675,648
bytes on the VPS, below the 512 MiB limit with no memory-limit or OOM events.
They do not establish sustained multi-user throughput or a latency guarantee.

The [deployment record](deployment-health.json) identifies release
`20261018-8230e773705d` and its actual images. All 76 runtime files in the
[source manifest](source-manifest.json) match the workspace. PostgreSQL identity,
start time, image, mounts and migrations were preserved. The API has exactly
three read-only data mounts and no NumPy or Docker client/socket. Inspection of
the [actual native and evaluator children](child-isolation.json) confirms the
native reference exclusion and six explicit read-only evaluator inputs under
the existing limits. Disposable QA accounts and their data were removed after
evidence capture; normal collection removed orphaned artifacts and scratch.
Service health and the other shared-host applications remained unchanged.

## Reproduce the checks

Install the pinned development dependencies and build C++ as described in the
[repository setup](../../README.md#technical-setup). Run the full suites with:

```sh
MERIDIAN_CPP_BINARY="$PWD/build/cpp/meridian_ekf_replay" .venv/bin/python -m pytest -q -ra
npm --prefix web test
npm --prefix web run check:studies
npm --prefix web run build
```

The [workspace setup](../../docs/hosted-workspace.md) covers the PostgreSQL test
environment and `python server/manage.py test workspace --noinput`. The
[deployment guide](../../docs/workspace-deployment.md) provisions computation,
the qualified dataset and investigation mounts. On an operator-authorized local
instance with Playwright and Chromium available, run:

```sh
node web/scripts/check-workspace-investigation-browser.mjs \
  http://127.0.0.1:8090/ outputs/investigation-browser
```

This script creates disposable accounts, a project and two jobs, including a real
native computation. Its report records the exact created identities for cleanup;
credentials are not saved. The workspace setup describes Playwright installation.

## Interpretation limits

The view identifies support and output gaps; it cannot infer their internal cause.
Feature tracks, biases, covariance, innovations and internal tracking status remain
unavailable because the maintained native adapter does not export them. Camera
images are original mono16 PNGs; display reduction affects plot vertices only.

The full-recording alignment remains fixed when selecting an interval. Fitting a
fresh alignment per interval would answer a different question and is not done.
Coverage always includes expected camera epochs, including initialization and
missing reference. The interval scorer's source hash is checked against its
recorded provenance. Agreement with the existing evaluator checks software
consistency, not navigation accuracy on a drone.

The recording is handheld and room2 remains reserved. No onboard, closed-loop,
real-time or flight validation is established. General uploads, configuration
comparison, independent reproduction downloads and backup restoration are outside
this increment.
