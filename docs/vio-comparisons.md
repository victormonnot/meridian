# Saved VIO run comparisons

A comparison records two exact terminal executions in one private project: a
baseline and a candidate. It shows configuration differences beside state,
qualification, trajectory error, coverage, initialization and observed host cost.
The values come from those executions, never from substituted qualification runs.
The maintained profile remains OpenVINS on calibrated TUM-VI room1; `num_pts`
is the supported configuration change. No automatic optimization is performed.

[Private-input VIO](private-vio.md) jobs are explicitly ineligible for this
comparison profile. Controlled calibration and timing comparisons remain a
separate planned increment; incompatible inputs are not compared implicitly.

## Compatibility before arithmetic

Both saved specifications must validate against the supported immutable profile.
Dataset and reference identities, camera interval, calibration and masks, native
runtime and adapter, scheduling, coordinate conventions, resource limits and
evaluation protocol must agree. Only the supported feature target and its
declared rendered estimator-configuration identity may differ. The specification
records the qualified runtime identity; a host's equivalent OCI representation
does not replace that identity or create a new numerical configuration.

When both retained investigations exist, their exact camera timestamps, reference
support, coordinate frames, units, time basis, evaluator/builder/scorer identities
and shared reference/IMU identities must also agree. Trajectory and evaluation
output hashes remain specific to each run. A shared filename or dataset label is insufficient.
The API verifies ownership, retention, sizes and publication hashes before the
comparison code sees an artifact. Corrupted or modified evidence is rejected;
it is not silently replaced with a metadata-only comparison.

Failed and cancelled executions remain valid subjects of a comparison. Their
terminal state and failure remain visible. Structurally valid partial evidence
can supply support and errors, without turning a failed job into a success.
Active jobs must finish before a comparison can be saved.

## Own support and shared support

The first table shows each run's scores on its **own evaluated camera epochs**.
Coverage always uses all expected camera epochs in the selected interval,
including initialization, missing output and unsupported reference. A lower
error on a smaller set of samples is not automatically an improvement.

The second table uses the **intersection of evaluated camera epochs**. Let
`C = evaluated_baseline ∩ evaluated_candidate`. For each run separately:

```text
common_position_rmse = sqrt(sum(position_error_m[i]^2 for i in C) / len(C))
delta = candidate_metric - baseline_metric
```

Both means, maxima and RMSE values use the same epochs and equal sample weights.
Orientation errors remain quaternion rotation errors, reported in degrees and
radians; they are not differences of displayed Euler angles. All errors come
from full-resolution samples. No display reduction changes a score.

Each run retains its **own original full-recording global SE(3) alignment**.
Selecting an interval or intersecting support does not fit a new rotation,
translation, scale or time offset. Those original alignments can themselves
depend on different full-recording support. Common-support scores therefore
answer a bounded question; they do not establish performance on excluded epochs.
An empty intersection produces null scores and an explicit status, never zero.

Estimated and evaluated support are reported separately, each divided into
epochs present in both runs, only the baseline, only the candidate, or neither.
Contiguous regions retain exact camera indices and decimal nanosecond bounds.
No pose or error is interpolated across gaps. A candidate that drops difficult
epochs remains visibly incomplete even if its own-support error decreases.

The interface creates full-recording comparisons. The API also accepts an
explicit closed interval within the pinned recording. Both bounds must be
provided as decimal nanosecond strings. The interval is saved with the result;
changing it requires creating a new comparison.

## Initialization, cost and repetitions

Initialization delay is the first exported estimated camera epoch minus the
first expected epoch. It is distinct from the native estimator's internal
initialization event. A run without estimates has no measured delay.

Native wall time, CPU time, peak RSS and container wall time are retained as
observations from the execution host, with explicit units. Load, scheduling and
hardware are not controlled by saving a comparison. A smaller observed duration
does not establish an embedded speedup or a real-time bound.

Two distinct jobs with the same specification are labeled a repeated execution.
The view retains both observations and their signed differences. It does not
pool runs, compute confidence intervals or claim statistically established
repeatability from a pair. Failed qualification and differing support are shown
alongside the errors. No scalar score chooses a winner.

## Persistent snapshot and source availability

Creation saves an immutable, schema-versioned comparison in PostgreSQL, with the
two run UUIDs, specification snapshots and hashes, terminal outcomes, available
evidence identities, comparison/scorer source identities, interval and derived
metrics. A canonical SHA-256 identifies the saved analysis. Reads return this
snapshot rather than recomputing it with a newer implementation.

Raw images, IMU samples and pose trajectories are not copied into the comparison.
Retained investigation files still follow the existing seven-day file policy.
If files have expired or an older job has no investigation, saved full-run scalar
results can still be compared as recorded metadata. Selected-interval and
common-support scores remain unavailable, with explicit reasons; full-run scores
are not substituted for them.

Deleting a source run or experiment leaves a saved comparison intact. Its run
identities remain part of its provenance, while current source and investigation
links become unavailable. Delete the comparison itself to remove that snapshot;
deleting its project or account removes it as well. No comparison is shared
between accounts. Every operation checks both project and comparison ownership.

Creation is idempotent within a project: a retry with the same UUID key and exact
normalized request returns the same saved comparison, including after source
deletion. Reusing that key for changed inputs returns a conflict. Project row
locking serializes concurrent creates and enforces the limit of 20 comparisons
per project. A serialized derived analysis is bounded to 2 MiB. No execution or
new background job is launched by creating a comparison.

## HTTP contract

Routes are relative to `/api/v1/projects/{project_id}` and use the existing
session authentication, CSRF and `Cache-Control: no-store` policies.

| Method and path | Behavior |
| --- | --- |
| `GET /comparisons` | Compact saved summaries and the project limit. |
| `POST /comparisons` | Validate two owned terminal jobs and save their immutable analysis. |
| `GET /comparisons/{comparison_id}` | Saved analysis and current source-access availability. |
| `DELETE /comparisons/{comparison_id}` | Remove only that comparison. |

Creation requires `schema_version: 1`, `name`, `baseline_job_id`,
`candidate_job_id` and `idempotency_key`. `description` is optional; `start_ns`
and `end_ns` are optional as a pair. Names contain 1–100 characters and
descriptions at most 2,000. Unknown fields, query parameters and update methods
are rejected. Missing or foreign resources return `404`; incompatible runs,
active runs, quota exhaustion and changed idempotent requests return `409`.
Unavailable or tampered published evidence returns `503` without host paths.

The additive migration creates only the comparison table. The API uses a
standard-library comparison module; no new numerical dependency, raw-data mount,
estimator change or worker execution mode is needed. See the
[deployment guide](workspace-deployment.md).

The [validation report](../results/comparisons/README.md) distinguishes synthetic
failure/support fixtures from actual native baseline, variant and repeat runs.
This handheld recording does not establish onboard or flight performance.
Independent reproduction downloads remain a separate increment.
