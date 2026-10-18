# Deploy the project workspace

This deployment serves accounts, private projects, versioned experiments and
durable VIO jobs with synchronized result investigation. Computation is opt-in: the `compute` Compose profile adds a
trusted worker after the qualified native image, prepared reference dataset and
bounded scratch filesystem have been installed separately. The API image does
not contain the estimator, Docker CLI, NumPy or recordings. The separate
`compose.investigation.yaml` override grants the API read-only access to published
artifacts and the two prepared camera-image directories.
The [workspace contract](hosted-workspace.md) describes authentication, ownership
and retention. The original static explorer is also served at `/index.html`.

## Services and isolation

The Compose stack has a dedicated PostgreSQL 17 database, a Django/Gunicorn API,
an unprivileged Nginx frontend and an optional computation worker. Only Nginx
joins the existing external `coolify` proxy network; the API, database and worker
use an internal project network. No production service publishes a host port.
Only the worker mounts the Docker socket. This maintained orchestrator has
host-level daemon privileges and is trusted infrastructure, not a sandbox for
arbitrary user code. Neither API nor numerical child containers get the socket.
Traefik routes this hostname through the existing `http`/`https` entrypoints and
`letsencrypt` certificate resolver. Router and service names are specific to
Meridian. Do not replace or reconfigure the shared proxy or other applications.

| Process | Memory limit | CPU limit | Additional bounds |
| --- | --- | --- | --- |
| PostgreSQL | 512 MiB | 0.5 | 128 PIDs, private persistent volume |
| API | 512 MiB | 0.75 | 128 PIDs, 32 MiB temporary filesystem |
| Nginx | 128 MiB | 0.25 | 64 PIDs, 16 MiB temporary filesystem |
| Worker | 512 MiB | 0.25 | 128 PIDs, 32 MiB temporary filesystem |
| Native estimator | 4096 MiB | 2 | 900 seconds, 128 PIDs, 128 MiB `/tmp` |
| Separate evaluator | 512 MiB | 0.5 | 120 seconds, no network or secrets |

Only one job executes globally. Native estimation and evaluation run sequentially;
the worker checks host memory available of at least 5120 MiB and free artifact-disk
space of at least 10 GiB before admission. These thresholds do not reserve memory
against subsequent activity by other applications on a shared host. A capacity
shortage leaves jobs queued. Metadata-service logs rotate at 10 MiB × 3 per
container; child-process logs are capped at 2 MiB and artifacts at 32 MiB per
attempt. Database storage has no filesystem quota, so aggregate growth still
requires monitoring. See the [job contract](hosted-vio-jobs.md) for account,
submission, attempt and retention limits.

The worker mounts prepared data read-only and private artifact/scratch directories
read-write. The estimator receives only sensor data, configuration and its output
directory. Ground truth is mounted only into the independent evaluator, together
with its exact evaluation inputs. Children have no network, application secrets
or database connection. Job summaries come from PostgreSQL. With investigation
enabled, the API also reads immutable published artifacts and the two camera
directories. These mounts are read-only. The API has no separate mount of the
prepared reference trajectory, IMU CSV or calibration tree, and no scratch or
Docker-socket mount. Published artifacts include the run's rendered configuration.
Nginx has no data mounts: image and investigation requests pass through Django
ownership checks.

PostgreSQL uses a separate bootstrap administrator. The application role owns
only its database and has no superuser, role-creation, database-creation or
replication capability. The API receives only its application password; the
bootstrap password remains outside it. Never connect to another application's
database or reuse its credentials.

Docker base images are pinned by digest; Python and JavaScript dependency versions
are locked. The root `.dockerignore` admits only the runtime/build sources needed
for these images. Private notes, Git objects, local databases, numerical outputs,
sensor archives and native estimator builds are excluded. Nginx serves compiled
assets only. Its content-security policy hashes the exact inline theme bootstrap
bytes during the build, without enabling arbitrary inline scripts or evaluation.
Only the image directive additionally permits `blob:` URLs, used to display
authenticated frame responses fetched by the workspace.

## Configure and build

Use a dedicated release directory and a secret environment file outside the
source checkout. Copy `deploy/workspace/.env.example`, restrict it to mode 0600,
set the canonical hostname, and generate three distinct random secrets for Django,
the bootstrap database role and the application role. Use hexadecimal database
passwords so the application's PostgreSQL URL needs no additional encoding.
Do not commit the environment file or paste resolved Compose configuration into
logs; it contains secrets.

