# Durable VIO job validation

Checked on **2026-10-18**. Increment 1.4 connects immutable experiment revisions to persistent jobs, a
separate compute worker and independent trajectory evaluation. The
[job contract](../../docs/hosted-vio-jobs.md) documents states, failure categories,
resource budgets, retry rules and retention. This report distinguishes automated
failure fixtures from real native and browser executions.

## Automated checks

The complete backend suite passes **92 tests on PostgreSQL**. Coverage includes
owner/CSRF checks, immutable input snapshots, idempotency, submission and retained
quotas, deletion rules, exclusive claims, leases, cancellation, bounded retries,
late-result fencing, artifact hashes and atomic publication. A blocked database
heartbeat cannot hold the in-memory supervisor lock past its local deadline.
An altered file is rejected as invalid output, without retry or result publication.

Four new tests use independent PostgreSQL connections and actual row locks:
concurrent identical submissions create one job; competing submissions cannot
exceed the last account slot; competing workers claim one attempt; and
cancellation/finalization serialize into one terminal outcome. Existing account,
project and revision race tests remain in the same suite.

Retention tests move the expiry timestamp explicitly: they check file removal
while preserving metrics and attempt records. Orphan scratch removal requires
confirmed child cleanup and preserves an active attempt. These tests do not
represent seven days of elapsed deployment operation or backup restoration.

The full numerical/core suite passes **1034 tests**, with C++ integration enabled.
It includes explicit checks for the classic/configuration and containerd/OCI
image identities, tampered bindings and rejection of unrelated images.
The compute supervisor tests launch real local subprocesses for cancellation,
lease loss, timeouts and excessive stdout, including a process that has closed
its stdout while continuing to run. Docker invocation/inspection fixtures cover
resource limits, OOM classification, reference isolation and verified cleanup.
Artifact fixtures reject nonfinite or malformed outputs, bad timestamps/counts,
configuration changes, unexpected files, symlinks and oversized outputs. Exit zero
and plausible headline metrics do not bypass validation. A completed execution
can retain `qualification.passed: false`; execution validity and integration
sanity criteria remain separate.

The frontend passes **200 Node tests**, the recorded-study export check and its
production build. Browser checks below exercise actual server responses and native
computation separately from these UI unit tests.

## Local browser and crash recovery

The real container stack passed **22 job-browser checks** and **49 workspace
regression checks**. The browser submitted historical version one after saving
a different version two, deliberately dropped a successful submission response,
retried the same idempotency key and confirmed that only one job existed. It
navigated away, reopened/refreshed the job and read its actual evaluated result.
Queued and active cancellation, loss of session cookies, cross-account isolation,
terminal deletion and stopped polling were checked. Desktop and 390 px layouts
had no horizontal overflow; no uncaught JavaScript or CSP error was observed.

A separate real job exercised worker loss during native estimation. The test
sent SIGKILL to that worker only. Docker treated this as a manual stop, so the
operator explicitly started the worker again; this is not evidence of automatic
service restart. The expired lease retained its slot until child cleanup. The
first attempt remained `expired` with `infrastructure_failure`; a different
worker ID executed attempt two with the same specification. Only attempt two
published a successful result. Sampled container observations found no overlapping
native estimators; no child remained after completion.

The recovered trajectory SHA-256 was
`d2a59ed328807d54af0a930006b7c2e850998c2dc7d6f89debd1c195ca4ed764`,
identical to the original qualification on the same local hardware. The initial
check script stopped because it expected capitalized Docker error text when
confirming the old container was absent. A corrected check resumed evidence
collection on that same job; no new crash or substitute successful job concealed
the interrupted run. Polling had a gap after that assertion, so the concurrency
observation is sampled rather than continuous.

All 16 published files were independently checked against their sizes and hashes.
A separately authenticated session was then kept across an API-only restart:
the session, job outcome and saved revision remained identical, and the database
container was unchanged. The [machine-readable recovery evidence](local-recovery.json)
retains timings, metrics, source/runtime identities and observation limits.

## Hosted deployment

