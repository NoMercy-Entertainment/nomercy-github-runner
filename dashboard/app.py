"""NoMercy Runners - control dashboard.

Serves the status page, the settings page, and the control endpoints for the
runners on this engine.

Auth: a password you set on first visit, hashed with PBKDF2 and never stored
in plaintext. Sessions are Flask's signed cookies over a secret generated once
and persisted, so restarting the dashboard does not log you out.

This runs over plain HTTP on a LAN by request. Passwords and the GitHub token
therefore cross the network unencrypted; the UI says so rather than letting it
be forgotten.
"""

import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

import docker_ops as ops
import github_api
import history
import runner_detail
from runner_detail import SECRET_KEYS, mask

DATA = os.environ.get("DASH_DATA", "/data")
PORT = int(os.environ.get("DASH_PORT", "9200"))
ENV_PATH = os.environ.get("ENV_PATH", "/repo/.env")
AUTH_PATH = os.path.join(DATA, "auth.json")
SECRET_PATH = os.path.join(DATA, "secret.key")

# Only these may be written from the UI. An allowlist rather than "write
# whatever was posted": the .env is read by the container runtime, and a typo'd
# or injected key there is a foothold, not a cosmetic bug.
EDITABLE = {
    "GH_TOKEN", "GITHUB_ORG", "RUNNER_LABELS",
    "RUNNER_GROUP", "RUNNER_CPU_LIMIT", "RUNNER_MEM_LIMIT",
}

app = Flask(__name__)


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


def _auth():
    try:
        return json.load(open(AUTH_PATH))
    except Exception:
        return None


def password_is_set():
    return _auth() is not None


def set_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    with open(AUTH_PATH, "w") as fh:
        json.dump({"salt": salt.hex(), "hash": dk.hex()}, fh)
    os.chmod(AUTH_PATH, 0o600)


def check_password(pw):
    a = _auth()
    if not a:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                             bytes.fromhex(a["salt"]), 200_000)
    # compare_digest, not ==: a plain comparison leaks the prefix length
    # through timing.
    return hmac.compare_digest(dk.hex(), a["hash"])


def logged_in():
    return session.get("ok") is True


@app.before_request
def guard():
    open_paths = {"/login", "/setup", "/static"}
    if request.path.startswith("/static"):
        return None
    if request.path in open_paths:
        return None
    if not password_is_set():
        return redirect(url_for("setup"))
    if not logged_in():
        # API callers get JSON; a browser gets the login page.
        if request.path.startswith("/api/"):
            return jsonify(error="not authenticated"), 401
        return redirect(url_for("login"))
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
_status_lock = threading.Lock()

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
    global _status
    while True:
        try:
            s = ops.collect()
            with _status_lock:
                _status = s
                _record_series(s)
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

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if password_is_set():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(pw) < 8:
            error = "Use at least 8 characters."
        elif pw != confirm:
            error = "Those do not match."
        else:
            set_password(pw)
            session["ok"] = True
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not password_is_set():
        return redirect(url_for("setup"))
    error = None
    if request.method == "POST":
        if check_password(request.form.get("password", "")):
            session["ok"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Wrong password."
        time.sleep(1)  # blunt the brute-force rate
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    idx = ops.next_free_index()
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
