# Runner Detail View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clicking a runner card opens `/runner/<name>`, a page showing that runner's container internals, its own Docker engine, its live terminal output and its job history, plus the lifecycle actions that already exist in the API.

**Architecture:** A new read-only module `runner_detail.py` collects single-runner data by shelling out through `docker_ops._docker()`; seven new Flask routes expose it; one new template renders it as four tabs. Slow calls are fetched on demand, never polled. Secrets are masked server-side before they reach JSON.

**Tech Stack:** Python 3.12 (container) / 3.13 (host tests), Flask, SQLite, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-07-28-runner-detail-view-design.md`

## Global Constraints

These bind every task. Violating any of them is grounds for rejecting the task.

- **Two Docker engines exist on this machine and must never be mixed.** The default context is `desktop-linux` — that is **Docker Desktop, running the BeastStack production stack**. The runners are on a separate engine reached **only** as `wsl -d github-runners -- docker ...`. A bare `docker ps` in this repo talks to the wrong daemon.
- **Never run stack-wide or host-wide Docker commands.** No `docker system prune`, no `docker volume prune`, no `--remove-orphans`. Name the service.
- **Never modify anything under `D:\docker-compose\BeastStack\`.**
- **The GitHub token must never appear in an HTTP response.** Mask server-side, before serialization. CSS or template masking leaves it in the payload.
- **Never rebuild `innerHTML` on a poll.** Create elements once, patch values. This is the fix recorded at `dashboard/templates/index.html:159`; re-introducing it brings back visible flashing.
- **No call slower than the poll interval goes on a poll loop.** `docker exec` into a runner takes seconds — `_inner_df()` allows it 20.
- **Collectors never raise.** Return data or an error string, following the contract in `docker_ops._run()`. One wedged runner degrades one section, not the page.
- **Every API response is `{"ok": true, "data": ...}` or `{"ok": false, "error": "..."}`.**
- **New Python modules must be added to `dashboard/Dockerfile`.** It copies modules by name; a file not listed is absent from the image and the app dies on import at startup.
- `.env` is untracked and holds a live token. Never commit it, never print its contents.

## File Structure

| File | Responsibility |
|---|---|
| `dashboard/runner_detail.py` | **new** — read-only introspection of one runner; owns `mask()` and `SECRET_KEYS` |
| `dashboard/templates/runner.html` | **new** — the detail page and its four tabs |
| `dashboard/tests/conftest.py` | **new** — import path, temp data dir, authenticated test client |
| `dashboard/tests/test_runner_detail.py` | **new** — collector unit tests |
| `dashboard/tests/test_routes.py` | **new** — route contract tests |
| `dashboard/app.py` | modify — seven routes, series ring buffer, import `mask`/`SECRET_KEYS` |
| `dashboard/github_api.py` | modify — add `runners()` |
| `dashboard/templates/index.html` | modify — cards link to the page, action buttons stop propagation |
| `dashboard/Dockerfile` | modify — copy `runner_detail.py` |

## Running things

**Tests run on the host**, not in the container:

```bash
cd /d/docker-compose/GithubRunners
python -m pytest dashboard/tests -v
```

Host has Python 3.13.14, pytest 9.0.2, Flask 3.1.3. The image pins Flask 3.0.3. The gap is acceptable because every test here exercises pure logic or the Flask test client, neither of which touches 3.0/3.1 differences.

**What is not automatically tested.** This project has no JavaScript test
runner, and adding one is out of scope. Tasks 8-10 are therefore covered by
render tests through the Flask test client — asserting the required element ids
are present — plus explicit manual verification steps against the live
dashboard. The spec's "a repeated poll with identical data performs no DOM
writes" is checked by watching for flashing over a full minute, not by an
assertion. Do not skip those manual steps; they are the only coverage the
frontend has.

**Deploying the dashboard** (names the one service, touches nothing else):

```bash
wsl -d github-runners -u root -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d --build dashboard"
```

**Looking at the live dashboard:** http://localhost:9200/

---

### Task 1: Test harness, and move `mask()` into the new module

Creates the test infrastructure this project has never had, and performs the one refactor everything else depends on. `app.py` will import `runner_detail`, so `runner_detail` cannot import `app` — `mask()` and `SECRET_KEYS` move down.

**Files:**
- Create: `dashboard/tests/conftest.py`
- Create: `dashboard/tests/test_masking.py`
- Create: `dashboard/runner_detail.py`
- Modify: `dashboard/app.py` — delete `SECRET_KEYS` (line 44) and `mask()` (line 167), import both from `runner_detail`
- Modify: `dashboard/Dockerfile` — add `runner_detail.py` to the COPY line

**Interfaces:**
- Produces: `runner_detail.mask(value: str) -> str`, `runner_detail.SECRET_KEYS: set[str]`

- [ ] **Step 1: Write the conftest**

`dashboard/tests/conftest.py`:

```python
"""Test fixtures.

The dashboard is written to run as a container with /data mounted. On the host
there is no /data, and importing app.py writes a secret key at import time, so
DASH_DATA is redirected to a temp dir BEFORE any dashboard module is imported.
"""
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # make dashboard/ importable

_TMP = tempfile.mkdtemp(prefix="dash-test-")
os.environ.setdefault("DASH_DATA", _TMP)
os.environ.setdefault("ENV_PATH", os.path.join(_TMP, ".env"))


