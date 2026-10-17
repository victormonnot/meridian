# Private project workspace

Increment 1.2 adds accounts, persistent private projects and a versioned reference
catalog. It does not execute VIO jobs. The qualified OpenVINS runner remains a
separate local capability; experiment configuration and hosted computation belong
to increments 1.3 and 1.4.

The [hosted preview](https://meridian.151.80.144.150.sslip.io/) is available on a
temporary HTTPS hostname. The [validation report](../results/workspace/README.md)
records its checked release and limits.

## Application boundary

The Vite workspace is a separate entry point, `web/workspace.html`. The original
roll explorer remains at `web/index.html`. The deployment serves the workspace
at `/`, the original explorer at `/index.html`, and the metadata API at `/api/v1`.
Projects expose Data, Experiments, Results and Settings views. Empty experiment
and result views do not imply a queued job or show a fabricated result.

Django 5.2 LTS supplies authentication, password validation/hashing, server-side
sessions, CSRF protection and database migrations. This replaces the earlier
FastAPI candidate for the initial metadata service. PostgreSQL persists accounts,
sessions, projects and authentication budgets. The numerical Python/C++ modules
are not imported by the HTTP service. The API image contains neither the VIO
runtime nor sensor recordings, Docker access, host repositories or private notes.

The reference catalog describes the exact calibrated TUM-VI room1 variant
qualified in increment 1.1. It distinguishes motion-capture coverage from
estimator-output coverage, includes calibration conventions and hashes, and
identifies the existing qualification as local. Absolute nanosecond timestamps
are decimal strings to preserve precision in JavaScript. Catalog entries are
metadata; they do not claim that sensor files or execution are available online.

## Identity, ownership and lifecycle

Registration uses a username and password. Django validates usernames and a
minimum 12-character password, rejects common/numeric/similar passwords, and stores
only a password hash. Passwords are limited to 1024 characters. Usernames are
case-sensitive. No email or third-party identity provider is required. Password
recovery is not available in this preview; an operator can use Django's
`changepassword` command for an established account after verifying ownership.

Production sessions are opaque, database-backed cookies with HttpOnly, Secure and
SameSite=Lax attributes. They expire eight hours after authentication; ordinary
reads do not extend that interval. Logout invalidates the server-side session,
including replay of its previous cookie. CSRF protection applies to login,
registration and every authenticated mutation. The client receives a masked CSRF
token from the session endpoint and refreshes it after authentication changes.
The preview sets a one-hour HSTS policy for its own host. It does not request
subdomain coverage or browser preload; Django's corresponding deployment
advisories (`security.W005` and `security.W021`) are deliberate for this staging
policy, rather than a claim of readiness for preloading.

Every project query is scoped to its authenticated owner. Reading, editing or
deleting another account's identifier returns the same 404 as a missing project.
The caller cannot choose or change an owner. Dataset selection is an allowlist at
creation and is immutable for this increment. No API accepts filesystem paths,
artifact identifiers, source code, container names or execution commands.

Each account can retain 20 projects, with names up to 100 characters and
descriptions up to 2000 characters. Creation locks the owner's database row so
concurrent requests cannot exceed the project cap on PostgreSQL. Upload and
computation allowances are zero: those endpoints do not exist. JSON request
bodies are bounded to 8 KiB and unknown fields are rejected.

Projects remain until their owner deletes them. Deletion removes the live project
metadata immediately and cannot be undone through the application. Account
removal is currently an operator action; deleting the Django user cascades to
their projects. Database backups can retain earlier copies until their retention
period ends. See the deployment guide for the actual backup policy.
The [deployment guide](workspace-deployment.md) covers the isolated services,
release procedure and preview's recovery limitations.

Authentication attempts have shared, database-backed budgets: 10 per normalized
username per 15 minutes, plus 100 sign-in attempts per ingress peer per 15 minutes
and 20 registrations per ingress peer per hour. Successful attempts also count.
Because the private ingress is the API's direct peer, its budget is aggregate
across visitors. Visitor-supplied forwarding headers cannot bypass it. Exceeding
a budget returns 429 with `Retry-After`. Registration can be closed using
`MERIDIAN_REGISTRATION_OPEN=0`. These initial preview limits can constrain busy
shared access; they are not an internet-wide abuse-prevention guarantee.

## API contract

Responses carry `schema_version: 1`. Errors have
`error: {code, message, fields?}`; field errors are arrays of messages.
Mutations send JSON and `X-CSRFToken`. Cookies remain on the same origin.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/v1/session` | Current user or null, CSRF token, capabilities, project cap and registration availability. |
| `POST /api/v1/auth/register` | Validate credentials, create account and start a session. |
| `POST /api/v1/auth/login` | Authenticate and start a session. |
| `POST /api/v1/auth/logout` | Invalidate the current session. |
| `GET /api/v1/projects` | List only the caller's projects. |
| `POST /api/v1/projects` | Create a project with name, optional description and supported dataset ID. |
| `GET /api/v1/projects/{uuid}` | Read one owned project. |
| `PATCH /api/v1/projects/{uuid}` | Update name and/or description. |
| `DELETE /api/v1/projects/{uuid}` | Delete one owned project. |
| `GET /api/v1/datasets` | Read the maintained catalog after authentication. |
| `GET /api/v1/datasets/{id}` | Read one catalog entry. |
| `GET /api/health` | Report database connectivity without user or environment details. |

API responses are not cached. The production ingress serves compiled assets only;
unknown static paths return 404. There is no general file download route.

## Local development and verification

Use Python 3.12 and the existing Node.js environment. SQLite is available only in
explicit local debug mode; deployment requires PostgreSQL. Local SQLite checks
do not establish PostgreSQL locking behavior.

```sh
python -m pip install -r server/requirements.lock
MERIDIAN_DEBUG=1 python server/manage.py migrate
MERIDIAN_DEBUG=1 python server/manage.py runserver 127.0.0.1:8000
```

In another terminal, run `npm --prefix web run dev` and open
`http://127.0.0.1:5173/workspace.html`. Vite proxies `/api` to the local Django
server; no cross-origin authentication configuration is needed.

```sh
MERIDIAN_DEBUG=1 python server/manage.py test workspace
MERIDIAN_DEBUG=1 python server/manage.py makemigrations --check --dry-run
npm --prefix web test
npm --prefix web run build
```

Set `DATABASE_URL` to a dedicated PostgreSQL test database to run the same tests
against PostgreSQL; Django creates a separate `test_` database and needs permission
to create/drop it. Never point the test process at deployment credentials.
The CI workspace job uses an isolated PostgreSQL service. Run
`python server/manage.py cleanup_workspace` periodically to remove expired
sessions and authentication counters older than a day.

The optional browser check targets a running container or HTTPS deployment with
its production content-security policy. It registers two disposable accounts and
creates, edits and deletes a project; run it only against an authorized test
deployment. It never uses an implicit public target.

```sh
npm install --prefix build/browser-tools playwright@1.63.0
build/browser-tools/node_modules/.bin/playwright install chromium
node web/scripts/check-workspace-browser.mjs \
  http://127.0.0.1:8090/ outputs/workspace-browser-check
```

`MERIDIAN_PLAYWRIGHT_MODULE` can point to an existing Playwright module and
`MERIDIAN_BROWSER_EXECUTABLE` to an installed Chromium executable. The output
contains screenshots and a report listing the exact disposable usernames for
operator cleanup, without passwords or cookies. The script does not delete
accounts through an administrator interface or infer the backing database from
browser behavior. API session-expiry and PostgreSQL concurrency guarantees remain
covered by their separate server tests.

## Sources

- [Django server-side sessions](https://docs.djangoproject.com/en/5.2/topics/http/sessions/).
- [Django CSRF protection for AJAX](https://docs.djangoproject.com/en/5.2/howto/csrf/).
- [Django deployment settings](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/).
- [Qualified VIO reference](vio-reference.md) and [recorded evidence](../results/vio-reference/README.md).
