# Durable VIO computation

The workspace can enqueue a saved experiment revision, execute the maintained
OpenVINS reference, evaluate its trajectory independently and retain the outcome.
The browser submits structured identifiers. It never supplies a command, image,
filesystem path, download URL or executable code.

The [configuration contract](versioned-experiments.md) remains unchanged. The
native image, prepared room1 recording, calibration, scheduling and evaluation
protocol retain their qualified identities. The
[checked runtime binding](vio-reference.md#immutable-experiment-specifications)
handles Docker configuration versus OCI manifest IDs without changing saved
specifications. This increment adds execution
orchestration, not a new estimator or a flight-validation claim.

## Browser and API

Select an immutable experiment version and choose **Run version N**. Runs are
also listed under the project's Results view. Closing the browser does not stop
work. Reopening a job retrieves its persisted state, attempts and outcome.
Configuration edits cannot change a submitted job's snapshot.

All routes use the existing authenticated, CSRF-protected `/api/v1` contract.
Every resource lookup includes project ownership; another account receives 404.

| Method and route | Behavior |
| --- | --- |
| `GET /projects/{project}/jobs` | List jobs, limits and worker availability. Optional `experiment_id` and `revision_version` filters. |
| `POST /projects/{project}/jobs` | Submit `{schema_version: 1, experiment_id, revision_version, idempotency_key}`. The key is a canonical UUID. |
| `GET /projects/{project}/jobs/{job}` | Retrieve state, immutable configuration identity, attempts and validated summary. |
| `POST /projects/{project}/jobs/{job}/cancel` | Request cancellation with `{schema_version: 1}`. Repeated cancellation is harmless. |
| `DELETE /projects/{project}/jobs/{job}` | Delete terminal metadata; the worker subsequently removes private files. Active jobs return 409. |

A new submission returns 201. Repeating the same key and target returns the same
job with 200 and `reused: true`, including after an uncertain network response.
Reusing the key for a different target returns 409. This identity lasts while the
job is retained; explicitly deleting it removes its idempotency record.

States are `queued`, `running`, `evaluating`, `succeeded`, `failed` and
`cancelled`. Phases refine preparation and estimation without inferring a
completion percentage. Active cancellation sets `cancel_requested` until the
worker has stopped its children. A queued cancellation is immediate. Terminal
cancellation does not rewrite a finished outcome.

`succeeded` means that output validation and independent evaluation completed.
The separate `result.qualification.passed` can be false. Integration sanity
criteria are not flight requirements or a universal configuration ranking.

## Queue and recovery

PostgreSQL stores jobs, attempts and a singleton queue mutex. Submission, claim,
heartbeat, cancellation and final publication serialize through that mutex.
Submission also locks the parent project; deleting a project or experiment is
refused while it contains active work. No estimator runs inside a transaction.

A claim records a unique attempt and a 30-second lease. An independent worker
thread renews it every five seconds, including while inputs are hashed. A local
monotonic deadline stops supervision if database renewal stalls; database I/O
does not hold the supervisor's in-memory lock. Worker SQL and connection waits
are separately bounded. An expired lease cannot be renewed or publish results.

An abandoned attempt retains the global execution slot until recovery confirms
that all its deterministically named, labelled child containers are gone. Only
then can it be retried. Infrastructure failures receive at most one retry: two
attempts total. Invalid inputs, numerical failures, initialization failures,
resource exhaustion and timeouts are terminal. Cancellation never retries.
Worker shutdown stops its children and leaves lease recovery to record the
interruption. An unavailable Docker daemon can delay cancellation or recovery;
the queue does not release that slot on an unverified cleanup.

## Isolation and budgets

The worker is a trusted infrastructure component with access to the Docker daemon.
That access is equivalent to host administration; it is not an arbitrary-code
sandbox. The HTTP container has no Docker client/socket or numerical dependencies.
The optional [investigation overlay](vio-investigation.md) mounts published files
and the two camera directories read-only for authenticated access; raw IMU,
reference and scratch directories remain unavailable to HTTP. Only the maintained
reference is executable.

| Component | Limits and access |
| --- | --- |
| Native estimator | Non-root; 2 CPU, 4096 MiB, no swap allowance, 128 processes, 900 seconds; read-only root and sensor/config mounts; no network, service secrets or reference trajectory. |
| Independent evaluator | Separate container; 0.5 CPU, 512 MiB, 64 processes, 120 seconds; no network, secrets or Docker socket; only explicit estimates, reference, requests, IMU and native-status inputs, without camera images. |
| Worker | Non-root trusted orchestrator; 0.25 CPU, 512 MiB; private database network and operator-provisioned storage. |
| Scratch | Dedicated host tmpfs, 128 MiB total, shared at identical paths with child containers. Startup refuses a missing or incorrectly bounded mount. |
| Logs and artifacts | Each supervised process log is capped at 2 MiB; Docker log storage is disabled for compute children. Validated artifacts are capped at 32 MiB per attempt. |

Artifact size monitoring occurs during supervision; the 128 MiB filesystem bound
covers transient growth between checks. The 32 MiB limit applies to accepted
artifacts and publication. CPU/memory values are caps, not measured requirements.
The immutable native resource profile has not been silently reduced for hosting.

There is one global computation slot. The initial preview admits at most two
active jobs per account and eight globally, and retains at most 20 job records
per account and 200 globally. Separate fixed 24-hour submission windows allow
10 per account and 100 globally; deleting jobs does not reset these counters.
Repeated idempotent requests do not consume another submission.

The worker admits new work only with at least 5 GiB host `MemAvailable` and 10 GiB
free artifact storage. Otherwise work stays queued. These are admission checks,
not reservations against unrelated processes on the shared host. The worker's
health check establishes a recent database heartbeat, not estimator readiness or
a guaranteed start time.

## Validation and publication

Exit code zero alone cannot publish success. The adapter checks the immutable
specification, runtime and prepared-input identities, native status/conventions,
strict counts and timestamps, finite trajectory/metrics, evaluation protocol,
configuration integrity and artifact hashes. The evaluator remains the qualified
independent implementation; ground truth is never mounted into the estimator.

The manifest contains only relative, allowlisted regular filenames, sizes and
SHA-256 identities. Symlinks and unexpected files are rejected. The worker copies
only manifest-listed files, verifies their bytes again, flushes them and promotes
the complete directory atomically. A database transaction then publishes the
summary and manifest only if the same attempt still owns a live lease and has
not been cancelled. A late cancelled success cannot publish its complete
manifest. Unreferenced directories are collected privately.

Failure codes are `invalid_input`, `initialization_failure`, `numerical_failure`,
`resource_exhaustion`, `timeout`, `infrastructure_failure` and `cancelled`.
Public messages do not expose host paths, raw database errors or service secrets.
The most recently published manifest can contain bounded failure diagnostics;
these are not successful results. Diagnostics from cancelled or interrupted
attempts are not guaranteed to survive. Attempt metadata remains in the job
history even when a later publication replaces earlier diagnostic files.

Private files expire after seven days. Job metadata and validated metric summaries
remain until explicit deletion, subject to the retained-record quotas. The worker
collects expired/orphaned files during its polling loop; deletion is deferred while
it is stopped or busy. This is application retention, not a qualified backup or
restoration service. Independent downloadable reproduction packages remain a
later increment; the browser exposes persisted execution summaries and retained
[investigation evidence](vio-investigation.md), including valid partial outputs
when independent evaluation is possible. The job's failure state is preserved.

[Validation evidence](../results/jobs/README.md) separates automated failure
fixtures, actual local crash recovery and deployed browser/native checks.
Deployment and scratch provisioning are described in the
[workspace deployment guide](workspace-deployment.md). The original
[reference guide](vio-reference.md) retains local reproduction commands and
dataset attribution. No onboard, real-time, closed-loop or flight validation is
established by hosted execution.
