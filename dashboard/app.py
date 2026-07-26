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
SECRET_KEYS = {"GH_TOKEN"}

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


def mask(value):
    """Show enough to recognise a token, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "•" * 8 + value[-4:]


# --------------------------------------------------------------------------
# background: status cache + drain watcher
# --------------------------------------------------------------------------

_status = {"generated": "", "disk": {}, "runners": []}
_status_lock = threading.Lock()


def _collector():
    global _status
    while True:
        try:
            s = ops.collect()
            with _status_lock:
                _status = s
        except Exception as e:  # noqa: BLE001
            print(f"[collector] {e}")
        time.sleep(5)


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
    """Remove and recreate every runner so new settings take effect."""
    env = read_env()
    names = ops.list_runner_names()
    results = []
    for name in names:
        idx = ops._index_of(name)
        ok, _, err = ops.remove(name)
        if not ok:
            results.append({"name": name, "ok": False, "error": err})
            continue
        ok2, new, err2 = ops.create(idx, env)
        results.append({"name": new, "ok": ok2,
                        "error": None if ok2 else err2})
    return jsonify(ok=all(r["ok"] for r in results), results=results)


if __name__ == "__main__":
    threading.Thread(target=_collector, daemon=True).start()
    threading.Thread(target=_drain_watcher, daemon=True).start()
    app.permanent_session_lifetime = 60 * 60 * 24 * 14
    app.run(host="0.0.0.0", port=PORT, threaded=True)
