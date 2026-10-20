# Repeating the hosted acceptance exercises

The [frozen acceptance protocol](hosted-acceptance-protocol.md) separates the
hosted engineering workflow, operational behavior and numerical qualification.
The [room2 protocol](room2-acceptance.md) fixes the reserved-recording evaluation.
Results must retain failed attempts and identify any correction before a repeat.

## Browser workflow

[`check-workspace-acceptance-browser.mjs`](../web/scripts/check-workspace-acceptance-browser.mjs)
drives real registration, projects, configuration revisions, computations,
investigation, comparison and export. It writes its checklist before the first
registration. The browser submits real jobs; no estimator responses are mocked.
Its evidence includes transport interruptions deliberately introduced to check
download recovery. Credentials remain in process memory and are never written.
Keep the raw evidence private.

Use an installed compatible Playwright and Chromium browser:

```sh
MERIDIAN_PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
MERIDIAN_BROWSER_EXECUTABLE=/path/to/chromium \
MERIDIAN_QA_ENVIRONMENT="Describe the physical client and server machines" \
node web/scripts/check-workspace-acceptance-browser.mjs \
  https://meridian.example.net/ /private/meridian-acceptance/browser
```

Run against an idle deployment with room1
prepared inputs and the pinned native runtime available. Use a new output
directory for every invocation, including retries, and retain previous evidence:
the harness can overwrite its report and checklist in an existing directory.
The complete workflow
admits four requests: three successful computations and one queued cancellation.
Retain these accounts, jobs, comparison and exact ZIP through backup,
restoration and restored-access verification. Then delete the original source
job and verify its URL is unavailable before independent reproduction. Keep the
archives and raw evidence through that reproduction. If the browser session is
gone, an operator may reset only the explicitly identified disposable QA
account's password after the snapshot to perform the authenticated deletion;
record this separate operator step.

The script is an automated engineering exercise, not an external usability study.
Screenshots and viewport checks do not establish accessibility conformance.
The HTTPS run must use a client on a different machine from the service.

## Operator faults and admission boundaries

[`qualify-workspace-operations.py`](../scripts/qualify-workspace-operations.py)
runs on the Docker host controlling the explicitly named Meridian Compose
project. It needs a healthy, idle deployment and permission to interrupt its
API/worker job infrastructure. It creates five disposable accounts and binds
their HTTP identities to the selected Compose database before fault injection.

```sh
umask 077
python3 scripts/qualify-workspace-operations.py \
  --base-url https://meridian.example.net \
  --project meridian-workspace \
  --output /private/meridian-acceptance/operations
```

The origin must already route to this deployment. The output directory must be
new. Coordinate exclusive access: the harness refuses unrelated active or queued
jobs, but this is not a maintenance lock against a concurrent operator or a new
visitor. Its report and downloaded archive contain private resource identifiers.
Do not publish them wholesale.

The exercise interrupts its own running worker, observes lease recovery, cancels
another active job and kills another owned native child. It damages one owned
retained trajectory temporarily, restores the exact bytes in a `finally` block,
and advances that job's expiry to check access revocation and collection.
It stops the worker briefly to fill eight queued slots across five accounts,
checks the next admission is rejected, cancels these queued jobs and restarts
the worker. It also reaches one account's ten-submission daily budget, including
cancelled and deleted requests, and checks idempotent replay.

Global submission counters are retained. The exercise removes its own accounts,
jobs and artifact directories and checks the initial application record counts.
If interrupted or cleanup fails, retain its report, inspect the exact listed
accounts/containers and restore service availability before retrying. Never
remove unrelated work or reset global budgets to make a retry pass.

The real PostgreSQL tests in
[`test_acceptance_limits.py`](../server/workspace/test_acceptance_limits.py)
check the 20/200 retained-job boundaries, eight global active jobs and 100 daily
global submissions with explicitly synthetic fixtures. They do not represent
200 actual computations or a throughput/load test.

## Backup and independent replay

Follow the [private backup and restoration procedure](workspace-backup.md)
after the browser jobs are terminal and the queue is empty. Copy the backup
off the server over an authenticated private channel; restore into a new
isolated database and a separate artifact directory. Verify account separation,
the saved comparison and the exact export bytes using the matching application
image. Record whether access checks used actual network requests or Django's
in-process HTTP client.

After capturing both the downloaded archive and the restored copy, delete the
original disposable source job and verify its original URL is unavailable.
Reproduce the exported experiment outside the service using the
[export contract](vio-exports.md), then independently audit the input identities,
trajectory/evaluation artifacts and numerical comparison. State explicitly any
reuse of a previously built runtime, prepared inputs or dependency environment.
One successful recovery rehearsal does not establish scheduled backups or a
disaster-recovery service objective.