@pytest.fixture
def client():
    """A logged-in Flask test client.

    app.py guards every route with a before_request hook, so an unauthenticated
    client gets 401 or a redirect. Setting session['ok'] is what login() does.
    """
    import app as dash

    dash.app.config["TESTING"] = True
    if not dash.password_is_set():
        dash.set_password("test-password-123")
    with dash.app.test_client() as c:
        with c.session_transaction() as s:
            s["ok"] = True
        yield c


@pytest.fixture
def anon_client():
    """An unauthenticated client, for asserting the guard actually guards."""
    import app as dash

    dash.app.config["TESTING"] = True
    if not dash.password_is_set():
        dash.set_password("test-password-123")
    with dash.app.test_client() as c:
        yield c
```

- [ ] **Step 2: Write the failing test**

`dashboard/tests/test_masking.py`:

```python
import runner_detail


def test_mask_keeps_ends_hides_middle():
    out = runner_detail.mask("ghp_abcdefghijklmnopqrstuvwxyz1234")
    assert out == "ghp_" + "\u2022" * 8 + "1234"


def test_mask_short_value_is_fully_hidden():
    assert runner_detail.mask("abc") == "***"
    assert runner_detail.mask("12345678") == "*" * 8


def test_mask_empty_is_empty():
    assert runner_detail.mask("") == ""
    assert runner_detail.mask(None) == ""


def test_secret_keys_contains_the_token():
    assert "GH_TOKEN" in runner_detail.SECRET_KEYS
```

- [ ] **Step 3: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_masking.py -v
```

Expected: `ModuleNotFoundError: No module named 'runner_detail'`

- [ ] **Step 4: Create the module**

`dashboard/runner_detail.py`:

```python
"""Read-only introspection of a single runner.

Separate from docker_ops, which owns fleet lifecycle and fleet telemetry. This
module only looks at one runner and never changes anything, so a bug here can
misreport but cannot break a runner.

Owns mask() and SECRET_KEYS because app.py imports this module; importing them
back from app.py would be a circular import.
"""

SECRET_KEYS = {"GH_TOKEN"}


def mask(value):
    """Show enough to recognise a token, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "\u2022" * 8 + value[-4:]
```

- [ ] **Step 5: Run the test again**

```bash
python -m pytest dashboard/tests/test_masking.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Point app.py at the new home**

In `dashboard/app.py`, add to the import block after `import history` (line 29):

```python
import runner_detail
from runner_detail import SECRET_KEYS, mask
```

Then delete the old definitions: the `SECRET_KEYS = {"GH_TOKEN"}` line (44) and the whole `mask()` function (167-173). Leave `EDITABLE` alone — it stays in `app.py`.

- [ ] **Step 7: Add the module to the image**

In `dashboard/Dockerfile`, change:

```dockerfile
COPY app.py docker_ops.py history.py github_api.py ./
```

to:

```dockerfile
COPY app.py docker_ops.py history.py github_api.py runner_detail.py ./
```

Without this the image has no `runner_detail.py` and `app.py` dies on import.

- [ ] **Step 8: Verify app.py still imports and the settings page still masks**

```bash
python -m pytest dashboard/tests -v
python -c "import sys; sys.path.insert(0,'dashboard'); import os, tempfile; os.environ['DASH_DATA']=tempfile.mkdtemp(); import app; print('app imports OK; mask:', app.mask('ghp_abcdefghijklmnopqrstuvwxyz1234'))"
```

Expected: tests pass, and the mask line prints `ghp_••••••••1234`.

- [ ] **Step 9: Commit**

```bash
git add dashboard/runner_detail.py dashboard/tests/ dashboard/app.py dashboard/Dockerfile
git commit -m "test: add pytest harness; move mask() into runner_detail

app.py will import runner_detail, so mask() and SECRET_KEYS move down to
avoid a circular import. First tests in this project."
```

---

### Task 2: Container internals collector

**Files:**
- Modify: `dashboard/runner_detail.py`
- Create: `dashboard/tests/test_runner_detail.py`

**Interfaces:**
- Consumes: `runner_detail.mask`, `runner_detail.SECRET_KEYS` (Task 1)
- Produces: `runner_detail.inspect(name: str) -> dict` — `{"ok": True, "data": {...}}` or `{"ok": False, "error": str}`

- [ ] **Step 1: Write the failing test**

`dashboard/tests/test_runner_detail.py`:

```python
import json

import docker_ops
import runner_detail

FAKE_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz1234"

FAKE_INSPECT = json.dumps([{
    "Id": "abc123def456",
    "Created": "2026-07-28T10:00:00.000000000Z",
    "Name": "/github-runner-1",
    "RestartCount": 2,
    "Image": "sha256:deadbeefcafe",
    "State": {
        "Status": "running",
        "ExitCode": 0,
        "Pid": 1234,
        "StartedAt": "2026-07-28T10:00:01.000000000Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    },
    "Config": {
        "Image": "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest",
        "Env": [f"GH_TOKEN={FAKE_TOKEN}", "GITHUB_ORG=NoMercy-Entertainment"],
    },
    "HostConfig": {
        "NanoCpus": 4_000_000_000,
        "Memory": 17_179_869_184,
        "RestartPolicy": {"Name": "unless-stopped"},
        "Privileged": True,
        "NetworkMode": "bridge",
    },
    "Mounts": [{
        "Source": "/mnt/d/docker-compose/GithubRunners/scripts/start.sh",
        "Destination": "/root/start.sh",
        "Mode": "ro",
        "RW": False,
    }],
    "NetworkSettings": {"IPAddress": "172.17.0.2"},
}])


def _fake_docker(result):
    """Replace docker_ops._docker with something that returns a canned result."""
    def fake(*args, **kwargs):
        return result
    return fake


def test_inspect_parses_the_fields(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, FAKE_INSPECT, "")))
    r = runner_detail.inspect("github-runner-1")
    assert r["ok"] is True
    d = r["data"]
    assert d["image"] == "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest"
    assert d["digest"] == "sha256:deadbeefcafe"
    assert d["restart_count"] == 2
    assert d["status"] == "running"
    assert d["pid"] == 1234
    assert d["cpu_limit"] == 4.0
    assert d["mem_limit_bytes"] == 17_179_869_184
    assert d["restart_policy"] == "unless-stopped"
    assert d["privileged"] is True
    assert d["ip"] == "172.17.0.2"
    assert d["mounts"][0]["destination"] == "/root/start.sh"
    assert d["mounts"][0]["rw"] is False


