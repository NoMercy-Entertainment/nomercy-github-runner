"""NoMercy Runners - control dashboard.

Serves controller fleets, settings, history and authenticated worker reads.
Every runner is reached through the controller and its worker agents; this
process talks to no container engine of its own.

Auth: single sign-on against Keycloak (oidc.py) proves who someone is; the
allowlist in users.py decides what they may do. There is no password here.
Sessions are Flask's signed cookies over a secret generated once and persisted,
so restarting the dashboard does not log you out.

This is also reachable over plain HTTP on the LAN by request. The GitHub token
then crosses the network unencrypted; the UI says so rather than letting it be
forgotten.
"""

import hmac
import json
import os
import re
import secrets
import threading
import time

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for)
from flask.sessions import SecureCookieSessionInterface
from flask_sock import Sock
from itsdangerous import BadSignature

import api_v2
import history
import oidc
import providers
import users

DATA = os.environ.get("DASH_DATA", "/data")
PORT = int(os.environ.get("DASH_PORT", "9200"))
ENV_PATH = os.environ.get("ENV_PATH", "/repo/.env")
SECRET_PATH = os.path.join(DATA, "secret.key")

app = Flask(__name__)

# ping/pong keeps the connection open through nginx, whose default
# proxy_read_timeout is 60s and which would otherwise cut an idle - that
# is to say, a correctly quiet - socket.
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)


# --------------------------------------------------------------------------
# secrets / auth
# --------------------------------------------------------------------------

def _secret_key():
    if os.path.exists(SECRET_PATH):
        return open(SECRET_PATH, "rb").read()
    key = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as fh:
        fh.write(key)
    os.chmod(SECRET_PATH, 0o600)
    return key


app.secret_key = _secret_key()


class ClockTolerantSessions(SecureCookieSessionInterface):
    """Session cookies that survive the clock being corrected backwards.

    This runs in a WSL distro whose clock is periodically pulled back by ~12
    seconds. Flask stamps the session cookie with the time it was signed, and
    itsdangerous refuses any cookie stamped after the current clock -
    "Signature age -12 < 0 seconds" - so a single backward jump invalidates
    every live session at once and logs everyone out mid-click. That was the
    whole of a long-standing "why do I keep getting logged out" complaint.

    A stamp in the future can only ever be our own clock's doing: the stamp is
    inside the HMAC, so a client cannot move it without the secret key. There
    is therefore nothing to defend against by rejecting it, and the check costs
    real sessions. Age is verified in one direction only - genuinely expired
    cookies are still refused below, and an unsigned or edited cookie still
    fails the signature check exactly as before.
    """

    def open_session(self, app, request):
        s = self.get_signing_serializer(app)
        if s is None:
            return None
        val = request.cookies.get(self.get_cookie_name(app))
        if not val:
            return self.session_class()
        try:
            # max_age=None verifies the signature but skips itsdangerous' own
            # age checks, so the lifetime can be enforced here instead - where
            # a backward clock jump is distinguishable from a stale cookie.
            data, issued = s.loads(val, max_age=None, return_timestamp=True)
        except BadSignature:
            return self.session_class()
        # time.time() is the clock itsdangerous stamps the cookie with, so
        # signing and expiry are measured against one clock. Comparing against
        # a second, independent clock is what this class exists to undo.
        age = time.time() - issued.timestamp()
        if age > app.permanent_session_lifetime.total_seconds():
            return self.session_class()
        return self.session_class(data)


app.session_interface = ClockTolerantSessions()


def request_is_secure():
    """Whether this particular request reached the browser over TLS.

    Two ways in: directly on http://192.168.178.19:9200, and through a reverse
    proxy that terminates TLS at https://gh-runners.phillippepelzer.me. Which
    one was used is a property of the request, not of the deployment, so it
    cannot be a constant in a template.

    X-Forwarded-Proto is spoofable by anyone who can reach this app directly,
    and that is acceptable *here*: the only thing it decides is whether a
    warning is displayed, so forging it hides a warning from the forger and
    from nobody else. Do not reuse this to build an OAuth redirect_uri or any
    absolute URL - those must come from pinned configuration, or a forged
    header redirects the authorization code somewhere it should not go.
    """
    # A chain of proxies appends: "https,http". The client-facing one is first.
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def _conf(key):
    """Deployment configuration: real environment first, then .env."""
    return (os.environ.get(key) or read_env().get(key) or "").strip()


