"""NoMercy Runners - control dashboard.

Serves the status page, the settings page, and the control endpoints for the
runners on this engine.

Auth: single sign-on against Keycloak (oidc.py) proves who someone is; the
allowlist in users.py decides what they may do. There is no password here.
Sessions are Flask's signed cookies over a secret generated once and persisted,
so restarting the dashboard does not log you out.

This is also reachable over plain HTTP on the LAN by request. The GitHub token
then crosses the network unencrypted; the UI says so rather than letting it be
forgotten.
"""

import collections
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

import docker_ops as ops
import github_api
import history
import oidc
import runner_detail
import users
from runner_detail import SECRET_KEYS, mask

DATA = os.environ.get("DASH_DATA", "/data")
PORT = int(os.environ.get("DASH_PORT", "9200"))
ENV_PATH = os.environ.get("ENV_PATH", "/repo/.env")
SECRET_PATH = os.path.join(DATA, "secret.key")

# Only these may be written from the UI. An allowlist rather than "write
# whatever was posted": the .env is read by the container runtime, and a typo'd
# or injected key there is a foothold, not a cosmetic bug.
EDITABLE = {
    "GH_TOKEN", "GITHUB_ORG", "RUNNER_LABELS",
    "RUNNER_GROUP", "RUNNER_CPU_LIMIT", "RUNNER_MEM_LIMIT",
}

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


@app.context_processor
def _template_role():
    """Let templates hide what the caller cannot use.

    Taken from g, which guard() already resolved, rather than re-reading the
    allowlist per render. Absent on the open pages, where there is no role -
    hence the default.
    """
    return {"role": getattr(g, "role", None)}


@app.before_request
def guard():
    if request.path.startswith("/static"):
        return None
    if request.path in OPEN_PATHS:
        return None

    role = g.role = current_role()
    if role is None:
        # API callers get JSON; a browser gets the login page.
        if request.path.startswith(("/api/", "/ws/")):
            # A WebSocket client cannot read a redirect to a login page.
            return jsonify(error="not authenticated"), 401
        return redirect(url_for("login"))

    # One policy, read top to bottom. Every mutating route here is a POST and
    # every read a GET, so the general rule is a single condition - but two
    # reads are not for everyone, and they are named rather than left to be
    # noticed later.
    if request.path == "/users" or request.path.startswith("/api/users/"):
        if role != "admin":
            return _forbid("Only an admin can manage access.")
    elif request.path == "/settings" and role == "viewer":
        return _forbid("Settings are not available with read-only access.")
    elif request.method == "POST" and role == "viewer":
        return _forbid("Your access is read-only.")
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
    return env


def write_env(updates):
    """Rewrite .env preserving comments, ordering and untouched keys.

    Regenerating the file from a dict would silently discard the commented-out
    alternatives already in there, which are notes to the reader.
    """
    try:
        lines = open(ENV_PATH).read().splitlines()
    except FileNotFoundError:
        lines = []

    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^(\s*)([A-Z_][A-Z0-9_]*)(\s*)=(.*)$", line)
        if m and m.group(2) in updates:
            key = m.group(2)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)

    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")

    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, ENV_PATH)


# --------------------------------------------------------------------------
# background: status cache + drain watcher
# --------------------------------------------------------------------------

_status = {"generated": "", "disk": {}, "runners": []}
# A condition, not a plain lock: fleet subscribers wait on it rather than
# asking every few seconds. The generation counter is what lets a
# subscriber tell "nothing published yet" from "published while I was
# sending".
_status_lock = threading.Condition()
_status_gen = 0

# Ten minutes of live history at the 5s collector interval. In memory rather
# than in SQLite: the samples table is deliberately per-job, and widening it to
# record continuously would grow the database for data the live view only needs
# while someone is looking at it.
SERIES_LEN = 120
_series = {}


