# Complete hosted workspace acceptance

**Hosted engineering acceptance passed, 20 October 2026.** The complete room1
workflow, operational boundaries, off-server recovery and independent export
replay pass. **Room2 fails the unchanged numerical qualification** because
reference and evaluated coverage are insufficient. This negative result is
retained separately; completion of this bounded workspace does not qualify a
general-purpose drone estimation product.

The [acceptance protocol](../../docs/hosted-acceptance-protocol.md) and
[room2 protocol](../../docs/room2-acceptance.md) were committed and pushed as
[`f3c7ff6`](https://github.com/victormonnot/meridian/commit/f3c7ff6) before the
reserved recording was acquired or evaluated. Starting implementation:
`50ac01c388d020102ec94e402307d5ed0071b62c`.
The [operator guide](../../docs/workspace-acceptance.md) describes the maintained
browser and operational exercises; the [backup procedure](../../docs/workspace-backup.md)
describes the private recovery rehearsal.

## Scope and evaluator

This is an automated engineering acceptance exercise with disposable accounts,
actual native computations and retained scientific responses. The HTTPS client
ran on a Linux workstation physically separate from the VPS. The local HTTP
run uses that workstation for both client and service. It is not feedback from
an external drone engineer or an accessibility conformance audit.

The hosted reference remains calibrated TUM-VI room1 and the pinned OpenVINS
adapter. A user can save supported configurations, run them, investigate
synchronized measurements and estimates, compare exact jobs and export a
reproducible experiment. Uploading a user recording, editing arbitrary estimator
code and operating a drone remain outside this first workspace.

## Complete browser workflow

[Local evidence](local-browser.json) and [HTTPS evidence](hosted-browser.json)
each record all 50 predeclared checks passing on the first execution. The same
unchanged harness covers registration, logout/login,
private projects, provenance, an immutable 200-point baseline, a second account's
real baseline, a queued spare cancellation, a rejected third active owner
request, investigation, a 100-point revision, saved comparison and verified
download/retry. No scientific response or successful business response is mocked.

Keyboard cursor control, exact camera timestamps, original stereo image decoding,
IMU selection, missing-pose states, interval support and unchanged global alignment
are checked. Viewport checks cover 1,440 pixels in light/dark themes and
390 pixels in the light theme, overflow, deep links and reloads. The script deliberately triggers a reversed
interval validation error and a transport interruption, preserving both.
There are no uncaught JavaScript or CSP errors in either journey.

The three successful local execution intervals do not overlap. Native times
are 51.35 s and 49.07 s for the two baselines, and 32.90 s for the candidate.
The VPS baselines take 85.08 s and 82.40 s natively, and the candidate takes
58.63 s. Observed queue waits are 1.18 s, 248.83 s and 234.15 s respectively.
The first baseline's full execution takes 250.00 s, including input verification,
native processing, evaluation and publication; native time alone does not
describe the user's wait.

The local and hosted baseline evaluation payloads and qualification decisions
are identical. Position ATE is 0.0612164 m for the baseline and 0.0511320 m for
the candidate on their respective evaluation supports. The saved comparison
also records common support; these two observations do not establish a generally
better configuration.

The hosted candidate ZIP is 681,997 bytes, with SHA-256
`d7243ecdc428c1e50f80eafee3179817a2530ba38fb2556619ef89f6ebf53af0`.
Two browser downloads and a separate authenticated HTTP response have identical
bytes. CDP exposes an empty attachment response body; the actual downloaded file
and the application's WebCrypto check establish its contents.
These timings are observations from single journeys on shared machines, not
throughput or real-time guarantees.

## Operational behavior

[Local operational evidence](local-operations.json) and
[hosted operational evidence](hosted-operations.json) each record 22 checks
passing on the first real exercise. The operator harness creates five disposable
accounts and verifies their HTTP identities against the exact Compose database
before introducing faults.

- Killing the worker during its own native computation leaves the first attempt
  explicitly expired with an infrastructure failure. A second attempt succeeds
  with the same saved specification; the old child is absent before the retry
  runs. At most one running native child is observed.
- Cancelling an active native job produces a terminal cancellation, removes its
  child and exposes neither successful metrics nor a complete export.
- Killing another owned native child produces the service's actual
  `numerical_failure` classification without a retry or fabricated result.
  This is an induced interruption, not a discovered OpenVINS defect.
- Damaging one owned retained trajectory causes export integrity rejection.
  Restoring its exact bytes restores the identical ZIP. Advancing only that
  job's expiration revokes export and triggers physical collection while
  preserving its metric metadata.
- With the worker briefly stopped, eight queued requests across five accounts
  are accepted; the next request is refused while its owner remains below the
  per-account cap. Cancelling the queued requests releases all eight slots.
- The same account reaches ten accepted daily submissions. Cancellation and
  deletion do not replenish that budget; a new request receives `429` and
  `Retry-After`, while an existing idempotency key still returns its original job.

These fault scenarios are administrator exercises on owned test resources.
Expiration is accelerated; seven days do not elapse. Polling observations are
not a throughput guarantee. Cleanup restores the initial application record
counts, physically removes all owned job directories and preserves global
submission counters. An independent stdlib audit verifies every one of the
38 payload files in the recovered archive against its manifest; this inventory
audit is distinct from numerical reproduction.

## Backup and restoration

The local rehearsal copies PostgreSQL metadata and every retained publication,
then restores them into a fresh PostgreSQL container with no network or published
ports and a separate artifact directory. All ten table fingerprints, migration
records and artifact hashes match before copied sessions are cleared. The local
snapshot contains 16 sessions; all 16 are cleared only in the restored copy.

The matching application image exercises its real CSRF, login, ownership,
investigation, comparison and export routes through Django's in-process HTTP
client. Owner access returns `200`, the other account receives `404` and signed-out
access receives `401`. The restored export is byte-identical to the browser
download. This is not a browser journey against a restored network service.

The hosted snapshot is transferred over authenticated SSH to the separate
development workstation and restored there. Its 58 inventory files total
33,620,677 bytes, including the database dump and 57 published artifact files
for three successful jobs. The measured pause for stopping, copying and issuing
API/worker restart commands is 17.169 s; health is verified afterwards, so this
is not a measured end-to-end availability interruption. The source database
container and its existing data remain in place.

The first hosted recovery attempt passes every metadata/artifact check and all
seven access checks, including the exact candidate ZIP above. Both copied
sessions are cleared in the restored database. Its restricted application role
has no superuser, role-creation, database-creation or replication privileges.
The original and transferred snapshot manifests and all inventory hashes match.

Actual rehearsals exposed operational-tool defects that unit tests had not
detected. The final helper binds PostgreSQL's pinned distribution digest to the
local Docker image identity, rejects a restore destination nested in its backup,
and waits for the final TCP server instead of the entrypoint's temporary Unix
socket server. PostgreSQL 17 forbids removing the bootstrap role's superuser
attribute, so restoration now creates a separate restricted application owner
before loading the dump. Its actual role flags are checked afterwards.

The initial interrupted restore attempts are retained in the private evidence;
their disposable containers are removed without replacing source data. A private
wrapper filename also initially shadowed Python's `operator` module before any
service action; renaming the wrapper corrected that invocation.
After verification, raw snapshot dumps, copied artifact trees, restore clones
and the temporary API image transfer are removed. Private hash/access reports
and the recovered experiment ZIP remain for audit. No scheduled backup or
off-server retention service is installed by this rehearsal.

## Independent replay after source deletion

The recovered hosted ZIP is reproduced outside the source checkout and backend
after authenticated deletion of its original job. Both original job and export
URLs return `404`. The browser download and restored copy are byte-identical.
The [cleanup record](browser-cleanup.json) binds these observations to the exact
archives and records preservation of unrelated data and submission counters.
The [reproduction evidence](reproduction/README.md) records the standalone
launcher, container inspection and independent audit.

This execution uses a new extraction and output directory, while explicitly
reusing the clean Python 3.12 environment, independently acquired/prepared public
room1 inputs and source-built native image from the preceding export acceptance.
It does not claim another fresh dependency installation, download or image build.
All 24 bundled source files match the current source; only the dataset preparation
module differs from the preceding export release.

The standalone replay exits successfully in 56.716 s, with 32.765 s of native
processing. All 38 archive payloads and 5,651 prepared input files verify.
Trajectory bytes, timestamps, evaluation support and missing regions are
identical; independently calculated direct pose differences and differences
in RMSE are zero. The original specification remains unchanged and the distinct
rebuilt runtime is reported explicitly.

The audit recomputes direct pose differences and RMSE from retained per-sample
errors. It is not a second SE(3) alignment implementation or evidence of
reproducibility across arbitrary hardware and dependency versions. No account,
private service endpoint or deleted source job is needed for replay.

The local cleanup wrapper initially misread the successful deletion response
schema, then used the wrong worker working directory for a filesystem probe.
Those interrupted operator reports are retained; the first local DELETE body
was not saved, so only the subsequent authenticated `404` checks are asserted
from retained evidence. The corrected HTTPS invocation captures DELETE `200`
and both `404` responses without interruption. These steps are separate from
the two successful browser journeys.

## Deployed identity and resources

The qualified release is `20261020-6a7122a290ca`, with source-bundle SHA-256
`6a7122a290ca19bc90300f96758022aa7fe821bf1defc10188bbc927b8ec3294`.
The [infrastructure evidence](infrastructure.json) binds all 103 release files,
34 API source files, 40 worker source files and each image's 24 reproduction
source files to the accepted checkout. It records application image identities,
the unchanged native runtime and prepared room1 manifest.

Live inspection observes all three native/evaluation container pairs in each
browser journey. Native children have sensor/configuration inputs and separate
output, with no reference, network, credentials or Docker socket; the reference
is available only to separate evaluation. The API has neither NumPy nor a Docker
CLI/socket. The trusted worker retains host Docker privileges; this is not a
sandbox for arbitrary uploaded code.

After the hosted operational exercise, the API's current cgroup peak is
177,188,864 bytes under its 512 MiB limit, with no observed OOM. This counter
covers the period after the backup stop/start, including later checks and faults;
it does not measure the entire earlier browser journey or the cost of a single
request. The evidence also retains native RSS, output sizes, host capacity,
service limits and the 128 MiB bounded job scratch configuration.

Final checks preserve the original PostgreSQL container, image, start time and
mounts. All acceptance accounts and their owned application records are removed,
submission counters remain intact, and no numerical children, job artifacts or
scratch attempts remain. Meridian and the three existing sibling applications
return HTTPS `200`; their configuration is unchanged. The inert native image
retention container remains present and has never started.

## Reserved room2 outcome

**All three room2 executions fail the original numerical qualification.**
They finish successfully as native computations and produce byte-identical
trajectories. Position ATE is 0.0926172 m and orientation RMSE is 2.340748° on
supported samples. Estimate coverage is 96.3914%, but reference coverage is
89.2783% against a 95% gate and evaluated coverage is 85.6697% against a 90% gate.

An independent timestamp audit reproduces the unsupported reference counts:
308 camera samples lie in motion-capture gaps wider than the fixed 20 ms limit,
and one lies after the final reference. Estimates have no interior or trailing
gaps after initialization. The gates, denominator, interpolation policy,
baseline parameters, evaluator and native implementation remain unchanged.
See the [full negative result and evidence](room2/README.md).

This is not a two-sequence accuracy ranking or proof of robustness on drone data.
Room2 remains a separate offline evaluation, not another selectable hosted profile.
The original room1 prepared manifest and all three supported configuration
identities remain unchanged.

## Automated boundaries

[Automated checks](automated-checks.json) record 1,245 Python tests with C++
integration, 161 PostgreSQL-backed API tests, 232 web tests and one native C++
test passing. Migration drift, recorded-study export and production web build
checks also pass.

Four new PostgreSQL tests use explicitly synthetic retained/queued job fixtures
to exercise 20 retained jobs per account, 200 globally, eight global active jobs
and 100 daily global submissions. They check rejected admissions, deletion,
cancellation and idempotent retry without presenting these fixtures as hundreds
of actual executions.

The initial backend run exposed a test-helper name collision with Django's
`fixtures` attribute: the 157 existing tests passed while the new class could not
initialize. Renaming that helper allowed all four new tests and the complete
161-test suite to pass. This was a harness correction, not a hidden application
or estimator failure.

## Limits to carry forward

The supported service budget remains one global computation, two active jobs
per account, eight globally, 20/200 retained jobs per account/globally and
10/100 accepted submissions per account/globally per fixed 24-hour window.
The [job contract](../../docs/hosted-vio-jobs.md) specifies numerical resource,
artifact and retry limits. Observed success under these limits is not a
general multiuser capacity guarantee.

The preview still uses its temporary HTTPS hostname. The intended custom
hostname is not activated. Password recovery, user recording uploads,
arbitrary estimator execution and scheduled backups remain unavailable.
One backup/restore rehearsal cannot establish continuous availability or a
disaster-recovery objective.

The next data work needs explicit clock/calibration conventions and independently
validated reference support for user acquisitions. Room2 demonstrates why a
recording described as having motion capture still needs a timestamp-level audit.
The hosted timings also make repeated input verification and queue latency
visible costs to investigate before increasing the service budget. These are
observed requirements, not an implementation plan for the next part.

These handheld indoor recordings do not demonstrate onboard deadlines,
embedded integration, flight safety, recovery after visual loss, covariance
consistency, expert usability or market value. The target remains engineering
support for state estimation; this evidence does not establish replacement
of an estimation engineer. Real bench acquisition remains in scope for later
work, with the drone disarmed, propellers removed and the estimator outside the
flight-control loop.
