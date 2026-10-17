# Hosted workspace validation

Checked on **2026-10-17**. This report covers increment 1.2: accounts, private
project metadata and a reference-dataset catalog. It is not a VIO execution,
navigation-accuracy or full part 1 acceptance report.

The [HTTPS preview](https://meridian.151.80.144.150.sslip.io/) runs a dedicated
Django/PostgreSQL/Nginx stack on a shared Linux VPS. The temporary hostname is
the tested origin. The [application contract](../../docs/hosted-workspace.md)
and [deployment recipe](../../docs/workspace-deployment.md) describe reproduction
and operational limits.

## Release identity

Release `20261017-af198d92062a` was built from the 48 allowlisted source files in
[source-manifest.json](source-manifest.json). The manifest records each file's
SHA-256, the transferred bundle digest and the deployed image identities. These
identify the checked artifacts; they do not promise byte-identical Docker builds
or publish the runtime images to a registry. Secrets were generated on the server
and are excluded from the source bundle, Git and this report.

The API and database use an internal network without published host ports. Only
Nginx joins the existing ingress network. Runtime inspection confirmed non-root
users, read-only container roots, dropped Linux capabilities and the configured
resource caps. The application database role has no superuser, database-creation,
role-creation or replication privilege. No VIO runtime or sensor recording was
installed by this release.

## Checks and observations

| Check | Observed result |
| --- | --- |
| Backend on PostgreSQL | 38 tests passed, including real simultaneous requests for project-count and authentication-budget enforcement. |
| Backend on local SQLite | 36 tests passed; the two PostgreSQL concurrency tests were skipped as intended. |
| Frontend | 179 Node tests passed; both Vite entry points built successfully. |
| Dependency and schema checks | Python dependency check and migration-drift check passed; npm audit reported zero advisories after a targeted `source-map-js` lock update. This is not a comprehensive security audit. |
| Local container browser | 28 checks passed against the compiled application and its content-security policy. |
| Remote HTTPS browser | The same 28 checks passed from a separate Linux workstation using Chromium with certificate validation enabled. No uncaught JavaScript errors or CSP violations were observed. |
| Transport and routing | HTTP redirected to the canonical HTTPS origin; production session/CSRF cookies had Secure, HttpOnly and SameSite=Lax attributes. API responses were not cached. Private and unsupported artifact paths returned 404. |
| Local API container replacement | An existing session and project remained accessible after API recreation without restarting Nginx. Docker DNS resolution refreshed the upstream address. |
| Hosted restart persistence | The original authenticated session and project survived a database restart while the API stayed running, then an API restart. All three services returned healthy. |
| Shared-host check | The three pre-existing hosted applications returned HTTPS 200 before and after deployment and after the persistence checks. Their configuration was unchanged. |

The three disposable accounts created for the first remote browser and persistence
checks were removed by exact identifier, along with their test project and three
sessions. Reusing the persistence witness's old session returned an anonymous
user and denied private access; its deleted credentials no longer authenticated.

The reusable [browser check](../../web/scripts/check-workspace-browser.mjs)
exercises registration, sign-in/out, project creation/editing/deletion, refresh
and deep links, a second account's rejected GET/PATCH/DELETE requests, text-only
rendering of user content, CSRF rejection and recovery, loss of authentication,
light/dark themes, and 390-pixel layouts. It also loads the original explorer,
its recorded parameter studies and web fonts under the deployed CSP. Expected
401/403/404 responses appear in the browser console during negative checks.

Browser checks verify authentication loss by removing cookies. Actual session
expiry and invalidation of a logged-out cookie are separate backend test cases.
Browser behavior alone does not prove the database engine or network isolation;
the deployment inspection and PostgreSQL tests provide those separate checks.

## Reproduction and limits

Use the server and browser commands in the
[workspace guide](../../docs/hosted-workspace.md#local-development-and-verification)
against a dedicated test database and an explicitly authorized deployment. The
browser script leaves exact disposable account identifiers in its local report
for operator cleanup; it does not save passwords or cookies. Raw QA reports and
screenshots are local artifacts. The public
[workspace screenshot](../../docs/media/README.md) uses fictional project text.

This release has no experiment specifications, computation queue, uploads,
project-generated VIO results, team sharing or self-service password recovery.
The catalog labels the existing OpenVINS qualification as a separate local result.
Storage is limited through project counts and field sizes, without a database
filesystem quota. Authentication budgets are aggregate behind the ingress and
can constrain busy shared access.

Restart persistence does not establish backup restoration, host-failure recovery,
load capacity or long-term availability. Scheduled backups and a verified restore
procedure remain unqualified. The preview should not be the only copy of
engineering work. The new PostgreSQL CI job is committed with this change; its
GitHub-hosted run requires a subsequent repository push. The unchanged numerical
suite was not rerun for this metadata/UI increment; its previous evidence remains
in the [VIO qualification report](../vio-reference/README.md).
