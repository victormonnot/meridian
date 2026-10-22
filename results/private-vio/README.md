# Private-input VIO qualification

This regression exercises the `openvins-private-stereo-v1` profile on an actual
uploaded camera/IMU recording, with fixed supplied calibration and optional
independent reference evaluation. The [implementation guide](../../docs/private-vio.md)
describes admission, execution, investigation and retention boundaries.

## Predeclared input and policy

The input is the first 30 seconds of the already consumed TUM-VI room1 recording,
selected before observing this profile's output. It contains 600 stereo pairs,
5,973 IMU samples and 3,596 reference poses including adjacent bracketing samples.
The [input manifest](input.json) records the pinned original archive, exact crop,
package/calibration hashes, attribution and preparation choices. Two generations
produced identical packages; the normal hosted-package importer accepted the ZIP.

Both variants use 200 features, supplied camera geometry and continuous IMU
noise, zero runtime time offset, disabled online calibration refinement, disabled
image masks and the profile's declared gravity magnitude of 9.80766 m/s². The
prepared sensor payloads are identical. Only reference inclusion differs.
No calibration was fitted, no new reserved recording was opened and no setting
was changed in response to the numerical result.

## Numerical results

The local and HTTPS runs produced the same reported numerical aggregates. In
each environment, the two variants also produced byte-identical trajectory CSVs.

| Observation | Reference included | Reference excluded |
| --- | ---: | ---: |
| Native execution | Succeeded | Succeeded |
| Expected camera epochs | 600 | 600 |
| Exported estimates | 468 (78%) | 468 (78%) |
| Missing initial epochs | 132 | 132 |
| Missing interior/trailing epochs | 0 / 0 | 0 / 0 |
| Initialization delay | 6.600339685 s | 6.600339685 s |
| Reference-supported epochs | 600 | 0 |
| Evaluated estimates | 468 | 0 |
| Aligned position RMSE | 0.0130230620 m | Unavailable |
| Aligned orientation RMSE | 0.622379916° | Unavailable |
| Physical-accuracy qualification | Not assessed | Not assessed |

Errors use the unchanged full-selected-interval SE(3) alignment, fixed scale,
20 ms maximum reference bracket and no fitted time offset. The reference covers
all camera epochs, but the reported errors cover only the 468 exported estimates.
Without reference, the investigation shows the raw estimator trajectory and
attitude in its own local frame, with exact camera images and prepared IMU data.
It reports null accuracy metrics rather than zero errors.

These runs check a software and data contract on a consumed benchmark. They do
not establish held-out performance, physical calibration validity, onboard
operation, real-time capability or flight readiness.

## Validation and observed failures

The [aggregate evidence](qualification.json) records 1,831 Python tests with C++
enabled, one CTest, 257 PostgreSQL tests on the host and in the exact minimal API
image, and 298 web tests with a production build. Focused cases cover partial
reference support, incompatible frames/clocks, exact timestamps above 2⁵³ ns,
immutable input identities, metadata tampering and retained numerical evidence
after input-file deletion.

The local private browser recipe passed 73 assertions across its documented
resume chain. A fresh HTTPS recipe passed 65 without retries. Both exercised
actual upload, supplied calibration, preparation, two real VIO jobs, exact image
hashes, interval controls, refresh persistence, owner/peer/anonymous boundaries
and explicit refusal of unsupported comparisons/exports. Twelve screenshots
covering dark/light/mobile views were inspected. The existing workspace passed
49 browser checks locally and 49 over HTTPS. The historical full room1 job also
passed its unchanged accuracy/coverage gates and 22 local job-workflow checks.
Its different interval and calibration policy prevent a direct accuracy
comparison with this 30-second private-input regression.

The first local private job failed before the estimator process started: Docker
could not create nested image mountpoints under the read-only sensor mount.
Creating the two empty staging destinations fixed it without changing input
bytes or numerical settings. The old failure remains recorded separately.
Harness corrections also handled asynchronous form completion, wrapped select
labels and the existing refusal of a derived revision with no artifact selected.

Eight local operational checks cover cancellation of a paused native child and
fenced recovery after worker interruption. The operator explicitly restarted the
worker after a manual KILL; automatic restart is not claimed. Expired attempts
could neither renew their lease nor publish over the retry, and the two main
results/input identities remained unchanged. Native/evaluation mounts were
observed separately; the native child received no reference or service secrets.

## Deployment and restoration

The deployed release contains 145 verified source files. The PostgreSQL
container, image, mounts and start time were preserved, and all five application
services recovered healthy after each backup. The historical native runtime and
prepared room1 pins remain unchanged.

| Recovery check | Local snapshot | HTTPS snapshot transferred off server |
| --- | ---: | ---: |
| Verified inventory files | 9,808 | 6,116 |
| Inventory bytes | 2,911,427,611 | 1,801,456,717 |
| Write pause | 81.46 s | 41.55 s |
| Copied sessions cleared | 28 | 3 |
| Restored API access/integrity checks | 71 | 71 |

Each recovery used isolated PostgreSQL and the source API image, with copied
inputs/results mounted read-only. Checks used real password and CSRF login,
owner/peer/anonymous requests, both exact jobs and input revisions, and the
supplied calibration artifact. Both full investigation responses matched their
source bytes. Two sampled PNGs and four prepared CSV reads per environment
matched their immutable hashes; the backup verifier separately checked the
complete inventory. This is an API recovery drill, without a restored browser,
TLS replay or estimator rerun.

The seven local and four HTTPS QA accounts and their recorded inputs/jobs were
removed after successful recovery. Preexisting rows and authentication budgets
were preserved. Input and job artifact stores returned to their baseline, with
all five services healthy and unrelated hosted applications unchanged. Disposable
restore databases, backup payload copies and the unused transferred API image
were then removed; private manifests, reports and logs were retained.

## Reproduction

Use the existing verified room1 import from the [import regression](../sensor-import/README.md):

```sh
python scripts/make-private-vio-fixture.py imported-room1 new-private-vio-fixture
```

The generated ZIP and calibration descriptor can be uploaded through Data and
Calibration & timing. Prepare two revisions with reference inclusion enabled and
disabled, select each in a private experiment, check the full prepared interval
and run its saved version with 200 features.

The [browser harness](../../web/scripts/check-workspace-private-vio-browser.mjs)
performs this workflow against an explicitly selected deployment. It creates
ordinary QA accounts and retains exact source/result identities for restoration
and scoped cleanup. Credentials and raw recording payloads are not included in
this public report. Portable private-input exports remain planned; operator
backups are a separate private recovery mechanism.