def _record_series(status):
    """Append one point per runner. Called from the collector, which has
    already computed all three values for the grid - so this costs an append,
    not a docker call."""
    runners = status.get("runners", [])
    live = set()
    for r in runners:
        name = r.get("name")
        if not name:
            continue
        live.add(name)
        ring = _series.get(name)
        if ring is None:
            ring = _series[name] = collections.deque(maxlen=SERIES_LEN)
        ring.append({
            "t": status.get("generated", ""),
            "cpu": r.get("cpu_percent", 0),
            "mem": ops.parse_size(r.get("mem_used")),
            "cache": ops.parse_size(r.get("build_cache")),
        })

    # An empty runner list is far more often a transient `docker ps` hiccup or
    # "Recreate fleet" briefly emptying every runner at once, than every
    # runner genuinely vanishing in the same instant. Pruning here would wipe
    # up to ten minutes of history for all six over what is usually a blip -
    # only prune when the list is non-empty, so a runner missing from a
    # *live* list still loses its ring, but a wholesale empty poll changes
    # nothing.
    if not runners:
        return
    # A removed runner must not keep its ring, or the dict grows for the life
    # of the process across add/remove cycles.
    for gone in set(_series) - live:
        del _series[gone]


def _series_for(name):
    return list(_series.get(name, ()))


def _collector():
    global _status, _status_gen
    while True:
        try:
            s = ops.collect()
            with _status_lock:
                _status = s
                _record_series(s)
                _status_gen += 1
                _status_lock.notify_all()
            _record_history(s)
        except Exception as e:  # noqa: BLE001
            print(f"[collector] {e}")
        time.sleep(5)