def _public_url():
    """The base URL browsers actually use, pinned rather than sniffed.

    Never derived from Host or X-Forwarded-Host. This app is also reachable
    directly on the LAN address, where those headers are attacker-controlled,
    and the OAuth redirect_uri is built from this - a forged header would send
    an authorization code to a host of the attacker's choosing.
    """
    return _conf("DASH_PUBLIC_URL").rstrip("/")


def _oidc():
    base = _public_url()
    return oidc.OIDC(_conf("OIDC_ISSUER"), _conf("OIDC_CLIENT_ID"),
                     _conf("OIDC_CLIENT_SECRET"),
                     f"{base}/callback" if base else "")


def current_role():
    """The caller's role right now, or None for "not allowed".

    Read from the allowlist on every request rather than trusted from the
    session, so a revoke or a downgrade lands on the next click instead of
    whenever a fourteen-day cookie happens to expire.
    """
    return users.role_of(session.get("sub"))


def _forbid(why):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=why), 403
    return render_template("forbidden.html", why=why), 403


# Reachable without a session: the sign-in page and the round trip to the IdP.
OPEN_PATHS = {"/login", "/auth/start", "/callback", "/auth/pending"}

#: API versions served. v1 is the existing surface, aliased so the new
#: id-keyed API can land beside it instead of replacing it in one step.
API_VERSIONS = ("v1",)

#: The shape of a fleet frame on the websocket. Reported by /api/version and
#: stamped on every frame by wire_frame().
#:
#: Bump this whenever the meaning of `data` changes in a way a client cannot
#: absorb silently. A socket outlives a deploy: a browser tab that connected
#: before an upgrade keeps receiving frames written by the new code, and
#: without a number in the frame its only way to notice is to misread them.
FLEET_SCHEMA = 1

_VERSION_PREFIXES = tuple(f"/api/{v}/" for v in API_VERSIONS)

#: Every API version this dashboard serves. v2 is its own route family
#: (api_v2.py), keyed by runner_id and fleet_id, not an alias of v1.
SERVED_VERSIONS = API_VERSIONS + ("v2",)


def policy_path(path):
    """The path authorisation decisions are made against.

    Version prefixes are stripped first, so `/api/v1/users/approve` is judged
    exactly as `/api/users/approve`. Without this, aliasing would silently
    open a second door to every path-scoped rule below: `/api/users/` is
    admin-only, and an alias that did not match that prefix would be
    admin-only no longer.
    """
    for prefix in _VERSION_PREFIXES:
        if path.startswith(prefix):
            return "/api/" + path[len(prefix):]
    return path


@app.context_processor
def _template_role():
    """Let templates hide what the caller cannot use.

    Taken from g, which guard() already resolved, rather than re-reading the
    allowlist per render. Absent on the open pages, where there is no role -
    hence the default.
    """
    return {"role": getattr(g, "role", None)}


def _secret_values():
    """The deployment's own secret values, for masking wherever they
    appear. Read from the environment file on each use, so a token changed
    in Settings is covered at once."""
    from control import redact
    env = dict(read_env())
    for key in ("GH_TOKEN", "FORGEJO_API_TOKEN", "OIDC_CLIENT_SECRET"):
        if os.environ.get(key):
            env.setdefault(key, os.environ[key])
    values = redact.secret_values(env)
    # An overridden deployment token can still occur in a worker's old logs.
    values += redact.secret_values(dict(os.environ))
    # And the tokens the control plane's own store holds (T-1901), so a
    # value set there is masked everywhere too.
    try:
        from store import schema as control_schema
        if os.path.exists(control_schema.DB_PATH):
            from control.secrets import SecretStore
            values += list(SecretStore(control_schema.DB_PATH)
                           ._values().values())
    except Exception:   # noqa: BLE001 - masking must never break a response
        pass
    redact.remember(*values)
    return tuple(values) + redact.known()


@app.after_request
def _redact_response(response):
    """NFR-4, enforced once for every API route (T-1801): a secret field is
    masked by name and a secret value wherever it appears. A route that
    returns a token by accident still returns it masked."""
    if not request.path.startswith("/api/") or not response.is_json:
        return response
    data = response.get_json(silent=True)
    if data is None:
        return response
    from control import redact
    clean = redact.redact_payload(data, _secret_values())
    if clean != data:
        response.set_data(json.dumps(clean))
    return response