Set `MERIDIAN_API_IMAGE`, `MERIDIAN_WEB_IMAGE` and, when enabled,
`MERIDIAN_WORKER_IMAGE` to unique release tags. The worker resolves its evaluator
image to an immutable content ID at startup. The
`MERIDIAN_DOMAIN` value is a hostname, without scheme, port or path. The hostname
must resolve to this server before certificate issuance. Keep registration open
for the public preview, or set `MERIDIAN_REGISTRATION_OPEN=0` to close new accounts
while preserving sign-in for existing users.

The commands below first deploy the metadata services with computation disabled
(`MERIDIAN_EXECUTION_ENABLED=0`). They assume the source is the current directory and the protected
environment file is `/srv/meridian-workspace/.env`:

```sh
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml build
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml up -d --wait --no-recreate db
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml run --rm --no-deps api python manage.py migrate --noinput
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml up -d --wait --no-deps api web
```

The database initialization script runs only on a new empty volume. Changing
environment passwords does not rotate an existing PostgreSQL role's password;
perform explicit role and secret rotation together. Migrations run as a separate
deployment step, not once per Gunicorn worker.

For an application-only upgrade, preserve the existing database container with
`--no-recreate`; moving to a new source-release directory otherwise changes the
initialization script's bind path and can recreate it unnecessarily. Keep the
release directory supplying that existing bind mount. `--no-deps` on migration
and API/web replacement prevents an implicit database replacement. A deliberate
database image or configuration upgrade requires its own maintenance procedure.

Verify HTTPS certificate validation, HTTP-to-HTTPS redirect, `/api/health`,
registration, sign-in, cookies and cross-account access from a separate machine.
Check the existing applications before and after the new deployment. Nginx's
`/healthz` is an internal static-serving check; it does not replace API or browser
verification. The HTTPS proxy header is trusted only because the application is
reachable through the private ingress network.

## Enable the qualified computation profile

Prepare the calibrated room1 tree and native runtime using the
[reference procedure](vio-reference.md). Transfer them separately from the source
bundle; compare archive SHA-256 values, verify every prepared file against its
manifest and retain the transfer manifest. Do not download datasets during an
API build or substitute a rebuilt native image without qualification. Dataset
license and attribution information remains in the prepared provenance.

