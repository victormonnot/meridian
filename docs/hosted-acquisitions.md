# Private acquisitions and resumable uploads

Private projects can retain several sensor acquisitions. The Data page accepts
a Meridian transfer package, receives its archive in bounded chunks, and tracks
validation and preparation independently of the browser session. A successful
import preserves the exact archive, the validated acquisition and a separate
prepared snapshot. Each file remains restricted to the project's owner.

This is an input-management workflow. **Ready to inspect** means the declared
payloads passed structural and identity checks and the supported preparation
completed. It does not establish physical calibration, synchronization quality
or estimator compatibility. [Private-input execution](private-vio.md) separately
checks an exact prepared revision against its supported stereo/IMU profile.
The frozen public room1 reference remains available. No new benchmark reservation
or physical recording is consumed by this acquisition-management increment.

The [acceptance evidence](../results/acquisitions/README.md) records local and
HTTPS browser checks, import recovery, the existing room1 sandbox regression
and off-server restoration with authenticated ownership checks.

## Prepare a transfer package

The accepted archive contains `acquisition.json` at its root and exactly the
payload inventory declared by that descriptor. Supported containers are TAR,
gzip-compressed TAR and ZIP with stored or deflated members. Names, links,
special files, duplicate members, CSV values, image payloads and identities are
checked by the [local importer](sensor-import.md).

For TUM-VI or DataFlash, run the existing local adapter first, then archive the
resulting acquisition directory. Uploading an upstream TUM-VI archive or an
unconverted `.bin` file is not supported by this hosted profile. For example,
after generating `outputs/import/acquisition` with the local package command:

```sh
python - <<'PY'
import json
from pathlib import Path
import tarfile

root = Path("outputs/import/acquisition")
document = json.loads((root / "acquisition.json").read_text())
with tarfile.open("outputs/recording.tar", "x", format=tarfile.USTAR_FORMAT) as archive:
    for name in ["acquisition.json", *sorted(item["path"] for item in document["files"])]:
        archive.add(root / name, arcname=name, recursive=False)
PY
```

Use this with an already validated local acquisition. Another portable choice
is to create a ZIP with the descriptor and each exact relative path in its
`files` inventory. Do not add the outer local import report or
`original-transfer` to that inventory. The adapter's original-file payloads are
part of the acquisition and must remain included.

A reproducible synthetic transfer for acceptance checks is available without
any downloaded recording:

```sh
mkdir -p outputs/acquisition-check
python scripts/make-acquisition-fixture.py outputs/acquisition-check/synthetic.zip
```

The archive contains two tiny stereo pairs, three IMU observations, no reference
trajectory and 5 MiB of explicitly synthetic original-file padding. The padding
crosses a transfer-chunk boundary; it is not sensor evidence. `--snapshots`
selects the IMU-only example, and `--corrupt` deliberately creates a payload/hash
mismatch for rejection testing.

## Transfer and lifecycle

Create an acquisition from the project's Data view and select the archive.
Transfer progress counts bytes acknowledged by the service. Refreshing or
closing the page does not discard acknowledged chunks. To resume, select the
same file again: the browser hashes every acknowledged prefix chunk and compares
it with the stored identity before sending the remainder. A different prefix
is refused. No complete multi-gigabyte archive is loaded into browser memory.
Private acquisition content and transfer state are not stored in localStorage.

| State | Meaning |
| --- | --- |
| `receiving` | A reservation exists; acknowledged chunks can be resumed. |
| `validating` | Transfer is complete; an isolated import is queued or running. |
| `ready` | A complete acquisition and prepared snapshot were published together. |
| `rejected` | The package, preparation or resource budget failed. |
| `cancelled` | The user cancelled, or an incomplete transfer expired. |

Completing a transfer queues work; archive decoding and image processing never
run inside the HTTP request. A lease fences each attempt. Only a live current
attempt can publish; an expired attempt retains its compute slot until its
container is confirmed stopped. Infrastructure interruptions permit at most two
attempts. Import and VIO share one global active-computation slot.

The sandbox reads one immutable archive, writes into a dedicated finite scratch
filesystem, and receives no account secret, database, Docker socket or network.
After it exits, the trusted worker verifies and copies the publication into
durable staging. A rename completes the directory, and a fenced database update
makes its immutable version visible. Partial output is never a ready acquisition.

## Inspect the accepted observations