def test_inspect_masks_the_token_out_of_the_payload(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, FAKE_INSPECT, "")))
    r = runner_detail.inspect("github-runner-1")
    # The whole serialized response, not just the env dict: this is the check
    # that matters, because anything reachable in JSON reaches the browser.
    assert FAKE_TOKEN not in json.dumps(r)
    assert r["data"]["env"]["GH_TOKEN"].startswith("ghp_")
    assert r["data"]["env"]["GITHUB_ORG"] == "NoMercy-Entertainment"


def test_inspect_reports_unlimited_as_none(monkeypatch):
    payload = json.loads(FAKE_INSPECT)
    payload[0]["HostConfig"]["NanoCpus"] = 0
    payload[0]["HostConfig"]["Memory"] = 0
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, json.dumps(payload), "")))
    d = runner_detail.inspect("github-runner-1")["data"]
    assert d["cpu_limit"] is None
    assert d["mem_limit_bytes"] is None


def test_inspect_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((False, "", "No such object")))
    r = runner_detail.inspect("github-runner-9")
    assert r["ok"] is False
    assert "No such object" in r["error"]


def test_inspect_survives_malformed_json(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, "not json at all", "")))
    r = runner_detail.inspect("github-runner-1")
    assert r["ok"] is False
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_runner_detail.py -v
```

Expected: `AttributeError: module 'runner_detail' has no attribute 'inspect'`

- [ ] **Step 3: Implement**

Append to `dashboard/runner_detail.py`:

```python
import json

import docker_ops


def _ok(data):
    return {"ok": True, "data": data}


def _err(message):
    return {"ok": False, "error": message or "unknown error"}


def _mask_env(pairs):
    """['K=V', ...] -> {'K': 'V'}, with SECRET_KEYS masked.

    Masking happens here, before the value is ever put in a dict that gets
    serialized. Masking later - in the template or in CSS - would leave the
    real token in the JSON payload.
    """
    env = {}
    for item in pairs or []:
        k, _, v = item.partition("=")
        env[k] = mask(v) if k in SECRET_KEYS else v
    return env


def inspect(name):
    """Container configuration and state, from `docker inspect`."""
    ok, out, err = docker_ops._docker("inspect", name, timeout=10)
    if not ok:
        return _err(err or out)
    try:
        c = json.loads(out)[0]
    except Exception as e:  # noqa: BLE001 - rendered, not raised
        return _err(f"could not parse inspect output: {e}")

    host = c.get("HostConfig") or {}
    state = c.get("State") or {}
    config = c.get("Config") or {}

    nano = host.get("NanoCpus") or 0
    mem = host.get("Memory") or 0

    return _ok({
        "image": config.get("Image", ""),
        "digest": c.get("Image", ""),
        "created": c.get("Created", ""),
        "restart_count": c.get("RestartCount", 0),
        "status": state.get("Status", ""),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "pid": state.get("Pid"),
        # Reported from HostConfig, not from .env: the point is to show what
        # the daemon actually enforced, which is the only way to notice that a
        # limit was set but never applied.
        "cpu_limit": (nano / 1e9) if nano else None,
        "mem_limit_bytes": mem or None,
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
        "privileged": bool(host.get("Privileged")),
        "network_mode": host.get("NetworkMode", ""),
        "ip": (c.get("NetworkSettings") or {}).get("IPAddress", ""),
        "mounts": [{
            "source": m.get("Source", ""),
            "destination": m.get("Destination", ""),
            "mode": m.get("Mode", ""),
            "rw": bool(m.get("RW")),
        } for m in c.get("Mounts") or []],
        "env": _mask_env(config.get("Env")),
    })
```

Move the `import json` and `import docker_ops` to the top of the file with the module docstring above them.

- [ ] **Step 4: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass, including the 4 masking tests from Task 1.

- [ ] **Step 5: Commit**

```bash
git add dashboard/runner_detail.py dashboard/tests/test_runner_detail.py
git commit -m "feat: container internals collector with server-side token masking"
```

---

### Task 3: Inner Docker engine collector

Each of the four calls degrades independently — a runner whose inner daemon is wedged still shows whatever else responded.

**Files:**
- Modify: `dashboard/runner_detail.py`
- Modify: `dashboard/tests/test_runner_detail.py`

**Interfaces:**
- Consumes: `_ok`, `_err`, `docker_ops._docker` (Task 2)
- Produces: `runner_detail.engine(name: str) -> dict` — always `{"ok": True, "data": {"df":…, "images":…, "containers":…, "volumes":…}}`, where each of the four sections is either its parsed value or `{"error": str}`

- [ ] **Step 1: Write the failing test**

Append to `dashboard/tests/test_runner_detail.py`:

```python
def test_engine_collects_all_four_sections(monkeypatch):
    responses = {
        "system": (True, '{"BuildCache":"12.3GB","Images":"4.1GB",'
                         '"Containers":"0B","Volumes":"0B"}', ""),
        "images": (True, '{"Repository":"alpine","Tag":"3.19","Size":"7.8MB"}\n'
                         '{"Repository":"node","Tag":"20","Size":"1.1GB"}', ""),
        "ps": (True, '{"Names":"buildx_buildkit","Status":"Up 2 hours"}', ""),
        "volume": (True, '{"Name":"cache-vol","Driver":"local"}', ""),
    }

    def fake(*args, **kwargs):
        joined = " ".join(args)
        for key, resp in responses.items():
            if f" {key}" in f" {joined}":
                return resp
        return (False, "", "unexpected call: " + joined)

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = runner_detail.engine("github-runner-1")
    assert r["ok"] is True
    d = r["data"]
    assert d["df"]["BuildCache"] == "12.3GB"
    assert len(d["images"]) == 2
    assert d["images"][1]["Repository"] == "node"
    assert d["containers"][0]["Names"] == "buildx_buildkit"
    assert d["volumes"][0]["Name"] == "cache-vol"