Release `20261018-adf227683c6c` serves the
[HTTPS workspace](https://meridian.151.80.144.150.sslip.io/). The
[source manifest](source-manifest.json) records all 69 deployed source files,
the release archive and actual application/runtime image identities. Migration
`0003_durable_vio_jobs` is additive. The existing PostgreSQL container, start time
and data volume were retained. The final worker uses 30-second health-check
intervals/timeouts under its 0.25 CPU cap; its queue heartbeat remains independent.

[Transfer verification](input-transfer.json) checked the prepared manifest and
all 5,651 files, both archive hashes, the exact native configuration and its eleven
layers. The server's containerd store identifies the same native content by OCI
manifest `54863f14…`, bound cryptographically to the original configuration
`73e1213d…`. No native rebuild or change to saved experiment identities was used.

The final HTTPS deployment passed the same **22 job checks and 49 regression
checks** from a separate workstation with certificate validation enabled. The
[check inventory](checks.json) records both environments. The browser observed
actual queued/running/evaluating/succeeded/cancelled states, retried an uncertain
POST without duplicate work, and cancelled queued and active work. The active
HTTPS cancellation check waits for a running job, not specifically a native
process; stopping a native child was separately exercised locally.

The [hosted run evidence](hosted-run.json) retains full-resolution evaluation
metrics, host/runtime identities, timings and the complete validated manifest.
All 16 files were independently rehashed after publication. The deployed native
trajectory had the same SHA-256 as the local qualification and recovered run.
That is an observation for these executions, not a cross-hardware determinism
guarantee.

| Observation | Hosted 200-point baseline |
| --- | ---: |
| Position ATE RMSE, m | 0.06121637 |
| Orientation RMSE, degrees | 1.32554139 |
| Estimated / expected camera samples | 2689 / 2821 |
| Evaluated / expected camera samples | 2624 / 2821 |
| Reference-supported / expected camera samples | 2756 / 2821 |
| First estimate delay, s | 6.600339685 |
| Native wall / CPU time, s | 75.6588 / 74.3924 |
| Native peak RSS, KiB | 81792 |
| Entire job, started to finished, s | 224.78385 |

The total job duration includes preparation, runtime verification, separate
evaluation and publication; native time alone is not user-visible latency. These
are single shared-host observations, not a throughput or real-time guarantee.
The host reports four logical CPUs and an Intel Haswell processor model.

[Observed container settings](container-isolation.json) cover the actual native,
probe and evaluation children: non-root user, no network, read-only root, dropped
capabilities, disabled Docker log storage and the declared CPU/memory/PID caps.
Native mounts contained only sensors, configuration and output. Evaluation
received three explicit input files plus its output directory, without the sensor
tree, database credentials or Docker socket. API inspection separately confirmed
no NumPy or Docker client. The worker remained healthy under computation; its
measured health checks completed within the final 30-second budget.

The [final deployment check](deployment-health.json) confirmed all four Meridian
services healthy, the existing database container and volume unchanged, and HTTPS
200 responses for Meridian and the three existing applications. The four exact
QA accounts and their five sessions were removed after evidence collection.
Normal artifact collection left no QA output, scratch attempt or compute child;
aggregate submission budgets were preserved.

## Boundaries

Only the maintained OpenVINS/TUM-VI room1 profile runs. The executions in this
report use the 200-point baseline; they do not qualify every supported parameter
choice or rank configurations. The estimator receives
calibrated stereo/IMU measurements without reference poses or GNSS. Reference
poses are confined to independent evaluation. The existing quaternion/frame/time
conventions and predeclared integration sanity criteria remain those of the
[reference qualification](../vio-reference/README.md).

The worker is trusted infrastructure with Docker daemon access. Native and
evaluation containers receive no daemon socket, service secrets or network.
The HTTP container contains neither numerical dependencies nor data/artifact
mounts or a Docker client. A single global compute slot and admission thresholds
bound use of the shared host; they do not guarantee capacity if other applications
consume resources later. This is not an arbitrary-code sandbox.

The recording is handheld; no onboard, closed-loop, real-time or drone-flight
performance is established. Room2 remains reserved. Uploads, synchronized VIO
investigation, run comparison and independent result downloads are later
increments. Account recovery and qualified backup restoration remain unavailable.
