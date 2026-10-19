# Part 1: hosted workspace and reference VIO workflow

**Status: increments 1.1–1.6 completed by 2026-10-19; increments 1.7–1.8 remain planned.**
This plan details part 1 of the [development roadmap](professional-estimation-roadmap.md).
The current [implementation](../README.md) includes the qualified
[local VIO reference](../results/vio-reference/README.md), the original single-axis
study and its static results explorer. The [project workspace](hosted-workspace.md)
adds accounts, private projects, catalog metadata and versioned experiments.
[Durable VIO jobs](hosted-vio-jobs.md) add isolated execution and independent
evaluation of saved specifications. [Synchronized investigation](vio-investigation.md)
connects retained executions to stereo images, input IMU, poses, support and
traceable interval metrics. [Saved comparisons](vio-comparisons.md) preserve
configuration differences and outcomes on each run's own and common support.

## Outcome and scope

At completion of part 1, an engineer can open the hosted site, create a private
project, select a prepared camera/IMU recording, run a reference VIO implementation, change a supported
parameter, inspect and compare actual results, and export a reproducible
experiment. Using the hosted application requires no local Meridian installation.
The reference recording is an engineering benchmark with traceable calibration
and evaluation data. The product workflow is configuration, execution,
investigation, and delivery.

This part provides a complete but deliberately bounded engineering task. The
initial estimator and datasets are maintained by the service. User acquisitions
enter in part 2; broader estimator integration and code modification enter in
parts 3 and 4. Arbitrary uploaded code, general calibration tooling, automatic
tuning, AI agents, team administration, and flight-control integration are outside
this part. Only the eight increments below are detailed here.

GNSS is not an estimator input for this reference. Independent motion-capture
poses are used only for evaluation. Existing roll experiments and bench replay
remain available with their original claims and scope; they do not fulfill the
VIO milestone. Real drone bench acquisition remains in the overall roadmap,
initially disarmed, with propellers removed and the estimator outside the control
loop. Meridian remains independent of ARGOS.

## Proposed reference and qualification gate

| Component | Initial choice | Qualification required in increment 1 |
| --- | --- | --- |
| VIO implementation | OpenVINS, using its EKF/MSCKF visual-inertial estimator. | Pin and build a revision, execute real inputs, verify initialization and output semantics, and document dependencies and notices. |
| Integration recording | TUM-VI `room1`, calibrated 512 × 512 stereo/IMU variant. | Pin exact files, calibration, masks, transformations, and checksums; confirm matching configuration. |
| Separate evaluation recording | TUM-VI `room2`, reserved from configuration tuning. | Freeze the acceptance protocol before inspecting its results; record any later use for tuning. |
| Execution | Headless server-side runner; prefer a small ROS-free adapter if feasible. | Compare the required adapter work with the upstream serial runner and select a reproducible path. |
| First engineering comparison | Baseline versus one supported configuration change. | Select a meaningful bounded parameter and record units, valid range, and expected tradeoff before comparing results. |