def _record_history(status):
    """Turn each poll into history: job events from the log, plus a sample.

    Events come from the log rather than from watching state flip idle->busy:
    the log carries the runner's own timestamps, so start and end times are
    exact instead of rounded to whenever a poll happened to notice, and a
    dashboard restart does not lose the jobs that ran while it was down.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for r in status.get("runners", []):
        name = r["name"]
        if r["state"] == "stopped":
            continue

        try:
            for kind, when, job, result in history.parse_events(
                    ops.logs_since(name, 45)):
                if kind == "start":
                    history.open_run(name, r.get("registration"), job, when)
                else:
                    history.close_run(name, job, when, result)
        except Exception as e:  # noqa: BLE001
            print(f"[history:{name}] {e}")

        # A job killed mid-flight never writes the "completed" line the loop
        # above keys on, so nothing there will ever close its row. Gated on
        # an open run existing: that keeps the extra inspect off the 5s poll
        # for every idle runner and bounds it to one call per poll while a
        # job is genuinely in progress.
        try:
            if history.has_open_run(name):
                started = ops.started_at(name)
                if started:
                    history.close_interrupted(name, started)
        except Exception as e:  # noqa: BLE001
            print(f"[orphans:{name}] {e}")

        # Samples attach to whichever run is open, so an idle runner records
        # nothing and the graph covers exactly the job's duration.
        if r["state"] in ("busy", "draining"):
            try:
                history.add_sample(name, r.get("cpu_percent") or 0,
                                   ops.parse_size(r.get("mem_used")), now)
            except Exception as e:  # noqa: BLE001
                print(f"[sample:{name}] {e}")


def _backfill():
    """One deep pass over existing logs at startup.

    The steady-state collector only looks back 45s, so without this the
    history would start empty even though the runners' logs already hold
    every job they have run since their last restart. Re-runs are harmless:
    (runner, job, started_at) is UNIQUE.

    Resource samples cannot be recovered - those only exist if we were
    watching - so backfilled runs show timing and result but no graph.
    """
    try:
        for name in ops.list_runner_names():
            events = history.parse_events(ops.logs_since(name, 7 * 24 * 3600))
            for kind, when, job, result in events:
                if kind == "start":
                    history.open_run(name, None, job, when)
                else:
                    history.close_run(name, job, when, result)
            if events:
                print(f"[backfill] {name}: {len(events)} events")
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] {e}")


def _enricher():
    """Fill in repo / workflow / branch / commit from the GitHub API.

    Separate from the collector: an API sweep can take seconds and must not
    delay telemetry. Best-effort throughout - an unmatched run keeps its
    log-only data rather than being dropped.
    """
    while True:
        try:
            env = read_env()
            token, org = env.get("GH_TOKEN"), env.get("GITHUB_ORG")
            if token and org:
                gh = github_api.GitHub(token, org)
                for run in history.pending_enrichment(limit=10):
                    found = gh.find_job(run.get("registration"),
                                        run.get("job_name"),
                                        run.get("started_at"),
                                        run.get("ended_at"))
                    if found:
                        history.apply_enrichment(run["id"], found)
                    else:
                        history.mark_unmatched(run["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[enricher] {e}")
        time.sleep(90)


def _drain_watcher():
    """Stop draining runners once their current job finishes.

    Runs here rather than in the request that started the drain: a job can take
    an hour, and an HTTP request cannot wait that long.
    """
    while True:
        try:
            for name in list(ops.load_state().get("draining", [])):
                if name not in ops.list_runner_names():
                    ops.set_draining(name, False)
                    continue
                if ops.is_idle(name):
                    print(f"[drain] {name} idle - stopping")
                    ops.stop(name)
                    ops.set_draining(name, False)
        except Exception as e:  # noqa: BLE001
            print(f"[drain] {e}")
        time.sleep(10)


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
    return render_template("index.html")


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
    env = read_env()
    return render_template(
        "settings.html",
        env=env,
        token_mask=mask(env.get("GH_TOKEN", "")),
        env_path=ENV_PATH,
    )


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# live push
# --------------------------------------------------------------------------

# How long a subscriber sleeps before waking on its own when the collector
# publishes nothing. It exists so revocation is still noticed on a fleet that
# is not changing - not to poll for data.
WS_IDLE_WAKE = 20


def diff_status(old, new):
    """What changed between two fleet snapshots, or None if nothing did.

    Field-level and per runner, so an idle fleet costs nothing on the wire and
    a busy one costs one state and one job string. Sending the whole snapshot
    every tick would be polling with a socket wrapped around it.
    """
    before = {r["name"]: r for r in (old or {}).get("runners", []) if r.get("name")}
    after = {r["name"]: r for r in (new or {}).get("runners", []) if r.get("name")}

    changed = {}
    for name, now in after.items():
        was = before.get(name)
        if was is None:
            # Never seen by this client: a diff of it would be unusable.
            changed[name] = now
            continue
        fields = {k: v for k, v in now.items() if was.get(k) != v}
        if fields:
            changed[name] = fields

    delta = {}
    if changed:
        delta["runners"] = changed
    gone = sorted(set(before) - set(after))
    if gone:
        delta["gone"] = gone
    for key in ("disk", "host"):
        if (old or {}).get(key) != (new or {}).get(key):
            delta[key] = (new or {}).get(key)

    # "generated" moves every tick and means nothing on its own. Including it
    # would make every tick look like a change and undo the whole point.
    return delta or None


def fleet_frames(authorised, wait_for_change, current):
    """Frames for one subscriber: a baseline, then only what changes.

    `authorised` is re-checked on every wake, including wakes where nothing
    changed. The access model is built on the role being re-read per request,
    and a socket is one request that lasts hours - without this, revoking
    someone would only take effect when they closed the tab.
    """
    if not authorised():
        return
    previous = current()
    yield {"type": "snapshot", "data": previous}

    while True:
        wait_for_change()
        if not authorised():
            return
        now = current()
        delta = diff_status(previous, now)
        previous = now
        if delta:
            yield {"type": "update", "data": delta}


@sock.route("/ws/fleet")
def ws_fleet(ws):
    sub = session.get("sub")
    seen = {"gen": -1}

    def authorised():
        return users.role_of(sub) is not None

    def current():
        with _status_lock:
            seen["gen"] = _status_gen
            # The collector replaces this dict rather than mutating it, so the
            # reference is safe to hand out unlocked.
            return _status

    def wait_for_change():
        with _status_lock:
            _status_lock.wait_for(lambda: _status_gen != seen["gen"],
                                  timeout=WS_IDLE_WAKE)

    try:
        for frame in fleet_frames(authorised, wait_for_change, current):
            ws.send(json.dumps(frame))
    except Exception:      # noqa: BLE001 - a closed browser tab is not an error
        pass


@app.route("/api/status")
def api_status():
    with _status_lock:
        return jsonify(_status)


def _detail_target(name):
    """Validate a runner name from the URL.

    Same rule as _target(), which reads from the JSON body. Checked before any
    docker call so a crafted name can never reach the command line.
    """
    if not re.fullmatch(r"github-runner-\d+", name or ""):
        return None, (jsonify(ok=False, error="bad runner name"), 400)
    if name not in ops.list_runner_names():
        return None, (jsonify(ok=False, error="no such runner"), 404)
    return name, None


def _detail(name, fn, *args, **kwargs):
    """Run a collector and turn its result into a response."""
    name, err = _detail_target(name)
    if err:
        return err
    result = fn(name, *args, **kwargs)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/runner/<name>")
def runner_page(name):
    if not re.fullmatch(r"github-runner-\d+", name or ""):
        return render_template("runner.html", name=name, missing=True), 404
    if name not in ops.list_runner_names():
        return render_template("runner.html", name=name, missing=True), 404
    return render_template("runner.html", name=name, missing=False)


@app.route("/api/runner/<name>/inspect")
def api_runner_inspect(name):
    name, err = _detail_target(name)
    if err:
        return err
    result = runner_detail.cached(
        f"inspect:{name}", 10, lambda: runner_detail.inspect(name))
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/api/runner/<name>/engine")
def api_runner_engine(name):
    return _detail(name, runner_detail.engine)


@app.route("/api/runner/<name>/logs")
def api_runner_logs(name):
    return _detail(name, runner_detail.logs,
                   since=request.args.get("since", ""))


@app.route("/api/runner/<name>/series")
def api_runner_series(name):
    name, err = _detail_target(name)
    if err:
        return err
    with _status_lock:
        return jsonify(ok=True, data=_series_for(name))


@app.route("/api/runner/<name>/github")
def api_runner_github(name):
    name, err = _detail_target(name)
    if err:
        return err
    env = read_env()
    token, org = env.get("GH_TOKEN"), env.get("GITHUB_ORG")
    if not token or not org:
        return jsonify(ok=False, error="GH_TOKEN or GITHUB_ORG not set"), 500
    try:
        # Keyed by org, not by runner: every runner on this page asks the same
        # question, and GitHub rate-limits.
        rows = runner_detail.cached(
            f"github:{org}", 60, lambda: github_api.GitHub(token, org).runners())
    except Exception as e:  # noqa: BLE001 - rendered in the panel
        return jsonify(ok=False, error=str(e)), 500
    if rows is None:
        return jsonify(ok=False, error="could not reach the GitHub API"), 500
    return jsonify(ok=True, data=rows)


@app.route("/api/runner/<name>/history")
def api_runner_history(name):
    name, err = _detail_target(name)
    if err:
        return err
    return jsonify(ok=True, data=history.list_runs(runner=name, limit=100))


@app.route("/api/runner/<name>/prune", methods=["POST"])
def api_runner_prune(name):
    name, err = _detail_target(name)
    if err:
        return err
    # Refuse rather than race: clearing the cache under a running job discards
    # exactly the layers it is about to reuse.
    if not ops.is_idle(name):
        return jsonify(ok=False, error=f"{name} is busy running a job"), 409
    result = ops.prune(name)
    return jsonify(ok=result["ok"], data=result), (200 if result["ok"] else 500)


@app.route("/api/prune-all", methods=["POST"])
def api_prune_all():
    """Sweep every idle runner. Busy ones are skipped, never interrupted.

    Each prune gets 120s rather than the 300s single-runner default: six
    runners at the full timeout would let one HTTP request run for close to
    an hour with no client-side timeout. 120s keeps the whole sweep inside
    something a browser tab will survive; a runner that needs longer than
    that reports an error here and can still be pruned individually.
    """
    results, skipped = [], []
    for name in ops.list_runner_names():
        if not ops.is_idle(name):
            skipped.append({"name": name, "reason": "busy running a job"})
            continue
        results.append(ops.prune(name, timeout=120))
    # A per-runner failure can leave freed_bytes as None (measurement was not
    # trustworthy) rather than 0 - treat that as "contributed nothing" to the
    # fleet total instead of crashing the whole sweep's response.
    freed = sum(r["freed_bytes"] or 0 for r in results)
    ok = all(r["ok"] for r in results)
    return jsonify(ok=ok, data={"results": results, "skipped": skipped,
                                "freed_bytes": freed})


def _target():
    name = (request.json or {}).get("name", "")
    if not re.fullmatch(r"github-runner-\d+", name or ""):
        return None, (jsonify(error="bad runner name"), 400)
    if name not in ops.list_runner_names():
        return None, (jsonify(error="no such runner"), 404)
    return name, None


@app.route("/api/runner/<action>", methods=["POST"])
def api_runner(action):
    name, err = _target()
    if err:
        return err

    if action == "start":
        ok, _, e = ops.start(name)
    elif action == "stop":
        ok, _, e = ops.stop(name)
        ops.set_draining(name, False)
    elif action == "restart":
        ok, _, e = ops.restart(name)
        ops.set_draining(name, False)
    elif action == "drain":
        # Marked, not stopped. The watcher thread stops it once the current
        # job finishes; if it is already idle that happens within ~10s.
        ops.set_draining(name, True)
        ok, e = True, ""
    elif action == "canceldrain":
        ops.set_draining(name, False)
        ok, e = True, ""
    elif action == "remove":
        ok, _, e = ops.remove(name)
    else:
        return jsonify(error="unknown action"), 400

    return jsonify(ok=ok, error=e or None), (200 if ok else 500)


@app.route("/api/runner/add", methods=["POST"])
def api_add():
    import providers
    idx = ops.next_free_index(providers.GITHUB)
    ok, name, err = ops.create(idx, read_env())
    return jsonify(ok=ok, name=name, error=None if ok else err), \
        (200 if ok else 500)


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    updates = {}
    for k, v in data.items():
        if k not in EDITABLE:
            continue
        # An empty secret field means "leave it alone", not "erase it" -
        # the form never receives the real value to send back.
        if k in SECRET_KEYS and not str(v).strip():
            continue
        updates[k] = str(v).strip()

    if not updates:
        return jsonify(ok=True, changed=[], note="nothing to change")

    try:
        write_env(updates)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500

    # Container env is fixed at creation, so these only take effect on
    # recreate. Say so instead of implying the change is live.
    return jsonify(ok=True, changed=sorted(updates),
                   note="Saved. Recreate runners to apply.")


@app.route("/api/recreate", methods=["POST"])
def api_recreate():
    """Remove and recreate every runner so new settings take effect.

    A cascade that destroyed six runners in production started here: `docker
    rm -f` reporting failure on a slow-but-successful removal was read as "the
    runner is still there", so the loop skipped its replacement and moved on
    to destroy the next one - repeatedly. The exit status of remove() is only
    a hint; list_runner_names() is the ground truth for whether the container
    is actually gone. If it is gone, replace it even if remove() said it
    failed. If it is NOT gone, or if create() fails, stop the sweep
    immediately instead of continuing on to the next runner - a runner that
    was removed and not replaced is lost capacity, and repeating that across
    the fleet is exactly the incident being fixed.
    """
    env = read_env()
    names = ops.list_runner_names()
    results = []
    for name in names:
        idx = ops._index_of(name)
        ok, _, err = ops.remove(name)
        if not ok and name in ops.list_runner_names():
            # remove() failed AND the container is still there: it is not
            # safe to assume anything about the rest of the fleet. Stop here
            # rather than destroying the next runner too.
            results.append({"name": name, "ok": False, "error": err})
            return jsonify(ok=False, results=results, aborted_at=name), 500

        ok2, new, err2 = ops.create(idx, env)
        results.append({"name": new, "ok": ok2,
                        "error": None if ok2 else err2})
        if not ok2:
            # A removed runner that failed to come back is lost capacity.
            # Stop rather than repeat that across the remaining runners.
            return jsonify(ok=False, results=results, aborted_at=new), 500

    return jsonify(ok=all(r["ok"] for r in results), results=results)


if __name__ == "__main__":
    history.init()
    threading.Thread(target=_backfill, daemon=True).start()
    threading.Thread(target=_collector, daemon=True).start()
    threading.Thread(target=_enricher, daemon=True).start()
    threading.Thread(target=_drain_watcher, daemon=True).start()
    app.permanent_session_lifetime = 60 * 60 * 24 * 14
    app.run(host="0.0.0.0", port=PORT, threaded=True)
