# Dashboard authentication and access control

Status: design agreed. Implementation in progress.
Supersedes: the shared-password login in `dashboard/app.py`.

## Why

The dashboard authenticated with one shared password, and it is now reachable
at `http://192.168.178.19:9200` and through a TLS-terminating proxy at
`https://gh-runners.phillippepelzer.me`. One secret shared by everyone who
should ever have access cannot be revoked for one person, records nothing
about who acted, and cannot express "may look, may not touch".

An earlier draft proposed "sign in with GitHub, restricted to members of
NoMercy-Entertainment". That was rejected during design and the reason is
worth keeping: org membership is a set, not an access rule. It resolves to six
people plus an outside collaborator, all of whom would have been able to stop,
remove and recreate runners. **Proving who you are must not, on its own, grant
access to anything.**

## Provider

One provider: Keycloak at `auth.nomercy.tv`, realm `NoMercyTV`. Confirmed
against its discovery document.

    issuer   https://auth.nomercy.tv/realms/NoMercyTV
    PKCE     S256
    signing  RS256
    claims   sub, name, preferred_username, email

There is no `groups` or `roles` claim, so Keycloak cannot express who may use
the dashboard - and does not need to. It is asked who someone is; never
whether they may do anything.

### The client

The existing `phillippepelzer.me` client is reused, as instructed. Its
`redirectUris` include `https://*.phillippepelzer.me/callback`, so the
dashboard callback must be exactly:

    https://gh-runners.phillippepelzer.me/callback

Path `/callback`, not `/auth/oidc/callback` - the wildcard covers the host
segment, not the path.

Accepted, recorded once: one secret now authenticates both Forgejo and this
dashboard, so rotating or revoking it for one breaks the other.

### The redirect_uri is pinned, never derived

Built from `DASH_PUBLIC_URL` in `.env`, never from `Host` or
`X-Forwarded-Host`. The app is also reachable directly on the LAN address,
where those headers are attacker-controlled; deriving the redirect from them
lets a forged header send an authorization code to another host.

### No JWT library

The image installs Flask and nothing else, and that is kept.

The flow is authorization code + PKCE `S256` + `state`, with a confidential
client, and the code is exchanged in a direct server-to-server TLS call to the
token endpoint. Claims are then read from `userinfo`, also over direct TLS -
not by parsing the `id_token`.

This is the case OIDC Core 3.1.3.7 covers: when the token is received by
direct communication with the token endpoint, TLS server authentication may
stand in for verifying the token signature. The threats a signature check and
a `nonce` would cover are already covered here - PKCE binds the code to this
login attempt, the client secret means only we can redeem it, `state` covers
CSRF, and TLS authenticates the issuer. A JWT verifier would add a dependency
and a keyset cache to be wrong about, for no threat left standing.

If the flow ever changes shape - an implicit or hybrid response, or an
id_token read from anywhere but the token endpoint - this reasoning expires
and a real verifier is required.

## Authorization

An explicit allowlist in `/data/users.json`. Authenticating grants nothing;
holding a nomercy.tv account grants nothing.

### Identity key

Keyed on `sub`, never `preferred_username` or `email`. A username can be
changed, freeing the old one for someone else to take; an allowlist keyed on
it would transfer access silently. `sub` cannot be transferred.

### Roles

| Role | May |
|---|---|
| `owner` | everything, plus approve/deny/revoke and set roles |
| `operator` | everything except user management |
| `viewer` | read only: fleet, runner detail, logs, engine, history |

Enforced in the existing `before_request` guard, not per endpoint. Every
mutating route is already a POST and every read a GET, so the rule is one
condition in one place: a POST from a `viewer` is 403. A check repeated at
fifteen call sites is a check that gets forgotten at one of them.

Two reads are not for everyone, and are named rather than left implicit:

- `GET /settings` - operator and above. The real token never reaches the
  browser (the template renders `token_mask`), but the configuration does, and
  a partially masked token is still a disclosure.
- `GET /users` - owner only.

### Owner bootstrap

Seeded, not claimed. `DASH_OWNER=oidc:preferred_username:<name>` in `.env`.

Deliberately not "first to sign in wins": the dashboard is reachable beyond
this machine, so that is a race that hands over the fleet. On the first
successful login matching that hint the owner is bound to the returned `sub`,
written to `users.json`, and the hint is never consulted again - so a later
rename cannot take the account over. The hint exists only because a human does
not know their own `sub`.

### Access request flow

1. An unknown identity authenticates successfully.
2. No allowlist entry: the request is recorded as pending (sub, display name,
   first seen) and the person gets a "requested access" page. They are signed
   in to nothing.
3. The owner approves with a role, denies, or later revokes, at `/users`.

Revocation is enforced per request, not at sign-in - otherwise someone just
revoked keeps a working session for the full fourteen-day lifetime. The
session carries the `sub` only; the role is read from the allowlist on every
request.

## Shape

- `dashboard/oidc.py` - discovery (cached), authorize URL with state and PKCE,
  code exchange, userinfo. Knows nothing about who is allowed.
- `dashboard/users.py` - the allowlist: roles, pending queue, approve, deny,
  revoke, owner binding. Knows nothing about OIDC.

Kept apart so `app.py` (695 lines) does not absorb this, and so each can be
tested without the other.

Routes: `GET /login` (the button), `GET /auth/start`, `GET /callback`,
`GET /auth/pending`, `GET /users`, `POST /api/users/<action>`, `GET /logout`.

Sessions keep the existing `ClockTolerantSessions`.

## Cutover

The password is removed in its own commit, last, only once login works end to
end. Removing it earlier locks the operator out of their own control panel,
and the way back in is `docker exec` by hand.

## Testing

- Allowlist: approve, deny, revoke, role change, unknown identity.
- Owner: bound from the hint on first login, then keyed on `sub`; a rename
  does not transfer it; not claimable by whoever signs in first.
- Guard: viewer POST is 403, operator POST passes, pending reaches neither,
  a revoked identity holding a live cookie is refused on its next request.
- Guard read exceptions: viewer refused `GET /settings`, non-owner refused
  `GET /users` - the two cases the POST rule does not catch.
- OIDC: `state` mismatch rejected, PKCE verifier sent, callback driven against
  a stubbed token and userinfo endpoint. No network in tests.

## Known limitation, accepted

The proxy last hop to this container is plain HTTP across the LAN, and the
dashboard remains directly reachable at `http://192.168.178.19:9200`, where
there is no TLS at all. Recorded so it is not mistaken for an oversight.