def test_engine_degrades_one_section_at_a_time(monkeypatch):
    def fake(*args, **kwargs):
        joined = " ".join(args)
        if " images" in f" {joined}":
            return (False, "", "daemon not responding")
        if " system" in f" {joined}":
            return (True, '{"BuildCache":"1GB"}', "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"]["error"] == "daemon not responding"
    assert d["df"]["BuildCache"] == "1GB"      # unaffected


def test_engine_handles_empty_output(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, "", "")))
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"] == []
    assert d["containers"] == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_runner_detail.py -k engine -v
```

Expected: `AttributeError: module 'runner_detail' has no attribute 'engine'`

- [ ] **Step 3: Implement**

Append to `dashboard/runner_detail.py`:

```python
def _exec_json_lines(name, args, timeout):
    """Run a docker command inside the runner that emits one JSON object per line."""
    ok, out, err = docker_ops._docker(
        "exec", name, "docker", *args, timeout=timeout)
    if not ok:
        return {"error": err or out or "command failed"}
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except ValueError:
            continue          # one malformed line must not lose the rest
    return items


def engine(name):
    """What this runner's own Docker daemon is holding.

    Four independent calls. Each is slow enough that this must never be put on
    a poll loop - _inner_df() allows the df call alone 20 seconds.
    """
    ok, out, err = docker_ops._docker(
        "exec", name, "docker", "system", "df", "--format", "json", timeout=20)
    if ok and out:
        try:
            df = json.loads(out.splitlines()[0])
        except ValueError:
            df = {"error": "could not parse df output"}
    else:
        df = {"error": err or "command failed"}

    return _ok({
        "df": df,
        "images": _exec_json_lines(
            name, ["images", "--format", "{{json .}}"], 20),
        "containers": _exec_json_lines(
            name, ["ps", "-a", "--format", "{{json .}}"], 15),
        "volumes": _exec_json_lines(
            name, ["volume", "ls", "--format", "{{json .}}"], 15),
    })
```

- [ ] **Step 4: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/runner_detail.py dashboard/tests/test_runner_detail.py
git commit -m "feat: inner Docker engine collector with per-section degradation"
```

---

### Task 4: Log tailer with a timestamp cursor

**Files:**
- Modify: `dashboard/runner_detail.py`
- Modify: `dashboard/tests/test_runner_detail.py`

**Interfaces:**
- Consumes: `_ok`, `_err`, `docker_ops._docker`
- Produces: `runner_detail.logs(name: str, since: str = "") -> dict` — `{"ok": True, "data": {"lines": [{"t": str, "text": str}], "cursor": str}}`

- [ ] **Step 1: Write the failing test**

Append to `dashboard/tests/test_runner_detail.py`:

```python
LOG_BATCH_1 = (
    "2026-07-28T10:00:01.100000000Z Runner listening for jobs\n"
    "2026-07-28T10:00:02.200000000Z Running job: build\n"
)
LOG_BATCH_2 = (
    "2026-07-28T10:00:02.200000000Z Running job: build\n"   # docker --since is
    "2026-07-28T10:00:03.300000000Z Step 1 of 4\n"          # inclusive: overlap
)


def test_logs_parses_timestamp_and_text(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert len(d["lines"]) == 2
    assert d["lines"][0]["t"] == "2026-07-28T10:00:01.100000000Z"
    assert d["lines"][0]["text"] == "Runner listening for jobs"
    assert d["cursor"] == "2026-07-28T10:00:02.200000000Z"


def test_logs_second_call_skips_the_overlap(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    first = runner_detail.logs("github-runner-1")["data"]

    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_2, "")))
    second = runner_detail.logs("github-runner-1", since=first["cursor"])["data"]

    texts = [ln["text"] for ln in second["lines"]]
    assert texts == ["Step 1 of 4"]           # no duplicate, nothing skipped
    assert second["cursor"] == "2026-07-28T10:00:03.300000000Z"


def test_logs_keeps_cursor_when_nothing_new(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, "", "")))
    d = runner_detail.logs("github-runner-1", since="2026-07-28T10:00:05Z")["data"]
    assert d["lines"] == []
    assert d["cursor"] == "2026-07-28T10:00:05Z"


def test_logs_tolerates_lines_without_a_timestamp(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, "no timestamp here\n", "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert d["lines"][0]["text"] == "no timestamp here"
    assert d["lines"][0]["t"] == ""


def test_logs_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((False, "", "No such container")))
    r = runner_detail.logs("github-runner-9")
    assert r["ok"] is False
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_runner_detail.py -k logs -v
```

