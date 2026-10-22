# Private workspace backup and recovery rehearsal

The operator tool [`backup-workspace.py`](../deploy/workspace/backup-workspace.py)
captures PostgreSQL metadata together with the published files that metadata
references. It also verifies the copy and restores it into a **new, isolated,
disposable PostgreSQL container**. It never selects an existing restore target or
replaces production data.

An experiment export is a different deliverable: it reproduces one successful
execution without an account. A workspace backup contains accounts, password
hashes, session records, private project names, authentication/submission counters,
experiment revisions, jobs, saved comparisons, acquisition records and upload
chunk identities. Version 3 adds supplied calibration/time artifact metadata and
immutable input revision publications to the retained originals and prepared
inputs introduced by version 2. Keep it private. The script
creates mode 0700 directories and mode 0600 files, requires copies to remain
owner-only, and rejects links and unexpected files. These permissions and hashes do not encrypt or authenticate
a backup. Store off-server copies through an authenticated private channel and
restrict access and retention separately.

## Capture an idle deployment

The supported source is the dedicated Meridian Compose project on Linux with
Docker access. Use the same release's operator tool. The database image must be
available locally. Run as an operator who can read the private artifact tree and
control the exact API, VIO worker, importer and database containers. The output
directory must not already exist.

```sh
umask 077
python3 deploy/workspace/backup-workspace.py backup \
  --project meridian-workspace \
  --artifacts /srv/meridian-workspace/jobs \
  --acquisitions /srv/meridian-workspace/acquisitions \
  --output /srv/meridian-backups/2026-10-20
python3 deploy/workspace/backup-workspace.py verify \
  --backup /srv/meridian-backups/2026-10-20
```

Create and protect the parent directory before running these commands. Coordinate
a short maintenance window: requests cannot complete while the API is stopped.
The tool refuses queued/running/evaluating jobs, receiving/validating acquisitions,
outstanding import attempts, queued/preparing input revisions and any labelled
numerical, import or revision child container.
Finish or cancel interrupted transfers before capture; this snapshot does not
preserve resumable partial uploads. It validates actual Compose labels and both
artifact/acquisition mounts, stops only the
API and workers that were running, then checks that the queue remains empty.
PostgreSQL and Nginx keep running. A custom-format `pg_dump` and exact copies of
published attempt files, retained original archives and derived acquisition
files are captured while application writers are stopped. The acquisition root
is required for current releases even when empty.
Database content fingerprints are checked before and after copying. A `finally`
block restarts precisely the previously running API/workers, including on failure.
Operators must check their health afterward; a failed restart is reported.

The completed manifest records PostgreSQL and application image IDs, migrations,
counts and row-content hashes for account/workspace tables, publication manifests,
file sizes/hashes and the observed pause duration. Artifact and input revision
tables participate in the same row-content fingerprints. The database dump includes the
whole application database. Artifact limits are 32 MiB per file, 512 MiB for the
dump, 16,384 job artifact files and 7 GiB of job artifacts. Acquisition bounds
are 4 GiB per original/import file and 2 GiB per revision file, 240,000 published
files plus at most 200 original/publication records, and 24 GiB total across
acquisitions and revisions. The complete backup is limited to 32 GiB with a
256 MiB UTF-8 manifest. Publication marker serialization remains unchanged.
Version 2 fingerprints tables from sorted per-row PostgreSQL SHA-256 digests,
so fingerprinting does not aggregate a complete table's JSON in memory.
A failed capture leaves an incomplete
private directory without a completed manifest; do not treat it as a backup.
The helper does not install cron tasks or modify other applications.

The PostgreSQL reference is pinned to its distribution digest and Linux amd64.
The helper verifies that digest in Docker's `RepoDigests` and records the actual
resolved image ID. Classic and containerd Docker stores can expose different IDs
for the same pinned distribution. Restoration resolves the same qualified
reference on the destination, verifies its digest/platform and executes that
exact local ID, while retaining both source and destination IDs in the report.

Only files referenced by retained publications are copied, including each
acquisition's exact uploaded archive, its prepared snapshot and each retained
input revision's independent publication. Deleted inputs,
partial transfers, abandoned staging directories and scratch are excluded.
Version 1 and 2 backups remain verifiable and restorable with their original scope.
The shared public reference recording, bounded scratch,
native/runtime images, application source releases, environment secrets,
database roles and passwords are separate recovery prerequisites. Keep the exact
release and immutable images or reproducible build inputs, verified prepared
dataset manifest and protected deployment configuration. A source checkout alone
cannot reconstruct these secrets. Never publish a backup or its private manifest.