[OpenVINS](https://docs.openvins.com/) supplies a concrete EKF-based VIO reference.
Its [ROS-free interface](https://docs.openvins.com/gs-installing-free.html) requires
the caller to feed buffered image and IMU data. The inspected revision,
`69488123ed9362dd44b6f28e7f4680abbff1442b`, is now built and qualified on room1
through Meridian's [real-recording adapter](../integrations/openvins/README.md).
The upstream non-ROS simulation executable does not provide that recording path.

The upstream
[serial runner at that revision](https://github.com/rpng/open_vins/blob/69488123ed9362dd44b6f28e7f4680abbff1442b/ov_msckf/src/ros1_serial_msckf.cpp)
can initialize from ground truth when `path_gt` is supplied. The Meridian
reference must exclude this path and keep reference poses inaccessible to the
estimator process. The runner also defines image pairing and scheduling behavior;
a replacement adapter must explicitly define and verify its own behavior.

[TUM-VI](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset) provides
motion-capture references throughout the room sequences and publishes its data
under CC BY 4.0. Preserve attribution and transformation notices. These are
handheld recordings: successful replay would validate this integration, not
performance on a drone or in flight. OpenVINS source is licensed under
[GPL-3.0-or-later](https://github.com/rpng/open_vins/blob/69488123ed9362dd44b6f28e7f4680abbff1442b/LICENSE);
document the adapter and distribution arrangement, notices, and source obligations
before distributing a runtime. No general licensing compatibility conclusion is
implied by selecting a candidate.

The [qualification report](../results/vio-reference/README.md) records the measured
accuracy, coverage, repeatability and host resources. Future reference revisions
must meet the same declared gate or record a protocol change. Deployment cost
and sustained multi-user throughput have not been measured yet.

## Minimal architecture and contracts

The stack retains useful Vite/D3 components. Increment 1.2 uses Django for the
Python metadata API and its established authentication/session integration, with
PostgreSQL persistence and individually owned projects. Django replaces the initial
FastAPI candidate. Application and API share an origin behind a dedicated Nginx
service on an existing VPS; the [deployment guide](workspace-deployment.md) records
the isolated services and limits. Increment 1.4 adds durable PostgreSQL jobs,
private artifact storage and a separate containerized computation worker.

The browser submits a saved revision's identifiers and follows the job's progress.
The API snapshots its immutable specification, checks ownership and quotas, and
queues work. A worker executes
the approved estimator with sensor inputs only; a separate evaluation stage reads
the resulting estimates and independent reference. Validated artifacts are
published with hashes and an immutable manifest. The UI reads authorized job
summaries and evaluated metrics. Increment 1.5 adds authenticated reads of
manifest-verified investigation artifacts and original camera images, with a
shared timeline and full-resolution interval scoring. Increment 1.6 adds compact,
immutable comparison snapshots with exact run identities and support regions.
Independent reproduction
downloads remain planned for increment 1.7.

Heavy computation must not run inside an HTTP request or rely on in-process
background tasks for durability; the
[FastAPI guidance](https://fastapi.tiangolo.com/tutorial/background-tasks/)
distinguishes heavy external computation. A PostgreSQL queue can initially avoid
an additional queue service;
[row locking](https://www.postgresql.org/docs/current/sql-select.html) is only a
primitive, so leases, retries, cancellation, and crash recovery still require
explicit implementation.

Persisted specifications and result/export manifests carry explicit schema or
protocol versions. Reject unsupported versions rather than interpreting them
using current defaults. Keep these identities distinct:

- **Dataset version:** sensor-file hashes, exact variant, units, axes, clocks,
  calibration, preprocessing, attribution, and a separate evaluation-reference
  identity and coverage description.
- **Run specification:** immutable dataset/calibration identity, selected interval,
  estimator and adapter revisions, runtime image digest, normalized configuration,
  scheduling policy, output conventions, resource budget, and evaluation protocol.
  Record seeds and known nondeterminism where applicable. The same specification
  must support hosted execution and independent reproduction.
- **Job and attempt:** a durable user request and its individual execution attempts,
  with state, timestamps, lease, progress, cancellation, and failure reason. A retry
  must not overwrite a previous attempt or silently change the specification.
- **Result manifest:** validated artifact hashes, actual runtime environment,
  input/output counts, coverage, evaluation status, limitations, and provenance.
  Missing diagnostics are explicitly unavailable rather than synthesized.

The existing static simulation schema is not the execution API. Extend contracts
only for the selected reference; a universal plugin system is unnecessary here.
Hashes establish identity, not permission to access a resource. Immutability
prevents silent modification; it does not prevent an owner from deleting data
under the documented retention policy.

## Sequential increments

Every increment includes its relevant UI states, failure handling, checks, and
documentation. Later integration testing does not defer those responsibilities.
Increments 1.1–1.6 are complete; later increments remain pending.

### 1.1 — Executable reference and evaluation protocol

**Completed:** [implementation/protocol](vio-reference.md) and
[qualification evidence](../results/vio-reference/README.md). The requirements
below are retained as the acceptance contract for future reference revisions.

**Dependency:** none beyond the existing repository and a usable development
environment.

**Deliverable:** a pinned, headless VIO execution path for the selected recording,
with a small dataset adapter if needed, sensor-only estimator inputs, normalized
outputs, a first run manifest, and a minimal independent evaluator implementing
the documented evaluation protocol.

- Verify the build, dependency and data provenance, configuration compatibility,
  license notices, and exact acquisition recipe. Keep large recordings out of Git.
- Define units, frames, quaternion order and rotation direction, time domains,
  camera/IMU offset, image conversion, stereo pairing, buffering, skipped samples,
  initialization, and end-of-stream handling. Distinguish noise densities from
  sample variances and record calibration assumptions.
- Specify timestamp association, valid intervals, initialization treatment,
  permitted gauge alignment, and trajectory-error metrics before examining the
  reserved sequence. Do not fit a free scale to conceal metric VIO scale errors.
  Report reference coverage, estimate coverage, and missing intervals separately.
- Implement the initial coverage/error calculation independently of the estimator.
  Verify it against known-error fixtures, including frame, time, and scale errors.
  Set and document numerical sanity criteria before accepting the reference;
  reproducibility alone cannot establish a correct integration.
- Export at least timestamped pose, initialization/status events, processed/skipped
  counts, and resource measurements. Inventory velocity, biases, covariance,
  feature tracks, and other diagnostics actually available through the adapter.
- Repeat execution to characterize reproducibility and measure elapsed time, peak
  memory, scratch space, artifact size, and relevant hardware/thread settings.

**Acceptance:** real stereo/IMU data produces inspectable estimates without
reference-based initialization and passes the declared coverage/error sanity
criteria. Checks establish input conventions, evaluator behavior, and reference
isolation. Repeats satisfy declared tolerances or expose documented variation.
The measured resource envelope and a meaningful configurable parameter support
the next increments. Failed qualification leads to a recorded reference revision,
not a placeholder trajectory presented as a result.

### 1.2 — Private hosted projects and initial interface

**Completed:** [application contract](hosted-workspace.md),
[deployment recipe](workspace-deployment.md), and
[validation evidence](../results/workspace/README.md). The requirements below
remain the access and persistence contract for subsequent increments.

**Dependency:** 1.1, including resource measurements and reference metadata.

**Deliverable:** a deployed staging application with authentication, persistent
private projects, and a reference-dataset catalog. The interface establishes the
project → dataset → experiments → results navigation and shows calibration,
reference coverage, provenance, and supported capabilities.

Use an established authentication/session integration. Enforce ownership at the
API and artifact-access layers, including images, logs, exports, and cancellation
as those operations arrive. Add per-account storage and computation budgets
before enabling public execution. Define retention and deletion behavior.

**Acceptance:** two accounts cannot read or modify each other's projects or
resources, including by substituting IDs or artifact paths. Sessions expire and
logout works. Project state survives refresh and service restart. The hosted
browser path works from a separate machine. Execution controls appear only when
connected to the later execution service; existing reference outputs are labeled
with their actual origin.

### 1.3 — Versioned experiments and configurations

**Completed:** [specification and API contract](versioned-experiments.md) and
[validation evidence](../results/experiments/README.md). The initial profile
supports the qualified full room1 recording and feature budgets 100, 200 and 400.
Increment 1.4 executes these saved revisions through durable hosted jobs.

**Dependency:** 1.2 and the qualified configuration contract from 1.1.

**Deliverable:** create, name, duplicate, and edit an experiment using the selected
estimator and dataset. Expose a small allowlist of useful numeric or enumerated
parameters, their units, defaults, and constraints. Record configuration changes
and preserve the exact immutable specification used by each execution.

Validate schema versions, types, bounds, supported combinations, and
dataset/calibration identity on the server. Build runtime configuration from
structured validated values.
Do not accept shell commands, arbitrary paths, fetch URLs, containers, or uploaded
source through this form.

**Acceptance:** invalid or unsupported configurations cannot create a runnable
specification. An edit creates a new version; previous results keep their original
configuration. A comparison can identify exactly which supported values changed.
The stored specification can be passed unchanged to the reference runner.

### 1.4 — Durable, isolated VIO computation

**Completed:** [execution contract](hosted-vio-jobs.md),
[deployment recipe](workspace-deployment.md) and
[validation evidence](../results/jobs/README.md). The requirements below remain
the execution and publication contract for subsequent increments.

**Dependency:** 1.3.

**Deliverable:** launch actual VIO jobs from the browser, observe queued/running/
evaluating/terminal states, cancel work, and revisit its outcome later. Persist
jobs and attempts with leases, heartbeat, bounded retries, idempotent submission,
and a rule allowing only the current attempt to publish a result.

Reuse the evaluator qualified in 1.1. Run the approved estimator separately from
the API with read-only sensor inputs, bounded CPU/memory/time/scratch/logs,
no runtime network access or service secrets,
and a non-root process. Give evaluation its own inputs and access to reference
poses. Apply per-account and global concurrency limits. Containers require
[explicit resource constraints](https://docs.docker.com/engine/containers/resource_constraints/);
default settings do not supply this budget. This setup covers a maintained
reference implementation, not a future arbitrary-code sandbox.

Validate output structure, timestamps, finiteness, conventions, and artifact
hashes before publishing success. Publish complete manifests atomically;
partial outputs remain identified as failed/incomplete. Cancellation must stop
child processes and prevent late result publication. Classify invalid input,
initialization failure, numerical failure, resource exhaustion, and infrastructure
failure rather than presenting every failure as a generic server error.

**Acceptance:** real jobs survive browser disconnects and are recovered or marked
explicitly after service/worker failure. Duplicate requests do not create
unintended duplicate work. Cancellation, timeouts, stale attempts, malformed
outputs, and quota enforcement have exercised failure paths. A zero process exit
code alone cannot establish success. Ground truth is absent from estimator inputs.

### 1.5 — Investigation view and traceable metrics

**Completed:** [investigation contract](vio-investigation.md) and
[validation evidence](../results/investigation/README.md). The native estimator
and qualified evaluation protocol are unchanged. Internal feature, bias,
covariance, innovation and tracking diagnostics remain explicitly unavailable;
the view reports observable output gaps and native initialization evidence.

**Dependency:** 1.4 and the protocol fixed in 1.1.

**Deliverable:** a synchronized investigation view linking images, IMU signals,
trajectory/attitude, initialization and tracking events, with available feature,
bias, or uncertainty diagnostics. Display frames, units, time basis, coverage,
configuration, and provenance close to the corresponding result. Unsupported
diagnostics are visibly unavailable.

Extend the initial evaluator and expose its trajectory metrics with association,
alignment, interval, and exclusion rules available to the engineer. Extend the
independent synthetic fixtures to cover every added metric and missing-data rule.
Compute metrics from full-resolution artifacts; any display downsampling must be
identified and must not change scores. Treat covariance
conversion, ordering, and availability explicitly when exposing uncertainty.

**Acceptance:** selecting an interval links the corresponding data across views;
failure and missing data remain visible. Metrics match the independent fixtures,
and their evaluated support is explicit. Host execution measurements are labeled
as such and do not imply embedded or real-time performance. The view remains
usable with representative full-recording sizes and failed/partial runs.

### 1.6 — Comparable configuration experiments

**Completed:** [comparison contract](vio-comparisons.md) and
[actual-run evidence](../results/comparisons/README.md), including local and HTTPS
validation. The requirements below remain the acceptance contract.

**Dependency:** 1.5.

**Deliverable:** execute and compare a baseline and a supported configuration
variant on the same recording under the same evaluation protocol. Show the
configuration difference beside trajectory error, coverage, failure status,
initialization behavior, and computation cost. Persist the comparison and its
exact run identities in the private project.

**Acceptance:** actual executions produce the compared values. Mismatched data,
calibration, intervals, or protocols are rejected as directly comparable or
explicitly separated. A run that drops difficult intervals cannot silently win
on the remaining samples. Repeat variation is visible where material. The
comparison supports a decision even when the variant is worse; no improvement
is required or invented. This increment is not an automatic optimizer.

### 1.7 — Independent reproduction and exports

**Dependency:** 1.6; the export contract has been maintained since 1.1.

**Deliverable:** download configuration, estimates, metrics, schema-versioned
manifests, evaluation rules, and a reproduction recipe identifying the pinned
runtime, adapter, source, dependencies, dataset acquisition/checksums, and required
attribution. Include
the source and notices needed by the chosen distribution arrangement. Exclude
credentials, internal filesystem paths, and expiring service-only URLs.

**Acceptance:** execute the exported experiment in a clean supported environment
outside the hosted backend, obtain the required public inputs, and reproduce its
results within the declared tolerances. No Meridian account or private backend
endpoint is required for reproduction. A documented container runtime or local
dependencies may be needed for this independent engineering use. Verify that
another account cannot obtain the export and that deletion revokes hosted access
according to the documented policy. Preserve schema versions and reject unsupported
export formats explicitly.

### 1.8 — Complete hosted acceptance

**Dependency:** 1.7.

**Deliverable:** a deployed, documented first VIO workspace with measured limits
and an acceptance report. Exercise the complete browser workflow from a separate
machine: sign in → create project → configure → execute → investigate → compare
→ export. Evaluate the reserved recording using the frozen protocol and report
its outcome, including failures and the limits of a two-sequence exercise.

Verify the deployed combination under the declared concurrent-user/job budget:
account separation, quotas, cancellation, worker recovery, failed outputs,
retention/deletion, and restoration of metadata plus artifacts from backup.
Record runtime/storage costs and supported resource limits. Check keyboard
navigation, readable units/statuses, loading/empty/error states, and whether a
user can complete the task without implementation instructions. External engineer
feedback is valuable when available; report who actually evaluated the workflow.

**Acceptance:** the complete task and independent export reproduction succeed on
the supported environment, with remaining limitations documented. Localhost-only
operation is insufficient for the hosted milestone. No flight, embedded,
universal robustness, or demonstrated market-value claim follows from this
acceptance. Record any deployment prerequisites still missing rather than marking
the part complete without a working hosted path.

## Decisions and evidence to carry forward

Increment 1 resolves execution feasibility, reference provenance, numerical
conventions, initial comparison settings, reproduction tolerances, and resource
needs. Increment 2 fixes the hosting/authentication/storage arrangement using
that evidence. The final acceptance report provides measured limits and concrete
requirements for user acquisitions in part 2; it does not predesign later parts.

Qualification may change the proposed implementation or dataset, but the outcome
remains a real VIO engineering workflow accessible through the hosted site.
No dates or throughput promises are assigned before the first measurements.