Expected: `AttributeError: module 'runner_detail' has no attribute 'logs'`

- [ ] **Step 3: Implement**

Append to `dashboard/runner_detail.py`:

```python
DEFAULT_LOG_WINDOW = "5m"


def logs(name, since=""):
    """Log lines newer than `since`, with the cursor to use for the next call.

    Bounded by timestamp rather than `--tail N`: a verbose build can emit
    thousands of lines between two polls, and a fixed tail would silently drop
    everything above it.

    `docker logs --since` is INCLUSIVE, so the line at the cursor comes back
    every time. Lines are filtered with a strict `>` and the caller sees each
    line exactly once.
    """
    window = since or DEFAULT_LOG_WINDOW
    ok, out, err = docker_ops._docker(
        "logs", "--timestamps", "--since", window, name, timeout=15)
    if not ok:
        return _err(err or out)

    lines = []
    cursor = since
    for raw in out.splitlines():
        stamp, sep, text = raw.partition(" ")
        if not sep or "T" not in stamp:
            # A line docker did not stamp - keep it, it is still output.
            lines.append({"t": "", "text": raw})
            continue
        if since and stamp <= since:
            continue                       # the inclusive-boundary overlap
        lines.append({"t": stamp, "text": text})
        if stamp > cursor:
            cursor = stamp
    return _ok({"lines": lines, "cursor": cursor})
```

RFC3339 nanosecond timestamps from `docker logs --timestamps` are fixed-width and zero-padded, so lexical `>` is chronological `>`. No date parsing needed.

- [ ] **Step 4: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/runner_detail.py dashboard/tests/test_runner_detail.py
git commit -m "feat: log tailer with an inclusive-boundary-safe timestamp cursor"
```

---

### Task 5: Live series ring buffer

**Files:**
- Modify: `dashboard/app.py`
- Create: `dashboard/tests/test_series.py`

**Interfaces:**
- Consumes: `ops.parse_size` (already in `docker_ops`)
- Produces: `app._record_series(status: dict) -> None`, `app._series_for(name: str) -> list[dict]`, module global `app._series: dict[str, collections.deque]`

- [ ] **Step 1: Write the failing test**

`dashboard/tests/test_series.py`:

```python
import app as dash


def _status(names, cpu=10.0, mem="1GiB", cache="2GB"):
    return {
        "generated": "2026-07-28T10:00:00Z",
        "runners": [{"name": n, "cpu_percent": cpu, "mem_used": mem,
                     "build_cache": cache} for n in names],
    }


def test_series_records_one_entry_per_tick():
    dash._series.clear()
    dash._record_series(_status(["github-runner-1"]))
    dash._record_series(_status(["github-runner-1"]))
    out = dash._series_for("github-runner-1")
    assert len(out) == 2
    assert out[0]["cpu"] == 10.0
    assert out[0]["mem"] > 0          # parsed to bytes
    assert out[0]["cache"] > 0


def test_series_is_bounded_at_120():
    dash._series.clear()
    for _ in range(200):
        dash._record_series(_status(["github-runner-1"]))
    assert len(dash._series_for("github-runner-1")) == 120


def test_series_drops_runners_that_disappear():
    dash._series.clear()
    dash._record_series(_status(["github-runner-1", "github-runner-2"]))
    assert dash._series_for("github-runner-2")
    dash._record_series(_status(["github-runner-1"]))
    assert dash._series_for("github-runner-2") == []
    assert "github-runner-2" not in dash._series


def test_series_for_unknown_runner_is_empty():
    dash._series.clear()
    assert dash._series_for("github-runner-99") == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_series.py -v
```

Expected: `AttributeError: module 'app' has no attribute '_series'`

- [ ] **Step 3: Implement**

In `dashboard/app.py`, add `import collections` to the import block, then add below `_status_lock = threading.Lock()` (line 181):

```python
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
    live = set()
    for r in status.get("runners", []):
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
    # A removed runner must not keep its ring, or the dict grows for the life
    # of the process across add/remove cycles.
    for gone in set(_series) - live:
        del _series[gone]


def _series_for(name):
    return list(_series.get(name, ()))
```

Then in `_collector()`, add the call inside the existing lock block:

```python
            s = ops.collect()
            with _status_lock:
                _status = s
                _record_series(s)
            _record_history(s)
```

- [ ] **Step 4: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/app.py dashboard/tests/test_series.py
git commit -m "feat: bounded in-memory CPU/mem/cache series per runner"
```

---

### Task 6: GitHub runner identity

**Files:**
- Modify: `dashboard/github_api.py`
- Create: `dashboard/tests/test_github_api.py`

**Interfaces:**
- Produces: `github_api.GitHub.runners() -> list[dict]` — each `{"id", "name", "status", "busy", "labels": [str], "runner_group": str}`

- [ ] **Step 1: Write the failing test**

`dashboard/tests/test_github_api.py`:

```python
import github_api

PAGE = {
    "total_count": 2,
    "runners": [
        {"id": 41, "name": "nomercy-kvzz9", "status": "online", "busy": False,
         "runner_group_name": "Default",
         "labels": [{"name": "self-hosted"}, {"name": "Linux"}]},
        {"id": 42, "name": "nomercy-ab12", "status": "offline", "busy": True,
         "runner_group_name": "lent",
         "labels": [{"name": "X64"}]},
    ],
}


def test_runners_flattens_labels_and_group(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", lambda path, params=None: PAGE)
    out = gh.runners()
    assert len(out) == 2
    assert out[0]["id"] == 41
    assert out[0]["name"] == "nomercy-kvzz9"
    assert out[0]["status"] == "online"
    assert out[0]["busy"] is False
    assert out[0]["labels"] == ["self-hosted", "Linux"]
    assert out[0]["runner_group"] == "Default"
    assert out[1]["busy"] is True


def test_runners_returns_empty_on_api_failure(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", lambda path, params=None: None)
    assert gh.runners() == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_github_api.py -v
```

