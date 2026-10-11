# Machine access and job events

An administrator creates machine access on the dashboard's **Access** page.
Give a result consumer `jobs:read`; add `fleet:read` only if it also needs the
runner inventory. Credentials expire and can be revoked on the same page.
The plaintext credential is shown once. Store it in the client's secret
store, not in source control or a command line. The dashboard stores its
SHA-256 digest, not the credential itself.

Browser login and machine credentials are separate. An invalid bearer token
does not inherit a browser administrator's session. Machine access cannot
read secrets, manage users, change resource limits, provision runners, or
remove registrations. The optional `runners:operate` scope permits only
start, stop, restart, drain, cancel-drain, and cache clearing.

## Subscribe to results

Use an HTTPS streaming request to `GET /api/v2/events/jobs` with
`Authorization: Bearer <credential>`. The response is Server-Sent Events.
Each event has a durable, increasing `id`, an `event: job` type, and a JSON
`data` record containing:

- Runner UUID, provider, job name and `started` or `finished` state.
- Repository, commit, run ID, attempt and job ID when supplied by the source.
- The source timestamp, controller receipt time and actual conclusion.

Keep the connection open. Persist the last processed event ID and supply it
as `Last-Event-ID` when reconnecting. Replayed events may already have been
processed; consumers must be idempotent. A connection keepalive does not
query the job database. Native database notifications wake the event stream
and dashboard WebSocket subscribers. Revocation closes the machine's stream.

A worker event is authenticated by the worker's pinned certificate and
checked against its runner ownership. It cannot grant itself the authority
of a signed forge event. A missing conclusion remains unknown.

## GitHub delivery

The Access page can configure an organization `workflow_job` webhook using
the existing GitHub credential. It requires webhook-management permission
and an HTTPS `DASH_PUBLIC_URL`. Only the hook with this dashboard's exact
target URL is created or changed; unrelated hooks are preserved.

GitHub signs the exact request body using a private key in
`job-webhook-github.key` in the dashboard data directory. The receiver checks
the signature before decoding the payload and matches the upstream runner
registration ID to an owned runner UUID. Unmanaged runners are ignored.
Repeated deliveries are deduplicated in the durable event ledger.

The deployed GitHub feed received genuine signed job deliveries on
2026-10-11, including repository, commit and the upstream conclusion. This
does not mean the legacy history collector or other periodic status readers
have already been replaced.

## Forgejo compatibility

Forgejo 16.0.5 does not expose GitHub's `workflow_job` webhook event. Its API
can accept the request while returning an empty event list. Configuration
therefore verifies the returned active state and event list and disables
only this dashboard's hook if the requested delivery is unsupported. An
active hook alone is not evidence of a working job feed.

The common authenticated worker event endpoint and replayable stream accept
both providers, but the Forgejo job producer still needs its own verified
event source. Existing Forgejo history remains in place until that source
is ready.

Primary compatibility source:
[Forgejo 16.0.5 webhook event types](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.5/modules/webhook/type.go).

## Deployment verification, 2026-10-11

The controller/dashboard deployment preserved all 19 runner UUIDs and
upstream registration identities. The subsequent webhook-validation update
recreated only the dashboard; the controller container was preserved.

The operator has supplied Stoney's Mac, Windows PC, Proxmox host and Linux
VM addresses. A dedicated SSH key is prepared outside the repository. Stoney
has installed its public key on the Mac, Proxmox host and Linux VM; Windows
still needs its administrator step. Enrollment awaits access to Stoney's
LAN or VPN. The Proxmox host is infrastructure, not an extra runner.
No machines have been enrolled before their access and capacity are verified.