#: Design 18.2's destroy group: what only an admin may do (T-1902). Paths as
#: policy_path gives them, so a /api/v1 alias is judged the same. Capacity
#: decreases are judged in their route, which knows the fleet's current
#: capacity; everything else in the group is recognised here, in one place.
DESTROY_PATHS = (
    re.compile(r"^/api/v2/runners/[^/]+/actions/"
               r"(remove|recreate|deregister)$"),
    re.compile(r"^/api/v2/fleets/[^/]+/recreate$"),
)


def destroys(method, path):
    return method == "POST" and any(p.match(path) for p in DESTROY_PATHS)


def _refuse_destroy(role, path):
    """Refused, and recorded with the actor when the control plane keeps an
    audit - a refused destroy is what an operator needs to find later."""
    try:
        from store import schema as control_schema
        if os.path.exists(control_schema.DB_PATH):
            from control import audit
            audit.record(control_schema.DB_PATH, path.rsplit("/", 1)[-1],
                         "refused", actor=session.get("email")
                         or session.get("sub") or "unknown",
                         outcome=f"requires admin; the caller is {role}",
                         parameters={"path": path})
    except Exception:   # noqa: BLE001 - the refusal stands either way
        pass
    return _forbid("Removing, recreating or deregistering needs the admin "
                   "role.")


@app.before_request
def guard():
    if request.path.startswith("/static"):
        return None
    if request.path in OPEN_PATHS:
        return None

    # Judged on the version-stripped path, so an /api/v1 alias inherits every
    # rule below rather than slipping past the ones that match on a prefix.
    path = policy_path(request.path)

    role = g.role = current_role()
    if role is None:
        # API callers get JSON; a browser gets the login page.
        if path.startswith(("/api/", "/ws/")):
            # A WebSocket client cannot read a redirect to a login page.
            return jsonify(error="not authenticated"), 401
        return redirect(url_for("login"))

    # One policy, read top to bottom. Every mutating route here is a POST and
    # every read a GET, so the general rule is a single condition - but two
    # reads are not for everyone, and they are named rather than left to be
    # noticed later.
    if path == "/users" or path.startswith("/api/users/"):
        if role != "admin":
            return _forbid("Only an admin can manage access.")
    elif path.startswith("/api/v2/secrets"):
        if role != "admin":
            return _forbid("Only an admin can see or set the forge tokens.")
    elif path == "/settings" and role == "viewer":
        return _forbid("Settings are not available with read-only access.")
    elif request.method == "POST" and role == "viewer":
        return _forbid("Your access is read-only.")
    elif destroys(request.method, path) and role != "admin":
        return _refuse_destroy(role, path)
    return None


# --------------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------------

def read_env():
    env = {}
    try:
        for line in open(ENV_PATH):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    # Controller deployments have no /repo mount. Deployment configuration
    # remains the base; persisted tokens override it without a restart.
    if not os.path.exists(ENV_PATH):
        env = dict(os.environ)
    from store import schema
    if os.path.exists(schema.DB_PATH):
        from control.secrets import SecretStore
        env = SecretStore(schema.DB_PATH).overlay(env)
    return env


# --------------------------------------------------------------------------
# background: history from the worker agents
# --------------------------------------------------------------------------

#: The v1 collector's snapshot, kept as the empty shape api_v2 reads. The
#: dashboard no longer collects anything itself - every runner reaches it
#: through the controller - but `cards.from_legacy` and the v2 fleet read
#: this dict, so it stays as the "nothing here" answer rather than becoming
#: a special case inside every reader.
_status = {"generated": "", "disk": {}, "runners": []}
# A condition, not a plain lock: fleet subscribers wait on it rather than
# asking every few seconds. The generation counter is what lets a
# subscriber tell "nothing published yet" from "published while I was
# sending".
_status_lock = threading.Condition()
_status_gen = 0


def _controller_collector():
    """Collect history through authenticated worker agents, independently of the UI."""
    from concurrent.futures import ThreadPoolExecutor
    last_read = {}
    mapped = False
    global _status_gen
    with ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            try:
                service, _ = api_v2.control_plane()
                if service is not None:
                    specs = [s for s in service.specs.list() if s.get("exec_unit_ref")
                             and s.get("actual_state") != "absent"]
                    if not mapped:
                        for spec in specs:
                            history.record_controller_logs(spec, "")
                    now = time.time()
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    def collect(spec):
                        rid = spec["runner_id"]
                        since = min(86400, max(60, int(now - last_read.get(rid, now - 86400)) + 30))
                        text = service.fetch_logs(rid, since_seconds=since)
                        history.record_controller_logs(spec, text, stamp)
                        return rid
                    futures = [pool.submit(collect, spec) for spec in specs]
                    for future in futures:
                        try:
                            last_read[future.result()] = now
                        except Exception as e:  # one unavailable worker must not stop others
                            print(f"[controller-history] {type(e).__name__}: {e}")
                    if not mapped:
                        # Current names have been linked first. Only genuinely
                        # legacy names receive historical, deleted identities.
                        history.backfill_runner_ids(service.specs)
                        mapped = True
                with _status_lock:
                    _status_gen += 1
                    _status_lock.notify_all()
            except Exception as e:
                print(f"[controller-collector] {type(e).__name__}: {e}")
            time.sleep(5)