Expected: `AttributeError: 'GitHub' object has no attribute 'runners'`

- [ ] **Step 3: Implement**

Add to the `GitHub` class in `dashboard/github_api.py`:

```python
    def runners(self):
        """Self-hosted runners as GitHub sees them.

        Worth showing beside the local view: a container that is Up while
        GitHub reports it offline is the visible shape of a registration that
        broke silently.
        """
        data = self._get(f"/orgs/{self.org}/actions/runners",
                         params={"per_page": 100})
        if not data:
            return []
        out = []
        for r in data.get("runners", []):
            out.append({
                "id": r.get("id"),
                "name": r.get("name", ""),
                "status": r.get("status", ""),
                "busy": bool(r.get("busy")),
                "labels": [l.get("name", "") for l in r.get("labels", [])],
                "runner_group": r.get("runner_group_name", ""),
            })
        return out
```

`_get` is already defined as `def _get(self, path, params=None)` at `github_api.py:33`, so this signature matches. `_get` returns `None` on failure, which is why the empty-list guard exists.

- [ ] **Step 4: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/github_api.py dashboard/tests/test_github_api.py
git commit -m "feat: list org runners as GitHub sees them"
```

---

### Task 7: The seven routes

**Files:**
- Modify: `dashboard/app.py`
- Create: `dashboard/tests/test_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 2-6
- Produces: the routes listed below; `app._detail_target(name) -> (name|None, response|None)`

- [ ] **Step 1: Write the failing test**

`dashboard/tests/test_routes.py`:

```python
import pytest

import docker_ops
import runner_detail


@pytest.fixture(autouse=True)
def _clear_cache():
    """Routes memoise. Without this, one test's monkeypatched collector result
    is served to the next test and the failure is baffling."""
    runner_detail._cache.clear()
    yield


ENDPOINTS = ["inspect", "engine", "logs", "series", "github", "history"]


def test_all_detail_endpoints_require_auth(anon_client):
    for ep in ENDPOINTS:
        r = anon_client.get(f"/api/runner/github-runner-1/{ep}")
        assert r.status_code == 401, ep


def test_page_requires_auth(anon_client):
    r = anon_client.get("/runner/github-runner-1")
    assert r.status_code in (301, 302)
    assert "/login" in r.headers["Location"]


def test_bad_name_is_rejected_before_any_docker_call(client, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("docker must not be called for an invalid name")
    monkeypatch.setattr(docker_ops, "_docker", explode)
    for bad in ["../etc", "github-runner-", "github-runner-1x", "nope"]:
        r = client.get(f"/api/runner/{bad}/inspect")
        assert r.status_code == 400, bad
        assert r.get_json()["ok"] is False


def test_unknown_runner_is_404(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    r = client.get("/api/runner/github-runner-7/inspect")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_inspect_route_returns_the_collector_payload(client, monkeypatch):
    import runner_detail
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(runner_detail, "inspect",
                        lambda n: {"ok": True, "data": {"image": "x"}})
    r = client.get("/api/runner/github-runner-1/inspect")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "data": {"image": "x"}}


def test_collector_failure_is_500_with_the_error(client, monkeypatch):
    import runner_detail
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(runner_detail, "inspect",
                        lambda n: {"ok": False, "error": "boom"})
    r = client.get("/api/runner/github-runner-1/inspect")
    assert r.status_code == 500
    assert r.get_json()["error"] == "boom"


def test_logs_route_passes_the_cursor_through(client, monkeypatch):
    import runner_detail
    seen = {}
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])

    def fake_logs(name, since=""):
        seen["since"] = since
        return {"ok": True, "data": {"lines": [], "cursor": since}}

    monkeypatch.setattr(runner_detail, "logs", fake_logs)
    client.get("/api/runner/github-runner-1/logs?since=2026-07-28T10:00:00Z")
    assert seen["since"] == "2026-07-28T10:00:00Z"


def test_series_route_returns_a_list(client, monkeypatch):
    import app as dash
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    dash._series.clear()
    dash._record_series({"generated": "t", "runners": [
        {"name": "github-runner-1", "cpu_percent": 5,
         "mem_used": "1GiB", "build_cache": "1GB"}]})
    r = client.get("/api/runner/github-runner-1/series")
    assert r.status_code == 200
    assert len(r.get_json()["data"]) == 1


def test_page_renders_for_a_known_runner(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    r = client.get("/runner/github-runner-1")
    assert r.status_code == 200
    assert b"github-runner-1" in r.data
```

Also append the cache tests to `dashboard/tests/test_runner_detail.py`:

```python
def test_cached_returns_the_same_value_within_ttl():
    runner_detail._cache.clear()
    calls = []

    def fn():
        calls.append(1)
        return {"ok": True, "data": len(calls)}

    assert runner_detail.cached("k", 60, fn) == runner_detail.cached("k", 60, fn)
    assert len(calls) == 1


def test_cached_refetches_after_ttl(monkeypatch):
    runner_detail._cache.clear()
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(runner_detail.time, "monotonic", lambda: clock[0])

    def fn():
        calls.append(1)
        return len(calls)

    runner_detail.cached("k", 10, fn)
    clock[0] = 1005.0
    runner_detail.cached("k", 10, fn)
    assert len(calls) == 1          # still inside the window
    clock[0] = 1011.0
    runner_detail.cached("k", 10, fn)
    assert len(calls) == 2          # window expired


def test_cached_keys_do_not_collide():
    runner_detail._cache.clear()
    runner_detail.cached("a", 60, lambda: 1)
    runner_detail.cached("b", 60, lambda: 2)
    assert runner_detail.cached("a", 60, lambda: 99) == 1
    assert runner_detail.cached("b", 60, lambda: 99) == 2
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_routes.py -v
```