Open **Inspect sensor data** to examine [camera/IMU and timing evidence](sensor-inspection.md)
without an estimator run. New imports retain immutable inspection summaries,
stream chunks and grayscale previews within the existing publication budget.
Older acquisitions retain their exact version and may report inspection as not
generated; reimport the original package to create a new inspected version.

## Storage and resource budgets

The existing room1 regression measures approximately 1.707 GB for the upstream
archive, 3.407 GB for the adapted acquisition including originals and 1.700 GB
for the preserved-image preparation. These observations motivate a separate disk
scratch allocation; the existing 128 MiB VIO tmpfs remains unchanged.

| Resource | Hosted limit |
| --- | --- |
| Uploaded archive | 4 GiB |
| Transfer chunk | 4 MiB |
| Materialized acquisition | 4 GiB |
| Prepared snapshot | 2 GiB |
| Temporary filesystem | 8 GiB backing image, dedicated ext4 |
| Account / global retained storage and reservations | 12 GiB / 24 GiB |
| Account / global file reservations | 120,000 / 240,000 |
| Retained files per acquisition, including archive and publication marker | 60,000 |
| Acquisitions per project / account / service | 10 / 20 / 100 |
| Archive members / aggregate CSV observations | 30,000 / 100,000 |
| Pixels per image | 16,000,000 |
| Published relative path | 256 UTF-8 bytes, also bounded after JSON escaping |
| Import process | 1 CPU, 1 GiB memory, no swap, 64 processes |
| Import wall time / retained subprocess output | 900 seconds / 2 MiB |
| Incomplete transfer inactivity | 24 hours |

Admission reserves the declared archive size plus the maximum acquisition and
prepared sizes, and a conservative file budget. PostgreSQL serializes admission
and deletion with the shared queue lock. Ready publications replace the maximum
reservation with actual retained bytes and file counts; failed or deleted
inputs release reservations only after physical cleanup. A small archive can
therefore require substantially more reserved space while validation is pending.

The normal JSON API retains its 8 KiB body limit. Only the authenticated binary
chunk route permits larger bodies, with its own fixed bound and offset/hash
checks. The service additionally preserves a free-disk floor before processing.
Archive expansion, derived output and metadata have independent bounds;
compressed upload size alone is never treated as a storage estimate.

## Availability, deletion and recovery

Cancellation stops receiving immediately or requests cancellation of an active
attempt. Active imports and VIO jobs prevent project deletion. Recursive deletion
is deferred while any computation owns the shared slot, so a large cleanup cannot
block another computation's lease heartbeat. Retry a busy deletion once that
computation finishes; cancelled uploads retain their reservation until cleanup.
The worker first
stops the relevant sandbox before releasing storage or allowing another attempt.
Abandoned receiving uploads expire after 24 hours without new committed progress,
with physical cleanup on an idle worker sweep.

Deleting a ready acquisition removes its original and derived files together
and leaves a metadata tombstone with unavailable inputs. A new download cannot
open deleted data. A download already holding an open file descriptor may finish;
deletion cannot revoke bytes already delivered to a client. Descriptor/report
metadata remain traceable until the project itself is deleted.

[Supplied calibration and time mappings](sensor-calibration.md) now create
separate immutable input revisions. Source-file deletion is refused while a
queued or preparing revision requires those files. Completed revisions own their
prepared payloads and remain available after source-file deletion; artifact
bindings retain the source metadata. Project deletion also refuses active input
preparations and cleans completed revision files before deleting their metadata.
Existing public-reference experiments, retained results and
downloaded complete experiment packages remain unchanged. Private-input
execution requires an available exact input version; a retained descriptor alone
is insufficient. Copying a complete package outside the service preserves that
copy independently of later service-side deletion.

The [backup tool](workspace-backup.md) captures the database, retained originals,
prepared snapshots, calibration/time artifacts, input revisions and job artifacts
together while writers are stopped. It refuses active imports, input preparations
and incomplete transfers. Restoration checks both
database fingerprints and every retained file identity before clearing copied
sessions. Operator backups may contain subsequently deleted inputs until those
copies expire; the manual rehearsal does not establish scheduled backups or a
recurring retention service.

Original downloads stream stored bytes with the recorded SHA-256 in the response
header; the client can verify it after download. A size check is performed when
opening a retained file. Publication and backup restoration perform full hash
verification. Access to metadata, reports and every file resolves the owned
project/acquisition before accessing storage.