def _enricher():
    """Fill in repo / workflow / branch / commit, and - for Forgejo - the end.

    Separate from the collector: an API sweep can take seconds and must not
    delay telemetry. Best-effort throughout.
    """
    while True:
        try:
            env = read_env()
            # Compared as strings: both sides are the same fixed ISO format,
            # which is how started_at is already compared elsewhere in this
            # codebase.
            stale_before = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 24 * 3600))
            _enrich_pending(env, stale_before)
        except Exception as e:  # noqa: BLE001
            print(f"[enricher] {e}")
        time.sleep(90)


def _enrich_pending(env, stale_before):
    """Run one enrichment sweep over history.pending_enrichment().

    Split out from _enricher() so the two invariants that actually matter
    here - Forgejo closing before it enriches, and a miss being given up on
    only after the staleness cutoff rather than on the first try - can be
    driven directly by a test. Neither survives being buried in a `while
    True: ... time.sleep(90)` loop, and a broken version of either would
    still pass every test that only exercises the parser.
    """
    clients = {}
    for run in history.pending_enrichment(limit=10):
        key = run.get("provider") or "github"
        if key not in clients:
            p = providers.by_key(key)
            clients[key] = p.forge_client(env) if p else None
        client = clients[key]
        if client is None:
            continue

        if key == "forgejo":
            found = client.find_task(run.get("job_name"),
                                     run.get("forge_task_id"),
                                     run.get("started_at"))
            # A task with no ended_at is treated the same as no match at
            # all, not enriched. Forgejo's task carries the end, which no
            # log line does, so this is the only path a Forgejo run ever
            # closes on - enriching it here regardless would create a run
            # that is enriched but never closes, which is the exact
            # failure the close-before-enrich ordering below exists to
            # avoid, reached from a different direction.
            if found and found.get("ended_at"):
                # Close first, then enrich: a crash between the two leaves
                # a closed run to be enriched next sweep, rather than an
                # enriched run that never ends.
                history.apply_close(run["id"], found["ended_at"],
                                    found.get("conclusion"))
                history.apply_enrichment(run["id"], found)
            elif run["started_at"] < stale_before:
                # Not marked unmatched on the first miss: the common
                # case is a task that simply has not finished yet, and
                # marking it would close the only route to its end
                # time for good.
                #
                # But a run that never matches would otherwise be
                # retried every 90 seconds for the life of the
                # deployment - a repo deleted mid-job, or a task
                # Forgejo pruned. After a day, close it honestly as
                # Unknown and stop asking.
                #
                # "Unknown", not one of Succeeded/Failed/Canceled: we
                # never found out how this run ended, and picking any
                # of the three would fabricate an outcome. Capitalised
                # to sit in the same vocabulary as those three, which
                # is what templates/history.html styles and filters on
                # - it now knows this word too, and counts it toward
                # the run total and toward no result tile, which is
                # the honest answer.
                history.apply_close(run["id"], run["started_at"],
                                    "Unknown")
                history.mark_unmatched(run["id"])
        else:
            found = client.find_job(run.get("registration"),
                                    run.get("job_name"),
                                    run.get("started_at"),
                                    run.get("ended_at"))
            if found:
                history.apply_enrichment(run["id"], found)
            else:
                history.mark_unmatched(run["id"])


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.route("/login")
def login():
    return render_template("login.html", secure=request_is_secure(),
                           oidc_ready=_oidc().configured())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# single sign-on
# --------------------------------------------------------------------------

@app.route("/auth/start")
def auth_start():
    client = _oidc()
    if not client.configured():
        return render_template(
            "login.html", secure=request_is_secure(), oidc_ready=False,
            error="Single sign-on is not configured on this dashboard."), 500
    try:
        url, state, verifier = client.authorize_url()
    except oidc.OIDCError as e:
        return render_template(
            "login.html", secure=request_is_secure(), oidc_ready=True,
            error=f"Could not reach the identity provider: {e}"), 502
    session["oidc_state"] = state
    session["oidc_verifier"] = verifier
    return redirect(url)


