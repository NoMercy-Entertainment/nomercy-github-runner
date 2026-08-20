# Dashboard authentication and access control

Status: design, approved in outline. Not implemented.
Supersedes: the shared-password login in `dashboard/app.py`.

## Why

The dashboard currently authenticates with one shared password over plain
HTTP, and it is about to be published to the LAN (192.168.178.0/24). One
secret shared by everyone who should ever have access cannot be revoked for
one person, records nothing about who did what, and has no answer for "let
this person look but not touch".

The first proposal here was "sign in with GitHub, restricted to members of
NoMercy-Entertainment". That was wrong and was rejected during design. Org
membership is a set, not an access rule: it currently resolves to six people
plus one outside collaborator, all of whom would have been able to stop,
remove and recreate runners. **Being able to prove who you are must not, on
its own, grant access to anything.**

## Model

Two separate layers. Conflating them is the mistake above.

**Authentication** answers *who are you* and produces a stable identity key
`provider:subject`. Two providers:

- `github` - GitHub OAuth. Not OIDC: no discovery document, so a small
  adapter exchanges the code and reads `GET /user`.
- `oidc` - a generic OpenID Connect client, configured entirely from `.env`
  (discovery URL, client id, client secret). `auth.nomercy.tv` is therefore
  configuration, not code, and any other compliant IdP works unchanged.

**Authorization** answers *may you*, and is an explicit allowlist in
`/data/users.json`. Nothing else grants access. Not org membership, not
holding a nomercy.tv account, not having authenticated successfully.

### Identity key

Keyed on the provider's immutable subject: GitHub's numeric `id`, OIDC's
`sub`. Never the username or the email address.

A GitHub username can be changed, which releases the old name for anyone else
to register. An allowlist keyed on `login` would hand the new owner of that
name the old holder's access, silently and with no event to notice. Emails
are reassignable in some directories for the same reason. The numeric id
cannot be transferred.

### Roles

| Role | May |
|---|---|
| `owner` | everything, plus approve/deny/revoke and set roles |
| `operator` | everything except user management |
| `viewer` | read only: fleet, runner detail, logs, engine, history |

Enforced in the existing `before_request` guard rather than per endpoint.
Every mutating route in this codebase is already a POST and every read is a
GET, so the rule is one condition in one place: a POST from a `viewer` is
403. A check that must be repeated at fifteen call sites is a check that will
eventually be forgotten at one of them.

That rule alone is not quite enough, and the exception is named rather than
left implicit. Two routes are reads that are not for everyone:

- `GET /settings` - operator and above. The real token never reaches the
  browser (the template renders `token_mask`, first four and last four
  characters), but the org, labels, group and limits are configuration, and a
  partially disclosed token is still a disclosure. A viewer is there to see
  why a build failed.
- `GET /users` - owner only.

Both are enumerated in one place next to the POST rule, so the guard reads as
a single policy rather than a general rule with forgotten holes.

### Owner bootstrap

Seeded, not claimed: `DASH_OWNER=github:6890678` in `.env`.

Deliberately not "the first account to sign in becomes owner". Once this is
published to the LAN that is a race, and losing it hands over the fleet. A
seeded owner is also independent of the data volume: if `users.json` is lost,
the owner can still get in and re-approve everyone.

### Access request flow

1. Unknown identity authenticates successfully.
2. Guard finds no allowlist entry - the request is recorded as pending
   (provider, subject, display name, first seen) and the person gets a
   "requested access" page. They are not signed in to anything.
3. The owner sees pending requests on `/users` and approves with a role,
   denies, or later revokes.

Revocation is enforced per request, not at sign-in. Otherwise a person whose
access was just withdrawn keeps a valid session cookie for the full session
lifetime, which is fourteen days.

## Shape

New modules, to keep `app.py` (695 lines) from absorbing this:

- `dashboard/identity.py` - the two providers. Given a callback request,
  returns `(provider, subject, display_name)` or an error. Knows nothing
  about who is allowed.
- `dashboard/users.py` - the allowlist: load/save, roles, pending queue,
  approve/deny/revoke. Knows nothing about OAuth.

Routes: `/auth/<provider>/start`, `/auth/<provider>/callback`, `/auth/pending`,
`/users` (owner only), `/api/users/<action>` (owner only), `/logout`.

Sessions keep the existing `ClockTolerantSessions`. The session carries the
identity key only; the role is read from the allowlist on every request, so a
role change or a revoke takes effect immediately.

### Dependency

`authlib` is added to the image for the OIDC client: discovery, PKCE, `state`,
`nonce`, and JWKS signature validation. Hand-rolling any of those is how OIDC
integrations end up accepting tokens they should not. The GitHub adapter needs
none of it beyond `state`.

## Cutover

The password is removed in its own commit, last, only once both providers have
been verified working end to end. Removing it earlier locks the operator out
of the control panel for their own fleet, and the only way back in is
`docker exec` by hand.

## Testing

- Allowlist: approve, deny, revoke, role changes, unknown identity.
- Guard: viewer POST is 403, operator POST passes, a pending identity reaches
  neither, and a revoked identity holding a live cookie is refused on its next
  request rather than at its next sign-in.
- Guard, read exceptions: viewer is refused `GET /settings`, non-owner is
  refused `GET /users`. Pinned by their own tests, because they are the two
  cases the POST rule does not catch.
- Owner seed: honoured when `users.json` is absent; not claimable by whoever
  signs in first.
- Callbacks: driven against a stubbed provider, no network in tests.
- Identity: a changed username does not move access; the subject is what is
  stored.

## Known limitation, accepted

This runs over plain HTTP on the LAN, so the OAuth `code` and the session
cookie cross the network unencrypted. The firewall rule is scoped to
192.168.178.0/24. Terminating TLS is a separate decision and is not in scope
here; it is recorded so it is not mistaken for an oversight.

## Open items, needed to run - not to build

- OIDC discovery URL for `auth.nomercy.tv` (`/.well-known/openid-configuration`
  on the bare host returns 404, so it is elsewhere - under Keycloak it is
  `/realms/<realm>/.well-known/openid-configuration`), plus client id/secret.
- A GitHub OAuth App under NoMercy-Entertainment. An OAuth App accepts a
  single callback URL, and `localhost` and `192.168.178.19` are different
  hosts; the LAN URL is canonical. A DHCP reservation for the host is assumed,
  or the callback breaks on a new lease.