Expected: 404s everywhere — the routes do not exist.

- [ ] **Step 3: Implement the cache, then the routes**

First, add `import time` to `dashboard/runner_detail.py` and append:

```python
_cache = {}


def cached(key, ttl, fn):
    """Memoise a collector result for `ttl` seconds.

    Stops several browser tabs, or a reopened page, from each triggering the
    same slow docker exec or the same GitHub request. Deliberately not an HTTP
    cache header: the frontend sends `cache: no-store` with a cache-buster, as
    index.html:271 does, so a header would simply be ignored.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value
```

Then add to `dashboard/app.py`, after `api_status()`:

```python
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
    return jsonify(ok=True, data=rows)


@app.route("/api/runner/<name>/history")
def api_runner_history(name):
    name, err = _detail_target(name)
    if err:
        return err
    return jsonify(ok=True, data=history.list_runs(runner=name, limit=100))
```

- [ ] **Step 4: Add a placeholder template so the page route can render**

`dashboard/templates/runner.html` — replaced entirely in Task 8, but the route test needs something now:

```html
{% extends "base.html" %}
{% block content %}
<h1>{{ name }}</h1>
{% if missing %}<p>This runner does not exist.</p>{% endif %}
{% endblock %}
```

Check `base.html` for the actual block name before writing this — if it is not `content`, use whatever `index.html` uses.