@app.route("/callback")
def auth_callback():
    # Popped, not read: a state is good for exactly one callback, so a
    # replayed or forwarded callback URL cannot be used a second time.
    state = session.pop("oidc_state", None)
    verifier = session.pop("oidc_verifier", None)
    offered = request.args.get("state", "")

    if not state or not verifier or not hmac.compare_digest(state, offered):
        # No state of ours means this sign-in did not begin here - which is
        # what CSRF against a callback looks like.
        return render_template(
            "forbidden.html",
            why="This sign-in did not start here. Try again."), 400

    code = request.args.get("code", "")
    if not code:
        return render_template(
            "forbidden.html",
            why=request.args.get("error_description")
                or request.args.get("error")
                or "The identity provider returned no authorization code."), 400

    try:
        claims = _oidc().exchange(code, verifier)
    except oidc.OIDCError as e:
        return render_template("forbidden.html", why=str(e)), 400

    role = users.sign_in(claims["sub"],
                         claims.get("preferred_username", ""),
                         claims.get("name", ""))
    if role is None:
        # Authenticated, and allowed nothing. No session is created.
        return redirect(url_for("auth_pending"))

    # Cleared first: a session that changes who it belongs to must not carry
    # anything the previous holder put in it.
    session.clear()
    session["sub"] = claims["sub"]
    session["name"] = (claims.get("name")
                       or claims.get("preferred_username") or "")
    session.permanent = True
    return redirect(url_for("index"))


@app.route("/auth/pending")
def auth_pending():
    return render_template("pending.html")


# --------------------------------------------------------------------------
# access management (admin only - enforced in guard())
# --------------------------------------------------------------------------

@app.route("/users")
def users_page():
    return render_template("users.html", people=users.list_users(),
                           waiting=users.pending(), roles=users.ROLES,
                           me=session.get("sub"))


@app.route("/api/users/<action>", methods=["POST"])
def api_users(action):
    body = request.get_json(silent=True) or {}
    sub = (body.get("sub") or "").strip()
    if not sub:
        return jsonify(ok=False, error="no sub given"), 400

    # The account that hands out access must not be able to strand itself.
    # There is no second way in: no password, and the bootstrap only reopens
    # once every admin is gone.
    if sub == session.get("sub") and action in ("revoke", "approve"):
        return jsonify(
            ok=False,
            error="You cannot change your own access."), 400

    if action == "approve":
        try:
            users.approve(sub, (body.get("role") or "").strip())
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
    elif action == "deny":
        users.deny(sub)
    elif action == "revoke":
        users.revoke(sub)
    else:
        return jsonify(ok=False, error="unknown action"), 404
    return jsonify(ok=True)


@app.route("/")
def index():
    """The fleet page, at the address people type.

    A redirect rather than a second render of the same template: /v2 is where
    the fleet page lives and where every link in it points, so one address
    owns it and a bookmark of either lands on the same page.
    """
    return redirect("/v2")


@app.route("/history")
def history_page():
    return render_template(
        "history.html",
        runners=history.distinct("runner"),
        jobs=history.distinct("job_name"),
    )


@app.route("/api/history")
def api_history():
    return jsonify(runs=history.list_runs(
        runner=request.args.get("runner") or None,
        job=request.args.get("job") or None,
        result=request.args.get("result") or None,
        limit=min(int(request.args.get("limit", 100)), 500),
        offset=int(request.args.get("offset", 0)),
    ))


@app.route("/api/history/summary")
def api_history_summary():
    return jsonify(history.summary())


@app.route("/api/history/run/<int:run_id>")
def api_history_run(run_id):
    run = history.get_run(run_id)
    return (jsonify(run), 200) if run else (jsonify(error="not found"), 404)


@app.route("/settings")
def settings():
    """Per-fleet defaults, held by the control plane.

    There is nothing to read from .env here any more: what a runner is made
    with is a fleet row, written through /api/v2/settings/<fleet_id>.
    """
    return render_template("settings_v2.html", role=g.role)


# --------------------------------------------------------------------------
# live push
# --------------------------------------------------------------------------

def wire_frame(frame):
    """A frame as it goes on the wire.

    `fleet_frames` decides WHAT changed; this decides how it is framed. Keeping
    the two apart is what lets the envelope gain fields without the diff logic
    and its tests having an opinion about the transport.

    The schema number rides on every frame, not only the snapshot, so a frame
    is self-describing on its own - in a log line, in a replay, or after a
    reconnect that lands on a freshly deployed dashboard.
    """
    return {"schema": FLEET_SCHEMA, **frame}


