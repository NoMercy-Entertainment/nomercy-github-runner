# Capacity sharing

The optional `/federation` page provides existing-person peer permissions,
local limits, capacity, approval/denial, outgoing requests, leases and activity.
It uses the existing control database and controller, native database changes,
SSE and exact deadlines. No federation daemon or peer Docker socket is added.
Owner changes require the local admin session and CSRF. Peers cannot access
normal runner operations, secrets or logs.

Lending is disabled by default and has not passed a live two-dashboard
allocation/revocation proof. Issue #5 stays open until that proof and the
remaining deployment prerequisites are complete.

## NoMercy connection

The owner explicitly signs in for sharing, then connects this dashboard. Only
that optional sign-in retains a short account access token, encrypted in the
server database, never in browser cookies or JSON. It is sent only to the fixed
configured api.nomercy.tv or api-dev.nomercy.tv API. Exact local expiry clears
it even when sharing is disabled; no refresh token is stored.

Registration uses configured DASH_PUBLIC_URL, never Host/Forwarded-Host. A
locally initiated nonce and dedicated signed callback prove owner and endpoint
before NoMercy may route capacity. Existing authenticated dashboard people can
receive local request permission; Connect permission also grants that exact
person courier routing. Broker grants or global admin roles never grant local
hardware authority. Local revocation immediately fences new requests and
drains only the peer's leases.

The additive nomercy-tv API registers fleets separately from media servers. It
uses queued request/result pushes and short-lived RSA assertions bound to
issuer, fleet, actor, purpose, exact body, request, nonce and expiry. Account
bearers are never forwarded to peer callbacks. Receivers trust only their
configured issuer's public keys and their own person grants. Both transports
check public DNS, pin the address, keep TLS hostname/SNI verification and refuse
redirects/environment proxies. Receipt credentials cover one request only.

The requester chooses a permitted NoMercy fleet. Its own GitHub credential
mints a short registration token, the only GitHub credential sent through the
courier. PATs are refused. Signed result pushes update durable local records
and trigger SSE. Lost transport acknowledgement is delivery-unknown, not proof
that possibly allocated capacity is absent. Retry undelivered result events is
an explicit owner action after transport/feature recovery.

Generic direct ingress is an optional independent fallback with an expiring,
revocable credential for bounded intents only. It cannot impersonate broker
identity or inspect/operate normal runners. Direct outgoing delivery is not
included in the current screen.

## Local limits and provisioning

Each lent runner receives 8 cores, 16 GiB RAM, 100 GiB disk and 40 GiB cache.
Requests contain only profile, organisation, count and bounded lease. They
cannot choose templates, paths, commands, mounts, environment or resource caps.
The owner selects guarded templates advertised by healthy workers in the local
lending screen. Normal fleet capacity and Fillz/Default/Stoney remain unchanged.

LocalProvisioner atomically persists private allocation claims and standalone
nullable-fleet specs, checks aggregate placement before publication, and uses
the ordinary reconciler/FSM. It never changes a normal fleet's desired count.
Its ProvisioningFlow hook uses encrypted requester registration tokens and
never falls back to receiver PAT minting for owned specs. Registration,
revocation, retirement and exact expiry clear private token copies.

Only the receiver's own GitHub organisation is supported, because its local
observer/deletion credential can prove and retire ownership there. Foreign
organisations fail closed; they still need a safe scoped observation/removal
contract, not a requester's long-lived PAT.

The owner allows a separate private selected-repository group. Provisioning
proves its policy, repositories and that members are privately claimed lent
runners. Active capacity requires authenticated immutable unit guard proof and
GitHub name/id/group/labels proof. Ordinary/public groups and registration drift
cannot be published as active capacity.

Normal hooks keep their behavior. Lent units additionally use
agent/hooks/federation-started.js, which refuses forks/unread/unverified events,
ends only its ancestor Worker and chains normal hooks for allowed events.
Windows protects it with existing hook ACLs; macOS requires root-owned system
hooks. Linux uses the opt-in images/linux/unit/Dockerfile.federation image;
normal image aliases are not replaced.