- [ ] **Step 5: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add dashboard/app.py dashboard/templates/runner.html dashboard/tests/test_routes.py
git commit -m "feat: seven runner detail endpoints with name validation"
```

---

### Task 8: The page — skeleton, overview tab, action footer

**Files:**
- Modify: `dashboard/templates/runner.html`
- Modify: `dashboard/tests/test_routes.py`

**Interfaces:**
- Consumes: `/api/status`, `/api/runner/<name>/inspect`, `/api/runner/<name>/series`, `POST /api/runner/<action>`
- Produces: element ids `rd-header`, `rd-tabs`, `tab-overview`, `tab-engine`, `tab-logs`, `tab-history`, `rd-actions`

- [ ] **Step 1: Write the failing render test**

Append to `dashboard/tests/test_routes.py`:

```python
def test_page_has_the_four_tabs_and_the_action_footer(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    body = client.get("/runner/github-runner-1").data.decode()
    for anchor in ["rd-header", "rd-tabs", "tab-overview", "tab-engine",
                   "tab-logs", "tab-history", "rd-actions"]:
        assert anchor in body, anchor


def test_missing_runner_page_says_so(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: [])
    r = client.get("/runner/github-runner-1")
    assert r.status_code == 404
    assert b"no longer exists" in r.data
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest dashboard/tests/test_routes.py -k "four_tabs or missing_runner" -v
```

Expected: FAIL — the placeholder template has none of those ids.

- [ ] **Step 3: Read the existing markup first**

Before writing, read `dashboard/templates/base.html` and `dashboard/templates/index.html` in full. Match the existing block name, class names, colour variables and card styling. This page must look like it belongs to the dashboard, not like a different application.

Pay particular attention to `index.html` lines 145-230: that is the patch-don't-rebuild pattern (`if (v.textContent !== valText) v.textContent = valText;`). Every value update on this page follows it.

- [ ] **Step 4: Write the page**

`dashboard/templates/runner.html`. Requirements, all of which the tests or the constraints depend on:

- extends `base.html`, uses the same block as `index.html`
- when `missing` is true, render only a message containing the words "no longer exists" and a link back to `/`
- `#rd-header` shows name, state badge, registration and uptime, populated from `/api/status` — so it paints before any detail call returns
- `#rd-tabs` holds four buttons switching `#tab-overview`, `#tab-engine`, `#tab-logs`, `#tab-history`; only the visible tab fetches
- `#tab-overview` renders three sparklines (CPU, memory, cache) from `/api/runner/<name>/series`, plus a definition list of every field `inspect` returns; format `mem_limit_bytes` as GB and show `cpu_limit`/`mem_limit_bytes` of `null` as "unlimited"
- `#rd-actions` renders the six buttons per the spec table, each POSTing `{"name": "<name>"}` to `/api/runner/<action>`, with REMOVE behind a confirm; on success, redirect to `/` for REMOVE and refresh state otherwise
- polling: `/api/status` every 5s (header + sparkline source), `/api/runner/<name>/series` every 5s, `/api/runner/<name>/inspect` every 10s but **only while the overview tab is visible**
- every update patches existing nodes; the only `innerHTML` writes happen when a tab's DOM is built for the first time
- a 404 from any poll switches the page to the "no longer exists" state

- [ ] **Step 5: Run the tests**

```bash
python -m pytest dashboard/tests -v
```

Expected: all pass.

- [ ] **Step 6: Verify it in the browser**

```bash
wsl -d github-runners -u root -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d --build dashboard"
```

Then open http://localhost:9200/runner/github-runner-1 and confirm: header populated, overview fields present, sparklines moving after ~30s, action buttons present, and **no flashing** over a full minute.

- [ ] **Step 7: Commit**

```bash
git add dashboard/templates/runner.html dashboard/tests/test_routes.py
git commit -m "feat: runner detail page with overview tab and action footer"
```

---

### Task 9: Engine and history tabs

**Files:**
- Modify: `dashboard/templates/runner.html`

**Interfaces:**
- Consumes: `/api/runner/<name>/engine`, `/api/runner/<name>/history`, `/api/runner/<name>/github`, `/api/history/run/<id>`

- [ ] **Step 1: Build the engine tab**

- fetches `/api/runner/<name>/engine` when the tab is first shown and on an explicit refresh button — **never on a timer**
- shows a spinner while in flight, because this call takes seconds
- four sections: totals from `df` (with build cache shown against the 40 GB cap from `start.sh`), images table sorted by size descending, inner containers, volumes
- a section whose value is `{"error": ...}` renders that error in place and leaves the other three alone

- [ ] **Step 2: Build the history tab**

- fetches `/api/runner/<name>/history` on first show
- table of this runner's runs: job name, result, started, duration
- clicking a row fetches `/api/history/run/<id>` and draws its CPU/memory graph from `samples_data`, which that endpoint already returns
- fetches `/api/runner/<name>/github` on first show and renders a panel with agent id, labels, runner group, GitHub status and busy flag, matched to this container by the `registration` value from `/api/status`
- if the GitHub panel errors, it says so and the local run table still renders

- [ ] **Step 3: Verify in the browser**

```bash
wsl -d github-runners -u root -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d --build dashboard"
```

Open http://localhost:9200/runner/github-runner-1 and check: engine tab loads within a few seconds and lists real images; the build cache figure matches the card on `/`; history lists real past runs; clicking one draws a graph; the GitHub panel shows an agent id and `online`.

Confirm the engine tab does **not** refetch on its own — watch it for a minute and see no repeated spinner.

- [ ] **Step 4: Commit**

```bash
git add dashboard/templates/runner.html
git commit -m "feat: engine and history tabs"
```

---

### Task 10: Logs tab

**Files:**
- Modify: `dashboard/templates/runner.html`

**Interfaces:**
- Consumes: `/api/runner/<name>/logs?since=<cursor>`

- [ ] **Step 1: Build the tab**

- on first show, fetch with no `since` (server defaults to a 5 minute window), then poll every 2s passing the cursor from the previous response
- **append** new lines to the pane; never rebuild it
- cap the buffer at 2000 lines, dropping oldest
- auto-scroll to the bottom only when already at the bottom; if the operator has scrolled up, hold position and show a "jump to latest" control
- a filter box hides non-matching lines with CSS, without refetching
- polling stops when the tab is not visible, and resumes from the held cursor

- [ ] **Step 2: Verify against a runner that is actually working**

```bash
wsl -d github-runners -u root -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d --build dashboard"
```

Open the logs tab and confirm:
- lines appear and keep appearing
- **no duplicated lines** at the poll boundary — this is what the cursor logic exists to prevent, so read carefully across a few polls
- scrolling up holds position; the jump control returns to the bottom
- the filter narrows without clearing the pane

- [ ] **Step 3: Commit**

```bash
git add dashboard/templates/runner.html
git commit -m "feat: live log tab with append-only rendering"
```

---

### Task 11: Make the grid cards open the page, and verify the whole thing live

**Files:**
- Modify: `dashboard/templates/index.html`

- [ ] **Step 1: Link the cards**

In the card-building code in `index.html` (around lines 159-230), make the card navigate to `/runner/<name>` on click. On every action button inside the card, call `event.stopPropagation()` so pressing STOP does not also navigate. Give the card a pointer cursor so it looks clickable.

- [ ] **Step 2: Verify the grid still behaves**

```bash
python -m pytest dashboard/tests -v
wsl -d github-runners -u root -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d --build dashboard"
```

At http://localhost:9200/ confirm: clicking a card body opens the detail page; clicking DRAIN/STOP/RESTART/REMOVE does **not** navigate; the grid still updates without flashing.

- [ ] **Step 3: Confirm the token is absent from every response**

```bash
TOKEN=$(grep -m1 '^GH_TOKEN=' .env | cut -d= -f2-)
for ep in inspect engine logs series github history; do
  echo -n "  $ep: "
  curl -s --cookie-jar /tmp/c --cookie /tmp/c "http://localhost:9200/api/runner/github-runner-1/$ep" \
    | grep -qF "$TOKEN" && echo "TOKEN LEAKED" || echo "clean"
done
```

This needs an authenticated session; if the endpoints return 401, log in first in the browser and export that cookie, or run the check from the browser console instead. Every line must read `clean`. Do not paste the token or the raw responses anywhere.

- [ ] **Step 4: Confirm the fleet is unharmed**

```bash
wsl -d github-runners -- docker ps --format '{{.Names}}\t{{.Status}}'
```

Expected: six runners plus the dashboard, all `Up`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/templates/index.html
git commit -m "feat: grid cards open the runner detail page"
```

---

## Definition of done

- [ ] `python -m pytest dashboard/tests -v` passes with no failures
- [ ] Clicking any card opens that runner's page; action buttons do not navigate
- [ ] All four tabs render real data from the live fleet
- [ ] The GitHub token appears in no API response (Task 11 Step 3)
- [ ] Nothing flashes on `/` or on `/runner/<name>` over a full minute
- [ ] The engine tab never refetches on a timer
- [ ] Six runners and the dashboard are `Up` afterwards
- [ ] `runner_detail.py` is in the Dockerfile COPY line
