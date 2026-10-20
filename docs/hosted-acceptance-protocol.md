# Hosted workspace acceptance protocol

Protocol version 1 is fixed before the increment 1.8 acceptance executions.
The starting implementation is commit
`50ac01c388d020102ec94e402307d5ed0071b62c`. Record the exact final source,
application images, native image, dataset manifests and environment with the
results. Fixes discovered during acceptance must be identified and the affected
checks repeated; an interrupted or failed attempt must not disappear from the
record.

## Scope

Qualify the maintained room1 engineering workflow over HTTPS from a Linux browser
client on a different physical machine from the VPS. The evaluator is an
automated engineering harness; do not describe this as feedback from an external
drone engineer or an independent usability study.

The separate [room2 protocol](room2-acceptance.md) fixes three baseline executions
on the previously reserved recording. Room2 is an offline acceptance case, not a
new selectable hosted profile. No estimator tuning, user recording upload,
arbitrary code execution or new flight-control integration is introduced here.

## Complete browser workflow

Use newly created disposable accounts and actual computation:

1. Register, sign out and sign in; create a private project and inspect recording
   provenance and supported configuration choices.
2. Save the default 200-point configuration and launch that exact revision.
   A second account submits its own baseline while the first is active.
3. Exercise the two-active-jobs-per-account boundary with one additional queued
   request; reject the next request with `409 queue_full`, then cancel the spare.
   Both accounts' real baseline jobs must finish separately.
4. Inspect original stereo images, IMU, trajectory, units, coverage and interval
   scores on the first successful result. Confirm the saved specification and
   full-recording alignment remain unchanged.
5. Save a new 100-point revision, execute it and compare the exact baseline and
   candidate jobs. Retain both own-support and common-support outcomes without
   inventing a winner or requiring an accuracy improvement.
6. Download the candidate experiment, verify its actual byte length and SHA-256,
   exercise an interrupted download and retry, and retain the exact archive for
   independent reproduction.
7. Exercise refresh/deep links, keyboard controls, desktop and 390-pixel layouts,
   light/dark themes, logout and second-account denial of private resources.

Do not fabricate scientific responses or execution states. Deliberate transport
interruption and request delays must be labelled. Retain raw response evidence,
download headers and unchanged screenshots. Record actual browser/CDP limitations.

## Operational boundaries

The declared service limits remain one global computation, two active jobs per
account, eight globally, 20 retained jobs per account and 200 globally, and
10/100 accepted submissions per account/globally in fixed 24-hour windows.
Native and evaluator limits remain those in the [job contract](hosted-vio-jobs.md).

| Case | Required observation |
| --- | --- |
| Two interactive accounts | Separate owned results; one observed native execution at a time, with queued waiting visible. |
| Eight queued jobs | With the worker temporarily stopped and only disposable acceptance jobs present, admit eight across five test accounts; a ninth request from an account below its own active limit receives `409 queue_full`. Cancel these requests before restarting. |
| Daily account submission budget | Reach ten accepted requests using the same disposable account; cancelled/deleted jobs do not reset the budget. The next new request returns `429 submission_limit` and `Retry-After`; replaying an existing idempotency key remains the same job. |
| Retained/global budget boundaries | Real PostgreSQL HTTP-boundary tests exercise exact limits with explicitly synthetic database fixtures. Do not mislabel these as 200 real executions or a 100-job load test. |
| Active cancellation | Cancel a real running native job; confirm terminal cancellation, child removal, no late success and no complete export. |
| Worker interruption | Interrupt the worker only while its current job belongs to the acceptance account. Restart it, observe lease recovery, confirm old children are absent before the retry, and require a second successful attempt with unchanged specification. |
| Failed native execution | Deliberately interrupt only a labelled native child belonging to the acceptance account. Require a failed job with an explicit failure code, no fabricated successful metrics, no complete export and no unintended retry. |
| Retained evidence integrity | Temporarily damage only a retained artifact of an owned acceptance job; reads must fail integrity checks. Restore the exact original bytes and reverify the manifest. |
| Expiration and deletion | Move only an acceptance job's expiration into the past and observe access revocation and worker collection while metric metadata remains. Label this as accelerated expiration, not seven days of elapsed operation. Explicit deletion revokes the corresponding resource. |

Operational fault injection is an administrator exercise, not an application
feature. Refuse to interrupt a service while unrelated work is active or queued.
Keep fault logs and restore service availability on every exit path. Never
change global host cleanup, other applications, the native runtime pins or
submission counters to make a check pass.

## Backup and restoration

After the retained browser jobs are terminal and the queue is empty, create a
consistent private backup of PostgreSQL metadata and all retained published
artifacts. Briefly stop only the exact API and worker services if needed for the
snapshot, recheck that the queue is idle, and restart only those services that
were running. Record downtime and database/container preservation.

The backup contains private account data, password hashes and potentially
session records. Keep it outside the public repository with restrictive
permissions. Application secrets, source recordings, qualified images and the
source release are separate recovery prerequisites, not silently reconstructed.

Copy the backup to the existing authorized development machine over the
authenticated administrative channel. Restore it into a new isolated
PostgreSQL instance and a new artifact directory, never into the live database.
Verify the inventory, original artifact bytes, saved specifications, comparisons,
project ownership and export access. Remove copied sessions; authenticate only
the disposable acceptance account for restored access checks.

Keep the original and restored identities in the report. An isolated successful
restore establishes the tested recovery procedure; it does not establish a
scheduled backup service, measured disaster-recovery time or an off-site
retention policy.

## Independent replay and acceptance record

Execute the browser-downloaded archive outside the hosted backend using its
included recipe, separately verified public inputs and the declared supported
Linux/Python/Docker environment. Record whether an existing independently
prepared dataset or source-built image is reused. The source execution must be
deleted before this replay; downloaded evidence remains available for audit.
Apply the [export tolerances](vio-exports.md) without refitting the direct poses.

The report must distinguish automated fixtures, actual browser requests, actual
native runs, operator-induced failures and independent replay. Record job queue
waiting, native/evaluation elapsed time, host memory/storage observations,
cleanup, preserved existing data, and remaining unavailable capabilities.
Container limits and a few observed timings are not sustained-throughput
guarantees.

Part 1 requires a working hosted engineering path and its independent export,
the reserved-recording outcome without hidden tuning, and the stated operational
and restoration checks. A room2 accuracy-gate failure remains an explicit result
and limitation; it cannot be repaired by changing the gate after observation.
No onboard, real-time, flight, universal robustness or market-value claim follows.
