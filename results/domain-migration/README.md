# Canonical hosted domain

On 2026-10-20, the hosted workspace moved to
[meridian.victormonnot.com](https://meridian.victormonnot.com/).
The previous `meridian.151.80.144.150.sslip.io` address redirects permanently
to the canonical HTTPS origin. Existing accounts and projects use the same
database; users sign in again because session cookies are host-only.

This change activates the hostname for the existing application release
`20261020-6a7122a290ca`. It does not rebuild the application or repeat the
[complete VIO acceptance campaign](../hosted-acceptance/README.md).

## Observed checks

| Area | Result | Evidence |
| --- | --- | --- |
| DNS | Explicit A record points to `151.80.144.150`, TTL 60 seconds; both Vercel authoritative nameservers, Google, Cloudflare and the client resolver agree. No competing AAAA/CNAME was observed. | [DNS](dns.json) |
| HTTPS | Normal certificate validation succeeds; root and `/api/health` return 200. The Let's Encrypt certificate covers the canonical host and expires on 2027-01-18. | [HTTPS](https.json) |
| Redirects | New HTTP redirects to HTTPS. Old HTTP and HTTPS return 301, preserving path and query; Chromium also retains the fragment and displays sign-in. | [HTTP checks](https.json), [browser redirects](browser-redirects.json) |
| Application | 49/49 maintained browser checks pass on the first execution: authentication, private projects, versioned configurations, conflicts, CSRF rejection/recovery, cross-account denials, logout and desktop/mobile layouts. | [Browser](browser.json) |
| Cookies | Authentication responses are `no-store`; session and CSRF cookies are host-only, `Secure`, `HttpOnly` and `SameSite=Lax`. | [Browser transport](browser-transport.json) |
| Infrastructure | Application/native images and all 103 original release sources are unchanged. Database and worker container identity, start time and mounts are preserved; all four services are healthy. | [Infrastructure](infrastructure.json) |
| Cleanup | Exactly two QA accounts, two remaining sessions and two private username budget windows were removed. Unrelated rows and aggregate budgets were preserved. | [Cleanup](browser-cleanup.json) |

The domain owner added the DNS record in the existing DNS provider.
CAA and nameserver observations remain consistent with the prior snapshot.
The three other hosted applications returned HTTPS 200 before and after the
switch. Their DNS records were observed afterward, but no exact prior snapshot
was available for a DNS comparison.

## Deployment and correction

The optional [Compose override](../../deploy/workspace/compose.canonical-domain.yaml)
adds routes for the previous hostname and a fixed HTTPS redirect. The deployment
environment now selects the canonical hostname for the API allowed host and
trusted CSRF origin. Only API/web were recreated, using the existing images.
The [deployment runbook](../../docs/workspace-deployment.md#changing-the-canonical-hostname)
documents retaining this override in future deployments and keeping both
hostnames resolvable for certificate renewal.

The first attempt obtained valid TLS but rolled back automatically: its verifier
compared the scheduling table's changing worker heartbeat as if it were immutable
application content. The corrected check preserves all ten table counts,
migrations and retained-job records, and compares nine application/authentication
table fingerprints while excluding that live scheduling fingerprint. The second
attempt passed. No application-data loss or application defect was observed;
the interrupted attempt and correction remain recorded in the evidence.

After browser cleanup, a final infrastructure check verified all 103 source files
against application commit `cf536acd849292b7696a8130615446ded0eafbbd`, along with
the exact deployed redirect override and unchanged image/container invariants.
Historical reports retain the URLs used for their original executions.

## Scope and limits

These are point-in-time DNS, transport and automated browser checks, not an
external expert usability study or an uptime guarantee. No VIO job was submitted
during this migration; estimator, dataset, evaluation and export behavior retain
their previous qualification evidence. API clients should address the canonical
origin directly rather than follow redirects for mutations.

Password recovery, recording uploads and scheduled backups remain unavailable.
Private environment backups, credentials, cookie values, account bindings and
raw database fingerprints are excluded from the public evidence.
