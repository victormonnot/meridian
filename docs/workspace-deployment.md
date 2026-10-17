# Deploy the project workspace

This deployment serves accounts, private project metadata and the reference
catalog. It does not install OpenVINS, download sensor recordings or run VIO jobs.
The [workspace contract](hosted-workspace.md) describes authentication, ownership
and retention. The original static explorer is also served at `/index.html`.

## Services and isolation

The Compose stack has a dedicated PostgreSQL 17 database, a Django/Gunicorn API,
and an unprivileged Nginx frontend. Only Nginx joins the existing external
`coolify` proxy network; the API and database use an internal project network.
No production service publishes a host port or mounts the Docker socket.
Traefik routes this hostname through the existing `http`/`https` entrypoints and
`letsencrypt` certificate resolver. Router and service names are specific to
Meridian. Do not replace or reconfigure the shared proxy or other applications.

The resource caps are 512 MiB / 0.5 CPU for PostgreSQL, 512 MiB / 0.75 CPU for the
API, and 128 MiB / 0.25 CPU for Nginx. These are metadata-service caps, not measured
VIO hosting requirements. Processes, temporary filesystems and container logs
are also bounded. Database storage lives in a dedicated named volume; it has no
filesystem quota yet. Project-count and text-size limits bound individual account
metadata, but operators must still monitor aggregate database/disk growth.

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

## Configure and build

Use a dedicated release directory and a secret environment file outside the
source checkout. Copy `deploy/workspace/.env.example`, restrict it to mode 0600,
set the canonical hostname, and generate three distinct random secrets for Django,
the bootstrap database role and the application role. Use hexadecimal database
passwords so the application's PostgreSQL URL needs no additional encoding.
Do not commit the environment file or paste resolved Compose configuration into
logs; it contains secrets.

Set `MERIDIAN_API_IMAGE` and `MERIDIAN_WEB_IMAGE` to unique release tags. The
`MERIDIAN_DOMAIN` value is a hostname, without scheme, port or path. The hostname
must resolve to this server before certificate issuance. Keep registration open
for the public preview, or set `MERIDIAN_REGISTRATION_OPEN=0` to close new accounts
while preserving sign-in for existing users.

The commands below assume the source is the current directory and the protected
environment file is `/srv/meridian-workspace/.env`:

```sh
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml build
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml up -d --wait db
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml run --rm --no-deps api python manage.py migrate --noinput
docker compose --project-name meridian-workspace \
  --env-file /srv/meridian-workspace/.env \
  -f deploy/workspace/compose.yaml up -d --wait api web
```

The database initialization script runs only on a new empty volume. Changing
environment passwords does not rotate an existing PostgreSQL role's password;
perform explicit role and secret rotation together. Migrations run as a separate
deployment step, not once per Gunicorn worker.

Verify HTTPS certificate validation, HTTP-to-HTTPS redirect, `/api/health`,
registration, sign-in, cookies and cross-account access from a separate machine.
Check the existing applications before and after the new deployment. Nginx's
`/healthz` is an internal static-serving check; it does not replace API or browser
verification. The HTTPS proxy header is trusted only because the application is
reachable through the private ingress network.

## Local container check

Use a different environment file, distinct secrets and the explicit local override:

```sh
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml build
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml up -d --wait db
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml \
  run --rm --no-deps api python manage.py migrate --noinput
docker compose --project-name meridian-workspace-local \
  --env-file build/workspace/local.env \
  -f deploy/workspace/compose.yaml -f deploy/workspace/compose.local.yaml up -d --wait api web
```

Set `MERIDIAN_DOMAIN=localhost` in this local environment. The override creates
an isolated ingress network and binds only `127.0.0.1:8090`; it enables Django
debug mode and HTTP cookies for local checks. Never use it on the hosted server.

## Operations and limitations

Keep the PostgreSQL volume and environment file across service restarts and
release updates. `docker compose down` preserves named volumes; `down --volumes`
deletes the database and must not be used for routine deployment. A source release
alone cannot recover accounts or projects.

Run `python manage.py cleanup_workspace` in a one-off API container periodically
to delete expired sessions and authentication counters older than a day. This
does not delete projects or active sessions. Authentication budgets persist across
API restarts. Session expiry is enforced even before this cleanup runs.

Increment 1.2 qualifies service restart persistence, not disaster recovery.
Scheduled backups, off-server copies and a verified restoration procedure are
not established by the Compose file. Until those are configured, treat the
hosted application as a preview for reproducible project metadata, not the only
copy of engineering work. No user recording upload exists in this increment.
If operators retain database snapshots, deleted records may remain in them until
those snapshots are removed; publish their retention period before relying on
such a backup service.

For rollback, retain the preceding source release and immutable images. Restore
the preceding application only when its schema remains compatible; do not blindly
reverse database migrations. Future data migrations require a backup and an
explicit rollback plan. Switching the hostname requires updating its DNS record,
the environment, allowed origins and the Meridian router together; other DNS
records and proxy routes remain unchanged.
