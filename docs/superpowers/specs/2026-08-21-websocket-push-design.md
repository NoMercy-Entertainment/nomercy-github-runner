# Push instead of polling

Status: design agreed. Implementation in progress.

## What is being replaced

Five polling loops:

| Page | Endpoint | Every |
|---|---|---|
| index | `/api/status` | 5s |
| runner | `/api/status` | 5s |
| runner | `/api/runner/<n>/series` | 5s |
| runner | `/api/runner/<n>/logs` | 2s |
| runner | `/api/runner/<n>/inspect` | 10s |

The fleet status is already computed once every five seconds by the background
collector and served from a cache, so the status polls are cheap - what they
cost is latency, up to a full interval between a runner changing state and the
grid showing it. The log poll is the one that is genuinely wasteful: a
`docker logs --since` process every two seconds per open tab.

## Scheme

`wss://` through the proxy, `ws://` on the LAN address. The client derives it
from `location.protocol` rather than being configured, so both entry points
work without a second setting to keep in sync.

## Endpoints

**`/ws/fleet`** - pushes the status snapshot as the collector produces it.
Replaces both status polls.

**`/ws/runner/<name>`** - pushes new series points, new log lines and the
inspect snapshot for one runner. Replaces the other three.

Only streams move. Engine info, history, and every action button stay on
plain HTTP: they are one-shot, user-triggered request/response, and putting
them on a socket means building correlation IDs and a state machine on top of
something HTTP already does correctly.

### Incremental, not repeated

The socket sends what is new, not the whole picture again. A new series point
rather than 120 of them; log lines past a server-held cursor rather than the
tail. The page still fetches the initial history over HTTP on load - a stream
is for what happens next, and making it also do backfill is what turns a
socket into a slower HTTP.

The log tail is only produced while the client says it is watching, so the
detail page does not spend a `docker logs` per two seconds on a tab nobody
has open. One `{"watch": {...}}` message from the client, no more protocol
than that.

## Authorization, and the hole this would otherwise punch

The access model rests on the role being re-read from the allowlist on every
request, which is why a revoke lands on the next click rather than when a
fourteen-day cookie expires.

**A WebSocket is one request that stays open for hours.** Left alone, that
silently converts "revoked immediately" into "revoked when they close the
tab", and the tests that pin the guarantee would still pass, because they
exercise HTTP.

So the socket loop re-reads the role on every tick and closes the connection
when it is gone. That is not an optimisation to add later; it is the thing
that keeps the model true, and it gets its own test.

The handshake itself goes through `before_request` like any other route, so
an unauthenticated connection is refused there. The guard's "API callers get
JSON" branch is extended to `/ws/` so a failed handshake gets a 401 rather
than a redirect to a login page no WebSocket client will read.

Both endpoints are reads, so `viewer` may open them. Only revocation matters.

## Falling back is a requirement, not a nicety

nginx does not forward `Upgrade` unless told to. If it is not configured, the
socket works on `http://192.168.178.19:9200` and fails through the proxy -
the same dashboard, broken for one way in and not the other.

The client therefore keeps the existing polling as a fallback: it tries the
socket, and on failure or repeated disconnection falls back to `setInterval`
and says so in the header. For an operations dashboard, silently showing
five-minute-old numbers is a worse failure than any amount of polling. The
existing `STALE_MS` indicator stays for the same reason.

Reconnection is backed off rather than immediate, so a dashboard left open
against a stopped container does not hammer it on restart.

## Dependency

`flask-sock`, which brings `simple-websocket`. Both are small, and flask-sock
supports the Werkzeug development server this runs on, so the serving model
does not change.

This is the first dependency added beyond Flask. It buys a protocol that
cannot be sensibly hand-rolled - unlike the OIDC client, where the argument
went the other way and no library was added.

## Order

1. `/ws/fleet`, with the auth re-check, the client fallback, and the
   reconnection policy. Smallest change that proves the transport end to end,
   including through nginx.
2. `/ws/runner/<name>` for series, logs and inspect.

Split because the first one answers the question the second one depends on:
whether the proxy passes upgrades at all.

## Testing

- The handshake is refused for an identity with no role.
- A socket open when its identity is revoked closes, rather than continuing
  to send - the property the whole access model rests on.
- A viewer may open both endpoints.
- The fleet socket sends a snapshot on connect and again when the collector
  produces a new one, and does not resend an unchanged one.
- The client falls back to polling when the socket cannot be opened.