Approval rechecks policy, quiet hours, quota, credential expiry and available
provisioning. Current policy reduction, peer revocation, lease expiry and exact
UTC quiet-hour boundaries retire affected owned leases. Quota remains reserved
until units and forge records are proven absent. Busy jobs drain through the
closed GitHub group and finish normally; lease expiry never force-kills them.
Fresh busy/unknown GitHub blocks destructive lent operations even with a
stopped/failed worker. Unit removal requires its forge record absent. Extension
requires a proven running job and fresh current-limit validation.

One controller convergence lock fences races, including revoke/expiry during
allocation. Temporary retirement/receipt errors schedule bounded retry
deadlines without releasing unproven capacity or ending the expiry worker.
Submission refuses an unavailable controller/expiry lifecycle. Credential
cleanup runs without adapters or broker enablement. No-op expiry writes are
avoided so native notifications cannot repeatedly trigger their own cleanup.

## Deployment prerequisites and release gate

- Deploy the reviewed additive NoMercy courier contract after PHP tests,
  migrations, backwards media checks and signing-key validation pass.
- Configure the existing OIDC client to obtain the NoMercy API's required
  audience. The API refuses dashboard-only tokens; no account/PAT delegation
  fallback is available.
- Each dashboard needs an existing public verified HTTPS DASH_PUBLIC_URL.
  Automatic runner-fleet hostname/Cloudflare tunnel handoff is not implemented.
  NAT-only fleets need that transport dependency; media tunnel jobs are not
  repurposed as runner administration.
- Prepare guarded templates and separate private groups without modifying
  normal Fillz. Keep lending and courier disabled until per-runtime negative
  fork/always-step probes and actual two-dashboard request/approval/register/
  job/quiet/expiry/revoke/absence proof pass.
- Foreign-org lending and generic direct outbound remain unsupported.

## Validation

Federation/broker/provisioner/outbound pytest files cover real RSA signing,
body/audience/purpose/replay binding, public DNS/TLS, credential expiry/exclusion,
identity/grants, atomic resources/claims, group/label drift, quiet/quota timers,
requester-token registration, unknown/busy retirement, monotonic actor-bound
results, CSRF and native cleanup. Hook tests end an isolated real Worker while
its Listener survives. These are component proofs, not live deployment proof.

`dashboard/tests/test_federation_http_lifecycle.py` also starts two independent
Flask dashboard processes and a test-only courier over actual HTTPS, verified
with a temporary test CA. It exercises owner-initiated callback proof,
known-person permission, manual approval, standalone fixed-resource claims,
requester-token consumption, signed active/terminal result callbacks and SSE.
It refuses body/audience/purpose tampering, preserves idempotent exact retries,
holds busy or unknown retirement, and proves native lease expiry and terminal
credential erasure without repeated status requests. Normal specs and fleet
rows remain unchanged. Every connection is routed to registered loopback test
ports; no production endpoint or agent is contacted.

That transport proof uses simulated GitHub/worker observations and a courier
wire-contract fixture. It does not replace the separate Laravel queue tests,
real OIDC audience validation, immutable per-runtime hook probes, or a live
two-dashboard GitHub allocation/job/drain/removal proof. The live gate above
therefore remains in force.

The strict-template eligibility gate also excludes current mutable-root Linux
workers. Linux lending requires owned readonly units and the opt-in guarded
image. Privileged DIND still assumes trusted admitted organisation code and is
not a general untrusted-code sandbox. macOS lending currently stays disabled:
system launchd ownership alone does not prove the job user lacks sudo rights.
A verified separate unprivileged macOS template contract remains required.
Windows's virtual service account and protected hook ACL is the eligible native
boundary. These gates change no normal worker root policy or normal runners.