## Transfer and verify

Copy the entire directory with a trusted SSH transport or another approved
private backup channel, preserving restrictive modes. Independently compare its
manifest SHA-256 before and after the transfer, then run `verify` at the
destination. The verifier checks every inventory entry against its recorded hash
and binds restored artifact identities back to the database publication
manifests. It rejects missing/extra files, symbolic/hard links and non-private
members. Do not extract an untrusted archive over a live deployment.

## Rehearse restoration without touching production

On a separate Linux Docker host with the pinned PostgreSQL image already present:

```sh
python3 deploy/workspace/backup-workspace.py restore \
  --backup /private/offserver/2026-10-20 \
  --output /private/rehearsals/2026-10-20 \
  --name meridian-restore-20261020
```

The destination and container name must both be unused. The helper creates a
network-none container with no published ports, a read-only root filesystem,
512 MiB memory/0.5 CPU limits and 256 MiB of disposable database tmpfs by default.
Use `--database-mib` to size that disposable store explicitly from 256 to 8192 MiB;
its memory limit becomes the larger of 512 MiB and the store size plus 256 MiB.
Version 2 captures the measured source database size and refuses a restore store
smaller than twice that size plus 64 MiB, before creating the output or container.
This is a capacity check, not a universal upper bound on PostgreSQL restore growth;
choose a destination with enough available memory. The 512 MiB dump limit remains
an explicit backup-profile bound. It uses a
new bootstrap administrator and a separate restricted application role, each
with a distinct random password. The helper waits for the final TCP server,
not the entrypoint's temporary initialization server. `pg_restore` runs
transactionally as the application owner with no source role restoration.
Counts, migrations, row-content fingerprints and
published artifact and private acquisition hashes must match before the rehearsal succeeds. Copied
sessions are then deleted, and the application role's actual PostgreSQL flags
are checked: non-superuser/no database creation/no role creation/no replication.

The generated password remains in the private Docker container configuration;
the temporary environment file is removed. The restore report is private.
The new database's data disappear when its container is removed; this is a
recovery **rehearsal**, not a replacement persistent production deployment.
The separate restored artifact and acquisition directories remain until explicitly removed.

Exercise the restored database using the matching API image and a new temporary
Django secret: verify signed-out rejection, owner access, other-account
rejection, saved comparisons, actual experiment download integrity, acquisition
metadata and original/prepared file downloads. Copied
sessions must not become valid sessions. Use an existing consenting QA account
password or reset only an identified disposable account on the restored copy.
If using Django's in-process test client, report that accurately; it does not
replace the browser/TLS acceptance. Mount only the restored artifacts and private
acquisition tree read-only
and use an isolated network namespace for this check. Prepared images are a
separate prerequisite for visual investigation; exports do not require copying
the shared public reference recording into the API. Private acquisition download
routes resolve ownership before opening a file in their separate private mount.

For an actual disaster recovery, provision new persistent storage and restricted
database roles from the deployment runbook, restore the verified dump/artifacts,
restore compatible images and private configuration, and clear old sessions before
enabling traffic. Preserve original artifact expiry times; restoring a backup
must not silently renew retention. Run the full access and reproduction checks,
then switch only Meridian's routing after review. This script deliberately does
not automate an overwrite or traffic switch.

Remove only the explicit rehearsal container when finished:

```sh
docker rm --force meridian-restore-20261020
```

Remove its separate artifact/output directory and temporary off-server QA copies
according to the declared retention policy. Do not remove the production volume,
artifact directory, scratch keeper or native-runtime retention container.

## Operational limits

The recovery objective is a verified point-in-time idle snapshot. This tool does
not provide continuous backups, point-in-time WAL replay, encryption, automated
off-server retention or a measured recovery-time/service-availability guarantee.
Capacity is bounded to this preview's dataset and quotas; larger workspaces need
a storage and backup design appropriate to their measured size.

Any retained database snapshot can contain records a user subsequently deleted.
Before operating a recurring backup service, publish its retention period and
implement expiry of off-server copies. A manual acceptance rehearsal and cleanup
do not establish that recurring service.