def encode_frame(frame):
    """A frame as the socket sends it: framed, then redacted exactly as every
    API response is (T-1801)."""
    from control import redact
    return json.dumps(redact.redact_payload(wire_frame(frame),
                                            _secret_values()))


@sock.route("/ws/v2/fleet")
def ws_fleet_v2(ws):
    sub = session.get("sub")
    previous = None
    try:
        while True:
            role = users.role_of(sub)
            if role is None:
                return
            g.role = role
            snapshot = api_v2.fleet_snapshot()
            comparable = {k: v for k, v in snapshot.items() if k != "generated"}
            if comparable != previous:
                ws.send(encode_frame({"type": "snapshot", "data": snapshot}))
                previous = comparable
            with _status_lock:
                _status_lock.wait(timeout=5)
    except Exception:
        pass


@app.route("/api/control/workers")
def api_control_workers():
    """The control plane's workers, with each one's health and why.

    Read-only, and it creates nothing: until the control plane has run there
    is no database to read, and opening one here would create an empty file in
    the dashboard's volume that nothing asked for. So an absent database is an
    empty list with a note, not an error and not a new file.

    The reason column is the point (T-0406). A worker marked degraded because
    it speaks a protocol major the controller does not is shown with both
    versions, rather than as a worker that merely went quiet.
    """
    from store import schema as control_schema
    if not os.path.exists(control_schema.DB_PATH):
        return jsonify(workers=[], note="the control plane has not run yet")
    from control.inventory import Inventory
    try:
        workers = Inventory(control_schema.DB_PATH).summary()
    except Exception:   # noqa: BLE001 - a half-made database reads as empty
        return jsonify(workers=[], note="the control database is not ready")
    return jsonify(workers=workers)


@app.route("/api/version")
def api_version():
    """What this dashboard serves, so a client can tell before it calls.

    A client that guesses gets a 404 it cannot distinguish from a route that
    was removed; this makes the difference visible.
    """
    return jsonify(api=list(SERVED_VERSIONS), schema=FLEET_SCHEMA)


def _register_version_aliases():
    """Serve every /api route a second time under /api/v1.

    Done by walking the map rather than by adding a decorator to twenty
    handlers: one place to read, and no route can be forgotten. The alias
    reuses the same view function, so the two cannot drift apart.

    /api/version itself is not aliased - it is how a client discovers the
    versions, so it must not live inside one.
    """
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith("/api/"):
            continue
        if rule.rule == "/api/version":
            continue
        if any(rule.rule.startswith(p) for p in _VERSION_PREFIXES):
            continue
        for version in API_VERSIONS:
            alias = f"/api/{version}" + rule.rule[len("/api"):]
            app.add_url_rule(
                alias,
                endpoint=f"{version}__{rule.endpoint}",
                view_func=app.view_functions[rule.endpoint],
                methods=sorted(rule.methods - {"HEAD", "OPTIONS"}),
            )


_register_version_aliases()


def _status_snapshot():
    with _status_lock:
        return _status


# Registered after the aliases, so no v2 route is ever served again under v1.
api_v2.init(app, _status_snapshot)


@app.route("/v2")
def fleet_v2_page():
    """The fleet page: six fleets from data, one card for every runner.
    `/` redirects here."""
    return render_template("fleet_v2.html")


@app.route("/runners/<runner_id>")
def runner_v2_page(runner_id):
    """One runner by its runner_id, read from the controller (T-1406)."""
    try:
        import store.storage as storage
        runner_id = storage.check(runner_id)
    except Exception:   # noqa: BLE001 - not a runner id is not a runner
        return render_template("forbidden.html",
                               why="That is not a runner id."), 404
    return render_template("runner_v2.html", runner_id=runner_id)


if __name__ == "__main__":
    # Every log line this process writes goes out with secret values masked
    # (T-1801).
    from control import redact as _redact
    _redact.install_log_redaction(_secret_values)
    history.init()
    # History is written from the workers' own logs, read over the agent
    # protocol. There is no local engine to collect from, and losing this
    # thread is losing the job history for every controller-managed runner.
    threading.Thread(target=_controller_collector, daemon=True).start()
    threading.Thread(target=_enricher, daemon=True).start()
    app.permanent_session_lifetime = 60 * 60 * 24 * 14
    app.run(host="0.0.0.0", port=PORT, threaded=True)