The saved specification pins the native image configuration digest
`sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da`.
For the transferred image, Docker's containerd store exposes the OCI manifest
digest `sha256:54863f14d9f92d2805f5fddcfb78d146c6aac2ad77dc348e304059e2252a400a`
as its image ID. The checked-in identity resolver hashes the exact manifest bytes
and verifies their binding to the pinned configuration; that manifest also commits
to all eleven layers. Both identities are retained in execution provenance. No
mutable tag fallback or change to saved specifications is permitted. This
representation difference is documented by [Docker's maintainer](https://github.com/moby/moby/issues/51934).

Configure these paths as identical absolute paths on the host and inside the
worker, because child bind mounts are resolved by the host Docker daemon:

| Variable | Purpose |
| --- | --- |
| `MERIDIAN_VIO_PREPARED` | Read-only prepared sensor, configuration and evaluation-reference tree |
| `MERIDIAN_JOB_ARTIFACT_ROOT` | Private persistent attempt artifacts, owned by UID/GID 10001 |
| `MERIDIAN_JOB_SCRATCH_ROOT` | Dedicated tmpfs of at most 128 MiB, owned by UID/GID 10001 |
| `MERIDIAN_DOCKER_GID` | Existing socket group from `stat -c %g /var/run/docker.sock` |

Use mode 0700 for the artifact and scratch roots. The provided `scratch.mount`
unit uses the default deployment path from `.env.example`. Install it under the
escaped mount-unit name, after inspecting that its `Where` matches the chosen
path. For the default path:

```sh
scratch_unit=$(systemd-escape --path --suffix=mount /srv/meridian-workspace/scratch)
sudo install -d -m 0700 -o 10001 -g 10001 /srv/meridian-workspace/jobs /srv/meridian-workspace/scratch
sudo install -m 0644 deploy/workspace/scratch.mount "/etc/systemd/system/$scratch_unit"
sudo systemctl daemon-reload
sudo systemctl enable --now "$scratch_unit"
findmnt --target /srv/meridian-workspace/scratch
```

The worker refuses a scratch directory that is not on a tmpfs or whose total
capacity exceeds 128 MiB. Do not replace it with an ordinary disk directory or
an unbounded host `/tmp`. No shared Docker daemon restart is needed.

After assets and storage have been checked, set `MERIDIAN_EXECUTION_ENABLED=1`
and build/start computation and investigation with the same release environment:

```sh
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.investigation.yaml \
  --profile compute build api worker
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.investigation.yaml \
  --profile compute up -d --wait --no-deps api worker
```

Apply pending migrations before replacing these services. Keep the existing
database container and volume as in the metadata upgrade procedure above.
The worker health check verifies PostgreSQL access and a heartbeat no older than
45 seconds; Docker runs this check every 30 seconds with a 30-second timeout
to accommodate startup under the worker CPU cap. It is a liveness check, not proof that resources or the native image
are currently usable. Qualification must submit a real saved revision, verify its
terminal state, input/output hashes and independent metrics, and test cancellation
and lease recovery. Capture redacted container limits/mounts, actual image IDs,
the source manifest, prepared-data manifest, database identity and existing-site
health before and after deployment. Never capture environment values or cookies.

## Private investigation access

Include `compose.investigation.yaml` on subsequent API upgrades so its three
read-only mounts remain present. It mounts `MERIDIAN_JOB_ARTIFACT_ROOT` at the
same path used by the worker, plus the prepared `cam0/data` and `cam1/data`
directories under `MERIDIAN_CAMERA_ROOT` (default `/reference-cameras`). Sources
must already exist; Compose refuses to create empty substitute directories.
The base Compose stack remains usable without prepared data when computation and
investigation are not enabled.

The API serves only an owned job's published investigation and indexed camera
frames. The worker publishes `investigation.json` and `image-index.json` with
the attempt's immutable artifact hashes. The index binds image identities to the
prepared dataset; it is not a general file-serving API. Original mono16 PNG bytes
are served without image conversion or additional image libraries in the API.
Images remain shared prepared inputs rather than being copied into each job's
32 MiB artifact allowance. Estimator preprocessing remains unchanged.

Artifact expiry and deletion revoke subsequent hosted investigation/image access.
Responses must remain private and uncached; do not add a public Nginx alias or
CDN cache for either mount. Old jobs without the investigation artifacts are
explicitly unavailable; deployment does not silently recompute or backfill them.
Qualification includes cross-account image denial, malformed identifiers, missing
or changed artifacts, expired access, and browser display of the original PNGs.
The [investigation validation report](../results/investigation/README.md) records
the tested release, artifact sizes and observed browser/API resource measurements.

## Local container check

Use a different environment file, distinct secrets and the explicit local override:

```sh
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml build
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml up -d --wait --no-recreate db
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml \
  run --rm --no-deps api python manage.py migrate --noinput
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml up -d --wait --no-deps api web
```

Set `MERIDIAN_DOMAIN=localhost` in this local environment. The override creates
an isolated ingress network and binds only `127.0.0.1:8090`; it enables Django
debug mode and HTTP cookies for local checks. Never use it on the hosted server.

For local computation and investigation, additionally include
`-f deploy/workspace/compose.investigation.yaml` and use `--profile compute`, prepared data, a
private artifact directory and a dedicated 128 MiB tmpfs with identical host and
worker paths. A Docker local volume with `type=tmpfs` and a keeper container can
provide this on a development host without a system-wide mount change. Use its
actual host mountpoint as `MERIDIAN_JOB_SCRATCH_ROOT`. The development-only worker
option `--ignore-capacity` bypasses admission thresholds only with Django debug
enabled; it does not remove native process or scratch limits. Do not enable this
override on the shared server.

## Operations and limitations

Keep the PostgreSQL volume and environment file across service restarts and
release updates. `docker compose down` preserves named volumes; `down --volumes`
deletes the database and must not be used for routine deployment. A source release
alone cannot recover accounts or projects.

Run `python manage.py cleanup_workspace` in a one-off API container periodically
to delete expired sessions and authentication counters older than a day. This
does not delete projects or active sessions. Authentication budgets persist across
API restarts. Session expiry is enforced even before this cleanup runs.

The running worker removes deleted/orphaned attempt artifacts and expires retained
artifacts after seven days. Results and hashes remain in database metadata until
their owning records are deleted. If the worker is stopped, physical artifact
cleanup waits for it to resume. Monitor disk usage, queue capacity and failed
cleanup: the execution slot stays occupied when a child cannot be confirmed
stopped. Do not delete active attempt directories manually or remove the shared
database volume to clear a queue.

Service and worker recovery checks qualify persistence and bounded retry, not disaster recovery.
Scheduled backups, off-server copies and a verified restoration procedure are
not established by the Compose file. Until those are configured, treat the
hosted application as a preview, not the only copy of engineering work.
No user recording upload exists in this increment; only the maintained room1
profile executes. Run comparisons and independent exports remain later increments.
If operators retain database snapshots, deleted records may remain in them until
those snapshots are removed; publish their retention period before relying on
such a backup service.

For rollback, retain the preceding source release and immutable images. Restore
the preceding application only when its schema remains compatible; do not blindly
reverse database migrations. Future data migrations require a backup and an
explicit rollback plan. Switching the hostname requires updating its DNS record,
the environment, allowed origins and the Meridian router together; other DNS
records and proxy routes remain unchanged.
