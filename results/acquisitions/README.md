# Private acquisition validation

Increment 2.3 adds owned sensor acquisitions, resumable transfers and isolated
preparation to the hosted workspace. This report records the implementation
checks performed on 20 October 2026. It evaluates input handling and recovery;
it does not measure estimation accuracy or establish calibration quality.

## Browser and API behavior

The same synthetic Meridian package was exercised against local Compose and
the canonical HTTPS deployment. Each acquisition browser run passed 33 checks;
the existing account/project/experiment workflow passed 49 checks in each
environment. The web suite passed 252 tests and its production build completed.
The numerical/import/backup suite passed 1,736 tests with the C++ executable,
and the server suite passed 193 tests on PostgreSQL 17. The server suite also
passed inside the API image with only server dependencies and no Pillow.

The transfer fixture is 5,250,234 bytes, crosses the 4 MiB chunk boundary and
contains analytic stereo/IMU observations with explicit synthetic padding.
Browser routing interrupts the second request after the first chunk has been
committed. Refresh restores the acknowledged offset; a wrong same-size prefix
is refused, and resuming the correct file sends only the missing suffix. The
downloaded original matches the complete source SHA-256.

The checks exercise corrupted payload rejection, durable state after refresh,
cancellation, confirmed deletion, unavailable tombstones, logout, another
account's denied metadata and file access, narrow-screen layout, and the absence
of uncaught browser or Content-Security-Policy errors. A previously downloaded
archive remains readable after service-side deletion. Ready inputs explicitly
report that calibration and VIO readiness have not been assessed.

See [browser evidence](browser.json) and [automated validation](validation.json).

## Cancellation and recovery

A separate local HTTP qualification uploads a 67,116,243-byte synthetic package
and pauses its exact owned import container. Cancelling that active acquisition
stops the child, publishes nothing and releases the storage reservation. While
the import holds the shared slot, an owned frozen-reference VIO job remains
queued with zero attempts; it is cancelled before import completion.

For a second acquisition, the importer is killed while its owned child is
paused. After the real 30-second lease expires, recovery removes the old child
and only the second attempt publishes. The recovered original download matches
the submitted byte count and SHA-256. All 31 captured queue observations show
at most one active global computation. The importer recovers health and the
qualification's exact temporary records and scratch files are removed.

These are induced faults and sampled observations, not an availability or
throughput guarantee. No VIO estimator executes in this check. See
[operational evidence](operations-local.json).

## Existing room1 under the import sandbox budget

The already-consumed room1 acquisition from increment 2.2 was packaged as a
3,411,189,760-byte Meridian TAR and processed in the restricted import child:
1 CPU, 1 GiB memory, no swap or network, and a dedicated 8 GiB disk scratch.
Import plus preparation completed in 130.57 seconds, producing 11,299 files
and 5,106,570,899 bytes. Both cameras retain 2,821 images; the IMU retains 28,122
observations and the independent reference retains 16,541 poses. The acquisition
identity matches the preceding import regression.

This is a local sandbox qualification, not a multi-gigabyte HTTPS transfer or
an estimator run. The prepared descriptor uses an explicit qualification ID,
so its identity differs from the earlier descriptor. The observed cgroup memory
peak reached 1 GiB and includes file cache; it is not process RSS. The supervisor
observed no OOM event and the child completed successfully. The evidence names
the measured image separately from the final deployment image. No reserved
recording was opened. See [room1 evidence](room1-sandbox.json).

## Deployment and coherent restoration

The hosted release preserves the existing PostgreSQL container, image, storage
mounts and start time. An additive migration introduces acquisition metadata.
The API, web, VIO worker, import worker and database are healthy; the import
scratch is a persistent, separate ext4 mount backed by an allocated 8 GiB image.
The existing 128 MiB VIO scratch and qualified native runtime remain unchanged.
Published Python sources were compared with the running API and workers.

The backup rehearsal stops all three writers and includes the database,
originals, acquisition payloads, prepared snapshots and publication markers.
Local and hosted snapshots are restored into fresh, isolated PostgreSQL
containers. The hosted snapshot is transferred off server and verified before
restoration; the exact hosted API image is transferred and its configuration
and layers checked for the restored access test.

Each restored-access run passes 34 checks using real password/CSRF login with
the ordinary application database role. Copied sessions are cleared, anonymous
requests return 401 and another restored account receives 404 for the owned
metadata and downloads. The owner downloads the exact original and three
prepared CSV files, checking their bytes and SHA-256 values. These restored
access checks use the Django in-process client; they do not claim a second
browser/TLS deployment of the restored application.

See [deployment and recovery evidence](recovery.json). The tested snapshots
contain synthetic inputs and small databases; this rehearsal does not measure
restoration at the global storage quota or provide scheduled backup/retention.
Source deletion does not erase copies already downloaded or retained in an
operator backup. The exact temporary QA accounts, snapshot payloads and restore
containers were removed after verification; existing workspace records and
authentication/submission budgets were preserved.

## Reproduce

Follow the [deployment guide](../../docs/workspace-deployment.md) to provision
the bounded scratch and enable the acquisition overlay, then create both
fixtures and run the maintained browser check:

```sh
mkdir -p outputs/acquisition-check
python scripts/make-acquisition-fixture.py outputs/acquisition-check/valid.zip
python scripts/make-acquisition-fixture.py outputs/acquisition-check/corrupt.zip --corrupt
node web/scripts/check-workspace-acquisitions-browser.mjs \
  http://127.0.0.1:8090/workspace.html outputs/acquisition-check/browser \
  outputs/acquisition-check/valid.zip outputs/acquisition-check/corrupt.zip
```

Playwright and Chromium must be installed as described in the
[workspace guide](../../docs/hosted-workspace.md). `MERIDIAN_PLAYWRIGHT_MODULE`
can select an existing Playwright module. The browser report identifies the
exact temporary accounts and retained acquisition for the subsequent
[backup/restore procedure](../../docs/workspace-backup.md) and operator cleanup.
Do not publish private snapshots, credentials, sessions or payload inventories.

The induced-fault check requires an operator to target only its exact labelled
child and importer on an idle disposable deployment. The Python/PostgreSQL
suites cover additional quota races, invalid archives, offset/hash failures,
expired leases, publication fencing and backup bounds. The
[acquisition contract](../../docs/hosted-acquisitions.md) documents the supported
profile and current limits. Private-input VIO execution remains increment 2.6;
sensor-quality investigation, calibration and timing are subsequent work.
