# Forgejo Runners Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show and control Forgejo runners in the existing runner dashboard with the same capabilities the GitHub runners already have.

**Architecture:** The Forgejo runners move onto the `github-runners` Docker engine so the dashboard reaches them over the socket it already mounts. A thin `providers.py` seam holds the handful of things that differ between the two forges; `docker_ops` stays one code path. Busy/idle and run results come from Forgejo's API, which answers both authoritatively, rather than from log scraping.

**Tech Stack:** Python 3.12, Flask 3.0.3, flask-sock, sqlite3, pytest, the `docker` CLI over a mounted socket, Forgejo 16.0.2 REST API v1.

**Spec:** `docs/superpowers/specs/2026-08-25-forgejo-runners-design.md`

## Global Constraints

- **The 16 existing test files in `dashboard/tests/` must not be modified and must stay green.** If a change would require editing one, stop and report it as a behaviour change rather than editing the test.
- Existing public signatures called by those tests must keep working with their current argument counts: `docker_ops._job_state(name)`, `docker_ops.is_idle(name)`, `docker_ops._inner_df(name)`, `docker_ops.prune(name)`, `docker_ops.started_at(name)`, `docker_ops.host_info()`, `history.open_run(runner, registration, job_name, started_at)`, `history.close_run(runner, job_name, ended_at, result)`, `history.close_interrupted(runner, container_started_at)`, `history.list_runs(...)`, `history.init()`. New parameters are added with defaults, never positionally in front.
- The eight `github-runner-N` containers running today must keep appearing in the dashboard without being recreated. They carry no `nomercy.provider` label.
- Container names stay a strict allowlist: `(?:github|forgejo)-runner-\d+`. The name reaches a command line.
- Forgejo instance URL: `https://forgejo.phillippepelzer.me`. Never `http://forgejo:3000` (a compose network that does not exist on the distro) and never the host gateway `172.28.192.1` (changes across reboots).
- Forgejo API auth header: `Authorization: token <FORGEJO_API_TOKEN>`.
- `GET /api/v1/admin/actions/runners` returns a **bare JSON array**, not an object with a `runners` key.
- `GET /api/v1/repos/{owner}/{repo}/actions/tasks` returns `{"total_count": N, "workflow_runs": [...]}` — the tasks are under `workflow_runs` despite the name.
- Every new `dashboard/*.py` module must be added to the `COPY` line in `dashboard/Dockerfile`, or `test_image_contents.py` fails.
- Run tests with: `cd dashboard && python -m pytest tests/ -q`

---

### Task 1: The provider seam

**Files:**
- Create: `dashboard/providers.py`
- Modify: `dashboard/Dockerfile` (the `COPY` line)
- Test: `dashboard/tests/test_providers.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `providers.Provider` with attributes `key`, `prefix`, `image`, `registration_path`, `registration_key`, and methods `name_for(index) -> str`, `container_env(env) -> (dict, str|None)`, `forge_client(env) -> object|None`.
  - `providers.GITHUB`, `providers.FORGEJO`, `providers.ALL`.
  - `providers.by_key(key) -> Provider|None`
  - `providers.for_name(name) -> Provider|None`
  - `providers.from_label(label_value, name) -> Provider|None`
  - `providers.valid_name(name) -> bool`
  - `providers.LABEL_PROVIDER = "nomercy.provider"`

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_providers.py
"""The provider seam, and the one property the live fleet depends on.

The eight github-runner-N containers running today were created by compose
before the nomercy.provider label existed. If provider resolution insisted on
the label, the entire existing fleet would vanish from the dashboard until it
was rebuilt - so the prefix fallback is not a nicety, it is what keeps the
current deployment visible.
"""
import providers


def test_the_two_providers_are_distinguishable():
    assert providers.GITHUB.key == "github"
    assert providers.FORGEJO.key == "forgejo"
    assert providers.GITHUB.prefix == "github-runner-"
    assert providers.FORGEJO.prefix == "forgejo-runner-"


def test_a_label_names_the_provider():
    assert providers.from_label("forgejo", "anything") is providers.FORGEJO


def test_an_unlabelled_container_falls_back_to_its_name():
    """The live fleet has no label. It must still resolve."""
    assert providers.from_label("", "github-runner-3") is providers.GITHUB
    assert providers.from_label(None, "forgejo-runner-1") is providers.FORGEJO


def test_a_label_beats_a_misleading_name():
    assert providers.from_label("forgejo", "github-runner-1") is providers.FORGEJO


def test_an_unknown_container_resolves_to_nothing():
    assert providers.from_label("", "immich-server") is None
    assert providers.by_key("gitlab") is None


def test_valid_name_is_an_allowlist_not_a_filter():
    assert providers.valid_name("github-runner-1")
    assert providers.valid_name("forgejo-runner-12")
    for bad in ("github-runner-", "github-runner-1;rm -rf /", "../etc",
                "forgejo-runner-1 ", "gitlab-runner-1", ""):
        assert not providers.valid_name(bad), bad


def test_name_for_builds_the_container_name():
    assert providers.FORGEJO.name_for(4) == "forgejo-runner-4"


def test_github_container_env_needs_no_network(monkeypatch):
    env, err = providers.GITHUB.container_env({"GH_TOKEN": "t"})
    assert err is None
    assert env["GH_TOKEN"] == "t"
    assert env["GITHUB_ORG"] == "NoMercy-Entertainment"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_providers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'providers'`

- [ ] **Step 3: Write the implementation**

```python
# dashboard/providers.py
"""What differs between the two forges, in one place.

The dashboard controls runners for GitHub Actions and for Forgejo. Almost
everything about them is the same - containers on one engine, each with its own
nested Docker daemon - so this module holds only what genuinely differs, and
docker_ops stays a single code path.

Deliberately data and construction, never behaviour that touches Docker:
docker_ops imports this module, so anything here calling back into it would be
a circular import. The forge API clients are safe to build here because neither
of them imports docker_ops.
"""

import os
import re

LABEL_PROVIDER = "nomercy.provider"

_NAME_RE = re.compile(r"(?:github|forgejo)-runner-\d+")


class Provider:
    def __init__(self, key, prefix, image, registration_path,
                 registration_key):
        self.key = key
        self.prefix = prefix
        self.image = image
        self.registration_path = registration_path
        # The field inside the runner's registration file that holds the name
        # the forge knows it by. GitHub writes "agentName"; Forgejo "name".
        self.registration_key = registration_key

    def __repr__(self):
        return f"<Provider {self.key}>"

    def name_for(self, index):
        return f"{self.prefix}{index}"

    def container_env(self, env):
        """Environment for a new runner container, as (dict, error).

        Returns an error rather than raising because one of the two has to
        talk to the network to build it - Forgejo mints a fresh registration
        token per runner - and a failed create must render, not 500.
        """
        raise NotImplementedError

    def forge_client(self, env):
        """An API client, or None when the deployment is not configured."""
        raise NotImplementedError


class _GitHub(Provider):
    def container_env(self, env):
        return {
            "GH_TOKEN": env.get("GH_TOKEN", ""),
            "GITHUB_ORG": env.get("GITHUB_ORG", "NoMercy-Entertainment"),
            "RUNNER_LABELS": env.get("RUNNER_LABELS", "self-hosted,Linux,X64"),
            "RUNNER_GROUP": env.get("RUNNER_GROUP", ""),
        }, None

    def forge_client(self, env):
        import github_api
        token, org = env.get("GH_TOKEN"), env.get("GITHUB_ORG")
        if not (token and org):
            return None
        return github_api.GitHub(token, org)


class _Forgejo(Provider):
    def container_env(self, env):
        url = (env.get("FORGEJO_INSTANCE_URL") or "").strip()
        if not url:
            return {}, "FORGEJO_INSTANCE_URL is not set"
        client = self.forge_client(env)
        if client is None:
            return {}, "FORGEJO_API_TOKEN is not set"
        token = client.registration_token()
        if not token:
            return {}, ("could not mint a registration token - check "
                        "FORGEJO_API_TOKEN and that Forgejo is reachable")
        return {
            "FORGEJO_INSTANCE_URL": url,
            "FORGEJO_RUNNER_REGISTRATION_TOKEN": token,
            "FORGEJO_RUNNER_LABELS": env.get("FORGEJO_RUNNER_LABELS", ""),
        }, None

    def forge_client(self, env):
        import forgejo_api
        url = (env.get("FORGEJO_INSTANCE_URL") or "").strip()
        token = (env.get("FORGEJO_API_TOKEN") or "").strip()
        if not (url and token):
            return None
        return forgejo_api.Forgejo(url, token)


GITHUB = _GitHub(
    key="github",
    prefix="github-runner-",
    image=os.environ.get(
        "RUNNER_IMAGE",
        "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest"),
    registration_path="/root/actions-runner/.runner",
    registration_key="agentName",
)

FORGEJO = _Forgejo(
    key="forgejo",
    prefix="forgejo-runner-",
    image=os.environ.get(
        "FORGEJO_RUNNER_IMAGE",
        "ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest"),
    registration_path="/data/.runner",
    registration_key="name",
)

ALL = (GITHUB, FORGEJO)
_BY_KEY = {p.key: p for p in ALL}


def by_key(key):
    return _BY_KEY.get((key or "").strip().lower())


def for_name(name):
    for p in ALL:
        if (name or "").startswith(p.prefix):
            return p
    return None


def from_label(label_value, name):
    """The provider of a container. The label decides; the name is the
    fallback for containers created before the label existed - which is every
    runner currently deployed."""
    return by_key(label_value) or for_name(name)


def valid_name(name):
    return bool(_NAME_RE.fullmatch(name or ""))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && python -m pytest tests/test_providers.py -q`
Expected: PASS, 8 tests.

Note: `test_github_container_env_needs_no_network` passes without `forgejo_api` existing because `_Forgejo.container_env` imports it lazily and is not called here.

- [ ] **Step 5: Add the module to the image**

In `dashboard/Dockerfile`, change:

```
COPY app.py docker_ops.py github_api.py history.py oidc.py runner_detail.py users.py ./
```

to:

```
COPY app.py docker_ops.py forgejo_api.py github_api.py history.py oidc.py providers.py runner_detail.py users.py ./
```

`forgejo_api.py` is listed now even though Task 3 creates it — `test_image_contents.py` only checks that every existing module appears in the Dockerfile, never the reverse, so listing it early is safe and saves a second edit.

- [ ] **Step 6: Run the full suite**

Run: `cd dashboard && python -m pytest tests/ -q`
Expected: all pass. The 16 pre-existing files are untouched.

- [ ] **Step 7: Commit**

```bash
git add dashboard/providers.py dashboard/tests/test_providers.py dashboard/Dockerfile
git commit -m "feat: a provider seam for the two forges"
```

---

### Task 2: Listing runners of both kinds

**Files:**
- Modify: `dashboard/docker_ops.py` (`list_runner_names`, `next_free_index`, add `list_runners`)
- Test: `dashboard/tests/test_list_runners.py`

**Interfaces:**
- Consumes: `providers.from_label`, `providers.ALL`, `providers.GITHUB`, `providers.FORGEJO`.
- Produces:
  - `docker_ops.list_runners() -> [(name: str, provider: Provider)]`
  - `docker_ops.list_runner_names() -> [str]` (unchanged signature, now covers both kinds)
  - `docker_ops.next_free_index(provider) -> int`

The spec said `list_runner_names()` "becomes" `list_runners()`. It is kept as a one-line wrapper instead: three call sites in `app.py` only ever ask "does this name exist", and keeping it removes churn without changing behaviour.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_list_runners.py
"""Listing must cover both fleets and must not lose the unlabelled one.

`docker ps --format` exposes a single label through the `.Label` function, so
one call gets names and providers together. Containers that are neither fleet
must not be listed at all: the dashboard's action routes check membership of
this list before touching anything.
"""
import docker_ops
import providers

# Verbatim shape of `docker ps -a --format '{{.Names}}\t{{.Label "..."}}'`.
# The github rows have an empty label because the containers deployed today
# predate it; docker prints an empty field, not the literal "<no value>",
# for a label a container does not carry.
PS_OUTPUT = (
    "github-runner-1\t\n"
    "github-runner-2\t\n"
    "forgejo-runner-1\tforgejo\n"
    "runner-dashboard\t\n"
    "immich-server\t\n"
)


def _fake_ps(out):
    def call(*args, **kwargs):
        return (True, out, "")
    return call


def test_both_fleets_are_listed(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    got = docker_ops.list_runners()
    assert got == [
        ("github-runner-1", providers.GITHUB),
        ("github-runner-2", providers.GITHUB),
        ("forgejo-runner-1", providers.FORGEJO),
    ]


def test_non_runner_containers_are_excluded(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    names = docker_ops.list_runner_names()
    assert "runner-dashboard" not in names
    assert "immich-server" not in names


def test_a_failed_ps_lists_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (False, "", "daemon down"))
    assert docker_ops.list_runners() == []


def test_next_free_index_is_per_provider(monkeypatch):
    """A forgejo runner must not be pushed to index 3 because two github
    runners exist. The two fleets number independently."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    assert docker_ops.next_free_index(providers.GITHUB) == 3
    assert docker_ops.next_free_index(providers.FORGEJO) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_list_runners.py -q`
Expected: FAIL with `AttributeError: module 'docker_ops' has no attribute 'list_runners'`

- [ ] **Step 3: Write the implementation**

In `dashboard/docker_ops.py`, add `import providers` beside the existing imports, and replace `list_runner_names` and `next_free_index`:

```python
def list_runners():
    """[(name, provider)] for every runner container on this engine.

    One `docker ps` for both fleets: `--format` exposes a single label through
    the `.Label` function, so names and providers arrive together rather than
    costing an inspect per container.

    Selection is by name prefix, not by the nomercy.runner label. The runners
    deployed today were created by compose, which never set that label, and a
    listing that required it would show an empty fleet.
    """
    ok, out, _ = _docker(
        "ps", "-a", "--format",
        '{{.Names}}\t{{.Label "' + providers.LABEL_PROVIDER + '"}}')
    if not ok:
        return []
    found = []
    for line in out.splitlines():
        name, _, label = line.partition("\t")
        name, label = name.strip(), label.strip()
        p = providers.from_label(label, name)
        # from_label can answer on the label alone; require the name to match
        # too, so a stray label on an unrelated container cannot enrol it into
        # a fleet whose action routes would then act on it.
        if p and name.startswith(p.prefix):
            found.append((name, p))
    return sorted(found, key=lambda t: (t[1].key != "github", t[1].key,
                                        _index_of(t[0])))


def list_runner_names():
    """Names only. app.py's guards ask nothing more than "does this exist"."""
    return [n for n, _ in list_runners()]


def next_free_index(provider):
    """Lowest unused index within one fleet. The two number independently."""
    used = {_index_of(n) for n, p in list_runners() if p is provider}
    i = 1
    while i in used:
        i += 1
    return i
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && python -m pytest tests/test_list_runners.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Fix the one existing caller of next_free_index**

In `dashboard/app.py`, `api_add()` currently calls `ops.next_free_index()`. Change it to pass GitHub explicitly, so the app still runs; Task 10 makes it read the provider from the request body.

```python
@app.route("/api/runner/add", methods=["POST"])
def api_add():
    import providers
    idx = ops.next_free_index(providers.GITHUB)
    ok, name, err = ops.create(idx, read_env())
    return jsonify(ok=ok, name=name, error=None if ok else err), \
        (200 if ok else 500)
```

- [ ] **Step 6: Run the full suite**

Run: `cd dashboard && python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add dashboard/docker_ops.py dashboard/app.py dashboard/tests/test_list_runners.py
git commit -m "feat: list both runner fleets from one docker ps"
```

---

### Task 3: The Forgejo API client

**Files:**
- Create: `dashboard/forgejo_api.py`
- Test: `dashboard/tests/test_forgejo_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces `forgejo_api.Forgejo(base_url, token)` with:
  - `runner_statuses() -> {uuid: "offline"|"idle"|"active"} | None` (None means the call failed, distinct from "no runners")
  - `registration_token() -> str | None`
  - `delete_runner(runner_id) -> bool`
  - `find_task(repo, task_id, started_at) -> dict | None` returning the same keys `history.apply_enrichment` already accepts: `run_id`, `repo`, `workflow`, `branch`, `sha`, `actor`, `url`, `conclusion`.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_api.py
"""Shapes captured from Forgejo 16.0.2's own swagger, not invented.

Two of them are easy to get wrong by assuming they mirror GitHub:
  - /admin/actions/runners returns a BARE ARRAY, not {"runners": [...]}
  - /actions/tasks puts the tasks under "workflow_runs", despite the name
Getting either wrong yields an empty result rather than an error, which is
exactly the failure that survives review.
"""
import forgejo_api

RUNNERS = [
    {"id": 3, "uuid": "edfa80e4-9f11-4757-8626-a707af9be520",
     "name": "beaststack-runner", "status": "idle",
     "labels": ["ubuntu-latest:docker://data.forgejo.org/oci/node:lts"],
     "version": "12.0.0", "ephemeral": False},
    {"id": 4, "uuid": "aa11bb22-0000-4757-8626-000000000000",
     "name": "nomercy-forgejo-1", "status": "active",
     "labels": [], "version": "12.0.0", "ephemeral": False},
]

TASKS = {
    "total_count": 2,
    "workflow_runs": [
        {"id": 830, "name": "build", "status": "success",
         "head_branch": "main", "head_sha": "abcdef1234567890",
         "event": "push", "display_title": "Build the plugin",
         "url": "https://forgejo.example/FiLL/p/actions/runs/12",
         "run_number": 12,
         "run_started_at": "2026-08-25T14:38:55Z",
         "updated_at": "2026-08-25T14:44:02Z",
         "workflow_id": "build.yml"},
        {"id": 829, "name": "test", "status": "failure",
         "head_branch": "main", "head_sha": "0000000000000000",
         "event": "push", "display_title": "Test",
         "url": "https://forgejo.example/FiLL/p/actions/runs/11",
         "run_number": 11,
         "run_started_at": "2026-08-25T14:33:15Z",
         "updated_at": "2026-08-25T14:35:00Z",
         "workflow_id": "test.yml"},
    ],
}


def _client(routes):
    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get = lambda path, params=None: routes.get(path)
    return fj


def test_runner_statuses_reads_a_bare_array():
    fj = _client({"/api/v1/admin/actions/runners": RUNNERS})
    assert fj.runner_statuses() == {
        "edfa80e4-9f11-4757-8626-a707af9be520": "idle",
        "aa11bb22-0000-4757-8626-000000000000": "active",
    }


def test_a_failed_call_is_not_an_empty_fleet():
    """None and {} must stay distinguishable: {} would mark every runner
    unknown-but-answered, None says the API could not be reached."""
    fj = _client({})
    assert fj.runner_statuses() is None


def test_registration_token_is_unwrapped():
    fj = _client({"/api/v1/admin/actions/runners/registration-token":
                  {"token": "REG-123"}})
    assert fj.registration_token() == "REG-123"


def test_registration_token_survives_a_failure():
    assert _client({}).registration_token() is None


def test_find_task_matches_on_id_and_maps_to_the_runs_columns():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z")
    assert got == {
        "run_id": 830,
        "repo": "FiLL/p",
        "workflow": "build.yml",
        "branch": "main",
        "sha": "abcdef12",
        "actor": None,
        "url": "https://forgejo.example/FiLL/p/actions/runs/12",
        "conclusion": "success",
        "ended_at": "2026-08-25T14:44:02Z",
    }


def test_find_task_falls_back_to_the_start_time():
    """If ActionTask.id turns out not to be the runner's task number, the
    exact start timestamp from the log still identifies the task."""
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 999999, "2026-08-25T14:33:15Z")
    assert got["run_id"] == 829
    assert got["conclusion"] == "failure"


def test_find_task_gives_up_rather_than_guessing():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    assert fj.find_task("FiLL/p", 12345, "2020-01-01T00:00:00Z") is None


def test_a_still_running_task_is_not_reported_as_finished():
    running = {"total_count": 1, "workflow_runs": [
        dict(TASKS["workflow_runs"][0], status="running", updated_at=None)]}
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": running})
    assert fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'forgejo_api'`

- [ ] **Step 3: Write the implementation**

```python
# dashboard/forgejo_api.py
"""Forgejo's account of its own runners and the jobs they ran.

Forgejo answers two questions the runner's log cannot. Whether a runner is
busy: `status` on the admin runners endpoint is enumerated offline/idle/active,
which beats inferring it from a log line. And how a job ended: the
forgejo-runner daemon logs a task starting and never logs it finishing, so
without this the history would have no end times and no results at all.

Everything is best-effort and returns None on failure. A run that cannot be
matched keeps its log-only data rather than being dropped.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Terminal task states. Anything else - running, waiting, blocked - means the
# task has not finished, and reporting an end time for it would be a lie.
FINISHED = {"success", "failure", "cancelled", "skipped"}


class Forgejo:
    def __init__(self, base_url, token):
        self.base = (base_url or "").rstrip("/")
        self.token = token

    # ----------------------------------------------------------------- http
    def _request(self, path, method="GET", params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers={
            # Forgejo's own scheme. Not "Bearer": that is GitHub's, and
            # Forgejo answers 401 to it on some deployments.
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
            "User-Agent": "nomercy-runner-dashboard",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode()
                return json.loads(body) if body.strip() else True
        except urllib.error.HTTPError as e:
            print(f"[forgejo] {e.code} {method} {path}")
            return None
        except Exception as e:  # noqa: BLE001 - surfaced, not raised
            print(f"[forgejo] {method} {path}: {e}")
            return None

    def _get(self, path, params=None):
        return self._request(path, "GET", params)

    # -------------------------------------------------------------- runners
    def runner_statuses(self):
        """{uuid: status}, or None if the call failed.

        None and {} are deliberately different. {} means Forgejo answered and
        knows of no runners; None means we could not ask, and the caller must
        report "unknown" rather than treating every runner as idle - prune and
        drain act on that answer.

        The endpoint returns a BARE ARRAY, not an object with a "runners" key.
        """
        data = self._get("/api/v1/admin/actions/runners",
                         {"limit": 100})
        if not isinstance(data, list):
            return None
        return {r["uuid"]: (r.get("status") or "")
                for r in data if isinstance(r, dict) and r.get("uuid")}

    def runner_ids(self):
        """{uuid: id}, for deregistration. None if the call failed."""
        data = self._get("/api/v1/admin/actions/runners", {"limit": 100})
        if not isinstance(data, list):
            return None
        return {r["uuid"]: r.get("id")
                for r in data if isinstance(r, dict) and r.get("uuid")}

    def registration_token(self):
        data = self._get("/api/v1/admin/actions/runners/registration-token")
        if not isinstance(data, dict):
            return None
        return data.get("token") or None

    def delete_runner(self, runner_id):
        if not runner_id:
            return False
        got = self._request(
            f"/api/v1/admin/actions/runners/{runner_id}", "DELETE")
        return got is not None

    # ---------------------------------------------------------------- tasks
    def find_task(self, repo, task_id, started_at):
        """The finished task a recorded run corresponds to, or None.

        Matched on id first. Whether ActionTask.id is the same number the
        runner logs as "task 830" is the one thing this design could not
        settle from the swagger alone, so the start timestamp - which the log
        gives exactly - is a second, independent way in.

        An unfinished task returns None rather than a row with no end: it will
        be picked up on a later sweep, when it has actually finished.
        """
        data = self._get(f"/api/v1/repos/{repo}/actions/tasks",
                         {"limit": 50})
        # The tasks live under "workflow_runs". The name is Forgejo's, not a
        # mistake here - reading "tasks" gets an empty list and no error.
        tasks = (data or {}).get("workflow_runs") or []

        match = None
        for t in tasks:
            if task_id and t.get("id") == task_id:
                match = t
                break
        if match is None:
            for t in tasks:
                if started_at and t.get("run_started_at") == started_at:
                    match = t
                    break
        if match is None:
            return None

        status = (match.get("status") or "").lower()
        if status not in FINISHED:
            return None

        return {
            "run_id": match.get("id"),
            "repo": repo,
            "workflow": match.get("workflow_id"),
            "branch": match.get("head_branch"),
            "sha": (match.get("head_sha") or "")[:8],
            # Forgejo's task payload carries no actor. Left explicit rather
            # than omitted so the column is written as NULL instead of
            # keeping a stale value from an earlier partial enrichment.
            "actor": None,
            "url": match.get("url"),
            "conclusion": status,
            "ended_at": match.get("updated_at"),
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && python -m pytest tests/test_forgejo_api.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/forgejo_api.py dashboard/tests/test_forgejo_api.py
git commit -m "feat: a client for Forgejo's runner and task API"
```

---

### Task 4: Busy and idle for Forgejo

**Files:**
- Modify: `dashboard/docker_ops.py` (`_registration`, `_job_state`, `is_idle`, `collect`, add `_runner_file`, `_forgejo_job_state`, `_forgejo_current_job`)
- Test: `dashboard/tests/test_forgejo_job_state.py`

**Interfaces:**
- Consumes: `forgejo_api.Forgejo.runner_statuses`, `providers.FORGEJO`.
- Produces:
  - `docker_ops._runner_file(name, provider) -> dict` (empty dict on failure)
  - `docker_ops._job_state(name, provider=None, runner_file=None, forge_status=None) -> (state, job)`
  - `docker_ops.is_idle(name, provider=None, forge_status=None, env=None) -> bool`
  - `docker_ops.prune(name, timeout=300, provider=None, env=None) -> dict`
  - `docker_ops.collect(env=None) -> dict` — each runner dict gains `"provider"`.

`is_idle` takes `env` because it is called from places that hold no forge status
of their own — the drain watcher, and `prune` itself. Without it, a Forgejo
runner's status would always be `None`, which reads as unknown, which reads as
not idle, and prune and drain would be refused for Forgejo runners for ever.

`provider=None` means GitHub. That default is what keeps `test_job_state.py` calling `_job_state("github-runner-7")` green, and it is checked by a test below so it cannot be removed by accident.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_job_state.py
"""Forgejo's own status decides busy/idle; the log only names the job.

The forgejo-runner daemon logs a task starting and never logs it finishing, so
the GitHub approach - take the last job event in the tail - would pin every
Forgejo runner busy for ever after its first job. The API is asked instead.

The unknown-is-not-idle rule carries over unchanged. prune() and the drain
watcher both act on is_idle(), and a wrong "idle" deletes layers a running
build needs.
"""
import docker_ops
import providers

RUNNER_FILE = {
    "id": 4,
    "uuid": "aa11bb22-0000-4757-8626-000000000000",
    "name": "nomercy-forgejo-1",
    "address": "https://forgejo.example",
}

LOG_TAIL = (
    'time="2026-08-25T14:33:15Z" level=info msg="task 829 repo is FiLL/p '
    'https://data.forgejo.org https://forgejo.example"\n'
    'time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/q '
    'https://data.forgejo.org https://forgejo.example"\n'
)

UUID = RUNNER_FILE["uuid"]


def _fake_docker(logs=LOG_TAIL):
    def call(*args, **kwargs):
        return (True, logs, "")
    return call


def test_active_reads_as_busy_and_names_the_last_task(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, job = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, {UUID: "active"})
    assert state == "busy"
    assert job == "task 830 - FiLL/q"


def test_idle_reads_as_idle_even_with_a_task_line_in_the_log(monkeypatch):
    """The log's last line is always a start. Only the API can say it ended."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    assert docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE,
        {UUID: "idle"}) == ("idle", "")


def test_an_unreachable_api_is_unknown_not_idle(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, None)
    assert state == "unknown"


def test_a_runner_forgejo_has_never_heard_of_is_unknown(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, {})
    assert state == "unknown"


def test_an_unregistered_container_is_unknown(monkeypatch):
    """No .runner file yet - it is still registering. Not idle."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, {}, {UUID: "idle"})
    assert state == "unknown"


def test_is_idle_refuses_prune_when_forgejo_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, None) is False


def test_is_idle_allows_prune_on_a_confirmed_idle(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, {UUID: "idle"}) is True


def test_is_idle_asks_the_forge_itself_when_nobody_handed_it_a_status(
        monkeypatch):
    """The drain watcher and prune() hold no status of their own. If is_idle
    did not fetch one, every Forgejo runner would read unknown - and prune and
    drain would be refused for the Forgejo fleet permanently."""
    class _Forge:
        def runner_statuses(self):
            return {UUID: "idle"}

    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: _Forge())
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, env={"x": "y"}) is True


def test_the_github_path_still_works_with_one_argument(monkeypatch):
    """Guards the default that keeps test_job_state.py green."""
    monkeypatch.setattr(docker_ops, "_docker", lambda *a, **k: (
        True, "2026-08-20 14:23:54Z: Listening for Jobs\n", ""))
    assert docker_ops._job_state("github-runner-3") == ("idle", "")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_job_state.py -q`
Expected: FAIL — `_job_state()` takes 1 positional argument.

- [ ] **Step 3: Write the implementation**

In `dashboard/docker_ops.py`, add near the other regexes:

```python
# time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/q ..."
# The daemon logs a task starting and never logs it finishing, which is why
# Forgejo's busy/idle comes from the API and this pattern only names the job.
RE_FORGEJO_TASK = re.compile(
    r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z".*?'
    r'msg="task (\d+) repo is (\S+)')
```

Replace `_registration` and add the new helpers:

```python
def _runner_file(name, provider):
    """The runner's registration file, parsed, or {} if unreadable.

    Read once per collect and passed to everything that needs it: the
    registration name, and - for Forgejo - the uuid that identifies this
    runner to the forge. Reading it twice would double the exec cost of every
    poll for no gain.
    """
    ok, out, _ = _docker("exec", name, "cat", provider.registration_path,
                         timeout=8)
    if not ok or not out:
        return {}
    try:
        # GitHub writes .runner with a UTF-8 BOM. json.loads chokes on it,
        # which silently turned every registration name into "-".
        return json.loads(out.lstrip("﻿"))
    except Exception:  # noqa: BLE001 - rendered, not raised
        return {}


def _registration(name, provider=None, runner_file=None):
    provider = provider or providers.GITHUB
    rf = runner_file if runner_file is not None else _runner_file(name, provider)
    return rf.get(provider.registration_key) or "-"


def _forgejo_current_job(name):
    """A label for whatever task was last picked up. Display only."""
    ok, out, _ = _docker("logs", "--tail", "200", name, timeout=10)
    if not ok:
        return ""
    last = None
    for line in out.splitlines():
        m = RE_FORGEJO_TASK.search(line)
        if m:
            last = m
    return f"task {last.group(2)} - {last.group(3)}" if last else ""


def _forgejo_job_state(name, runner_file, forge_status):
    """busy/idle/unknown from Forgejo, which knows without being inferred.

    forge_status is {uuid: status} or None. None means the API could not be
    reached, and a runner Forgejo has never heard of is equally unknown - a
    container mid-registration has no answer yet, and answering "idle" for it
    would let prune act on a runner about to pick up work.
    """
    uuid = (runner_file or {}).get("uuid")
    if not uuid or forge_status is None:
        return "unknown", ""
    status = forge_status.get(uuid)
    if not status:
        return "unknown", ""
    if status == "active":
        return "busy", _forgejo_current_job(name)
    # idle and offline both mean "running no job". offline is separately
    # visible from the container being down, so it needs no third state here.
    return "idle", ""
```

Change `_job_state` and `is_idle` to dispatch, leaving the GitHub body exactly as it is:

```python
def _job_state(name, provider=None, runner_file=None, forge_status=None):
    """Derive busy/idle/unknown for a runner.

    provider defaults to GitHub so the single-argument form keeps working.
    """
    provider = provider or providers.GITHUB
    if provider is providers.FORGEJO:
        return _forgejo_job_state(name, runner_file, forge_status)

    # ... existing GitHub body, unchanged ...


def is_idle(name, provider=None, forge_status=None, env=None):
    """True only for a definite "idle". "busy" and "unknown" both answer
    False - this gates both the drain watcher and cache prune.

    forge_status is fetched here when the caller has none. collect() already
    holds one for the whole fleet and passes it in; the drain watcher and
    prune() do not, and without this they would read every Forgejo runner as
    unknown and refuse to act on it for ever.
    """
    provider = provider or providers.GITHUB
    rf = None
    if provider is providers.FORGEJO:
        rf = _runner_file(name, provider)
        if forge_status is None:
            client = provider.forge_client(env or {})
            if client is not None:
                forge_status = client.runner_statuses()
    state, _ = _job_state(name, provider, rf, forge_status)
    return state == "idle"
```

And `prune`, whose first act is an idleness check, has to carry the same two
arguments through. Only its signature and that one call change:

```python
def prune(name, timeout=300, provider=None, env=None):
    # ... docstring unchanged ...
    if not is_idle(name, provider, env=env):
        return {"name": name, "ok": False,
                "error": f"{name} became busy before the prune started",
                "before": None, "after": None,
                "freed_bytes": None, "measured": False}
    # ... rest of the body unchanged ...
```

`timeout` stays in second position: `test_docker_ops.py` calls `prune(name)`
and `is_idle(name)` positionally and must not be edited.

Then rework `collect` to take the environment, fetch Forgejo's statuses once, and tag every runner:

```python
def collect(env=None):
    env = env or {}
    stats = _stats_map()
    draining = set(load_state().get("draining", []))
    runners = []
    found = list_runners()

    # One API call for the whole Forgejo fleet, not one per runner. Skipped
    # entirely when no Forgejo runner exists, so a GitHub-only deployment
    # never pays for a forge it does not use.
    forge_status = None
    if any(p is providers.FORGEJO for _, p in found):
        client = providers.FORGEJO.forge_client(env)
        if client is not None:
            forge_status = client.runner_statuses()

    for name, provider in found:
        ok, status, _ = _docker("ps", "-a", "--filter", f"name=^{name}$",
                                "--format", "{{.Status}}")
        status = status or "unknown"
        running = status.startswith("Up")

        if not running:
            runners.append({
                "name": name, "provider": provider.key, "registration": "-",
                "state": "stopped", "job": "", "uptime": status,
                "cpu_percent": 0, "mem_used": "0B", "mem_limit": "-",
                "build_cache": "0B", "images": "0B",
            })
            continue

        rf = _runner_file(name, provider)
        job_state, job = _job_state(name, provider, rf, forge_status)
        if name in draining:
            state = "draining"
        elif job_state == "unknown":
            state = "busy"
            job = job or "state unknown - could not read logs"
        else:
            state = job_state
        cpu_s, mem_s = stats.get(name, ("0%", "0B / 0B"))
        try:
            cpu = float(cpu_s.replace("%", ""))
        except ValueError:
            cpu = 0.0
        mem_used, _, mem_limit = mem_s.partition(" / ")
        cache, images = _inner_df(name)

        runners.append({
            "name": name,
            "provider": provider.key,
            "registration": _registration(name, provider, rf),
            "state": state,
            "job": job,
            "uptime": status.replace("Up ", "").split(" (")[0],
            "cpu_percent": round(cpu, 2),
            "mem_used": mem_used or "0B",
            "mem_limit": mem_limit or "-",
            "build_cache": cache,
            "images": images,
        })

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disk": _disk(),
        "host": host_info(),
        "runners": runners,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_forgejo_job_state.py tests/test_job_state.py -q`
Expected: PASS. `test_job_state.py` is the one that proves the default was preserved.

- [ ] **Step 5: Pass the environment through the collector**

In `dashboard/app.py`, `_collector()` calls `ops.collect()`. Change that one line to `ops.collect(read_env())`, so Forgejo credentials reach the status sweep.

- [ ] **Step 6: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/docker_ops.py dashboard/app.py dashboard/tests/test_forgejo_job_state.py
git commit -m "feat: read Forgejo busy/idle from the forge, not from logs"
```

---

### Task 5: The slim Forgejo runner image

**Files:**
- Create: `forgejo-runner/Dockerfile`
- Create: `scripts/start-forgejo.sh`
- Test: `dashboard/tests/test_forgejo_image.py`

**Interfaces:**
- Consumes: `FORGEJO_INSTANCE_URL`, `FORGEJO_RUNNER_REGISTRATION_TOKEN`, `FORGEJO_RUNNER_LABELS` in the container environment (produced by `providers.FORGEJO.container_env`).
- Produces: an image whose container registers itself on first boot, writes `/data/.runner`, and runs `forgejo-runner daemon` with its own nested `dockerd`.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_image.py
"""The properties of the Forgejo runner image the dashboard depends on.

Not a build test - building it takes minutes and needs the network. These are
the four things that, if they drift, break the dashboard silently rather than
loudly: the nested daemon (prune and the disk figures read it), the storage
driver (overlay-on-overlay does not mount), the build cache cap (a runner
filled a 1 TB disk once already), and the registration path providers.py reads.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

START = os.path.join(ROOT, "scripts", "start-forgejo.sh")
DOCKERFILE = os.path.join(ROOT, "forgejo-runner", "Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_runner_has_its_own_docker_daemon():
    assert "dockerd" in _read(START)


def test_the_daemon_uses_fuse_overlayfs():
    """The kernel cannot stack native overlay2 on the host's overlay."""
    assert "fuse-overlayfs" in _read(START)
    assert "fuse-overlayfs" in _read(DOCKERFILE)


def test_the_build_cache_is_capped():
    """Without a gc policy the nested daemons grow without limit. This is not
    theoretical - they once filled a 1 TB disk."""
    assert "builder" in _read(START) and "gc" in _read(START)


def test_registration_lands_where_providers_expects_it():
    import providers
    assert providers.FORGEJO.registration_path == "/data/.runner"
    start = _read(START)
    assert "/data" in start
    assert "forgejo-runner register" in start


def test_the_daemon_is_the_final_process():
    """exec, not a background start: the container must die when the runner
    does, or Docker reports Up for a runner that is gone."""
    assert "exec forgejo-runner daemon" in _read(START)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_image.py -q`
Expected: FAIL with `FileNotFoundError` for `scripts/start-forgejo.sh`.

- [ ] **Step 3: Write the start script**

```bash
# scripts/start-forgejo.sh
#!/usr/bin/env bash
# Forgejo Actions runner with its own Docker daemon.
#
# The daemon is nested rather than the host's socket being mounted. Forgejo
# jobs run in job containers named by the ubuntu-*:docker:// labels, and with a
# shared socket those containers, their images and their build cache land on
# the engine everything else runs on. That is the failure this whole distro
# exists to prevent.
set -euo pipefail

: "${FORGEJO_INSTANCE_URL:?FORGEJO_INSTANCE_URL is required}"
DATA=/data
mkdir -p "$DATA"
cd "$DATA"

# --- nested daemon -------------------------------------------------------
# fuse-overlayfs (userspace overlay): the kernel cannot stack native overlay2
# on top of the host's overlay filesystem when the container is itself an
# overlay mount.
#
# builder.gc caps the build cache. Without it the nested daemons grow without
# limit - the GitHub fleet once filled a 1 TB disk this way.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "storage-driver": "fuse-overlayfs",
  "builder": {
    "gc": {
      "enabled": true,
      "defaultKeepStorage": "20GB"
    }
  }
}
JSON

# A container restart leaves the pid file behind and dockerd then aborts with
# "pidfile is held by another process".
rm -f /var/run/docker.pid
pkill -9 dockerd 2>/dev/null || true

echo "Starting Docker daemon inside container (storage-driver=fuse-overlayfs)..."
dockerd --host=unix:///var/run/docker.sock > /var/log/dockerd.log 2>&1 &

for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "[FATAL] the nested Docker daemon did not come up:"
  cat /var/log/dockerd.log
  exit 1
fi

# --- registration --------------------------------------------------------
# Registration is once per volume. The dashboard mints a fresh token per
# runner, so a re-register after the file is gone needs a new container.
if [ ! -f "$DATA/.runner" ]; then
  : "${FORGEJO_RUNNER_REGISTRATION_TOKEN:?a registration token is required on first boot}"
  echo "Registering runner against ${FORGEJO_INSTANCE_URL} ..."
  forgejo-runner register --no-interactive \
    --instance "$FORGEJO_INSTANCE_URL" \
    --token "$FORGEJO_RUNNER_REGISTRATION_TOKEN" \
    --name "${FORGEJO_RUNNER_NAME:-$(hostname)}" \
    --labels "$FORGEJO_RUNNER_LABELS"
fi

# --- deregistration on the way out ---------------------------------------
# The dashboard deregisters through the API when it removes a runner, which
# covers the case it knows about. This covers the other one: the container
# being stopped by anything else. Both are idempotent.
trap 'echo "SIGTERM - unregistering"; forgejo-runner unregister 2>/dev/null || true' TERM

exec forgejo-runner daemon --config "$DATA/config.yaml"
```

Note: `forgejo-runner daemon` reads `--config`; the file is created by `register` on first boot. If it is absent the daemon uses its defaults, which is acceptable — the labels live in `.runner`.

- [ ] **Step 4: Write the Dockerfile**

```dockerfile
# forgejo-runner/Dockerfile
#
# A slim runner, not the GitHub runner image. Forgejo jobs run inside job
# containers named by the ubuntu-*:docker:// labels, so the toolchain in the
# GitHub image would be gigabytes this container never executes.
FROM ubuntu:24.04

ARG FORGEJO_RUNNER_VERSION=12.0.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg bash procps tzdata \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu noble stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
        # fuse-overlayfs for nested Docker (overlay-on-overlay)
        fuse-overlayfs fuse3 \
    && curl -fsSL -o /usr/local/bin/forgejo-runner \
        "https://code.forgejo.org/forgejo/runner/releases/download/v${FORGEJO_RUNNER_VERSION}/forgejo-runner-${FORGEJO_RUNNER_VERSION}-linux-amd64" \
    && chmod +x /usr/local/bin/forgejo-runner \
    && forgejo-runner --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY scripts/start-forgejo.sh /root/start-forgejo.sh
RUN chmod +x /root/start-forgejo.sh

VOLUME /data
ENTRYPOINT ["/root/start-forgejo.sh"]
```

Build from the repository root so the `scripts/` copy resolves:

```bash
docker build -f forgejo-runner/Dockerfile \
  -t ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest .
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_forgejo_image.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 6: Build the image for real and check it starts**

```bash
wsl -d github-runners -u root -- bash -lc \
  "cd /mnt/d/docker-compose/GithubRunners && docker build -f forgejo-runner/Dockerfile -t ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest ."
```

Expected: build succeeds and `forgejo-runner --version` printed a version during the build. Do not register a runner yet — that is Task 12.

- [ ] **Step 7: Commit**

```bash
git add forgejo-runner/Dockerfile scripts/start-forgejo.sh dashboard/tests/test_forgejo_image.py
git commit -m "feat: a slim Forgejo runner image with its own daemon"
```

---

### Task 6: History schema for two forges

**Files:**
- Modify: `dashboard/history.py` (`SCHEMA`, `init`, `open_run`, add `apply_close`)
- Test: `dashboard/tests/test_history_provider.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `runs.provider TEXT NOT NULL DEFAULT 'github'`, `runs.forge_task_id INTEGER`
  - `history.open_run(runner, registration, job_name, started_at, provider="github", forge_task_id=None)`
  - `history.apply_close(run_id, ended_at, result)` — closes a run by id, for the Forgejo path where the end comes from the API rather than from a log line.
  - `history.pending_enrichment(limit=20)` rows now include `provider` and `forge_task_id`.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_history_provider.py
"""Two forges in one history table, and an existing database that predates it.

CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
schema change alone would leave the live history without the new columns and
every insert would fail. init() has to migrate.
"""
import sqlite3

import history


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()


def _cols(tmp_path):
    c = sqlite3.connect(str(tmp_path / "h.db"))
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    finally:
        c.close()


def test_a_new_database_has_both_columns(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert {"provider", "forge_task_id"} <= _cols(tmp_path)


def test_an_existing_database_is_migrated(tmp_path, monkeypatch):
    """The live history table predates both columns."""
    db = str(tmp_path / "h.db")
    monkeypatch.setattr(history, "DB_PATH", db)
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " runner TEXT NOT NULL, registration TEXT, job_name TEXT NOT NULL,"
        " started_at TEXT NOT NULL, ended_at TEXT, duration_s INTEGER,"
        " result TEXT, UNIQUE(runner, job_name, started_at));")
    c.execute("INSERT INTO runs (runner, job_name, started_at)"
              " VALUES ('github-runner-1','build','2026-08-01T00:00:00Z')")
    c.commit()
    c.close()

    history.init()

    assert {"provider", "forge_task_id"} <= _cols(tmp_path)
    rows = history.list_runs()
    assert rows[0]["provider"] == "github", \
        "existing history must be labelled, not left NULL"


def test_open_run_keeps_its_four_argument_form(tmp_path, monkeypatch):
    """test_orphaned_runs.py calls it this way and must not be edited."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("github-runner-1", "nomercy-x", "build",
                     "2026-08-25T10:00:00Z")
    assert history.list_runs()[0]["provider"] == "github"


def test_a_forgejo_run_records_its_task(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "nomercy-forgejo-1", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    row = history.list_runs()[0]
    assert (row["provider"], row["forge_task_id"]) == ("forgejo", 830)


def test_apply_close_closes_by_id_and_computes_duration(tmp_path, monkeypatch):
    """Forgejo has no completion log line, so the end arrives by run id."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    run_id = history.list_runs()[0]["id"]
    history.apply_close(run_id, "2026-08-25T14:44:02Z", "success")
    row = history.list_runs()[0]
    assert row["ended_at"] == "2026-08-25T14:44:02Z"
    assert row["result"] == "success"
    assert row["duration_s"] == 307


def test_pending_enrichment_carries_what_the_forgejo_lookup_needs(
        tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    pending = history.pending_enrichment()
    assert pending[0]["provider"] == "forgejo"
    assert pending[0]["forge_task_id"] == 830


def test_an_open_forgejo_run_is_still_pending(tmp_path, monkeypatch):
    """The run above was never closed, and must be listed anyway: the API
    sweep is the only thing that can close it. An ended_at filter here would
    mean no Forgejo run is ever enriched or ever ends."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    assert len(history.pending_enrichment()) == 1


def test_an_open_github_run_is_not_yet_pending(tmp_path, monkeypatch):
    """Unchanged for GitHub: find_job() matches inside the start-to-end
    window, so a run with no end cannot be looked up yet."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("github-runner-1", "nomercy-x", "build",
                     "2026-08-25T10:00:00Z")
    assert history.pending_enrichment() == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_history_provider.py -q`
Expected: FAIL — no `provider` column.

- [ ] **Step 3: Write the implementation**

In `dashboard/history.py`, add the two columns to `SCHEMA`'s `runs` table, immediately after `result TEXT`:

```sql
  provider      TEXT    NOT NULL DEFAULT 'github',
  forge_task_id INTEGER,
```

Add a comment above the `gh_*` block inside `SCHEMA`:

```sql
  -- The gh_ prefix is historical. These columns carry enrichment for both
  -- forges; renaming them would be a migration over live history for no
  -- functional gain.
```

Replace `init()`:

```python
def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS does nothing to a table that already
        # exists, so a deployed database never gains a column from SCHEMA
        # alone. The live history predates both of these.
        have = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        if "provider" not in have:
            c.execute("ALTER TABLE runs ADD COLUMN provider TEXT NOT NULL"
                      " DEFAULT 'github'")
        if "forge_task_id" not in have:
            c.execute("ALTER TABLE runs ADD COLUMN forge_task_id INTEGER")
```

Replace `open_run`:

```python
def open_run(runner, registration, job_name, started_at,
             provider="github", forge_task_id=None):
    # provider and forge_task_id are keyword arguments with defaults, not new
    # positional ones: existing callers and tests pass four arguments.
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO runs"
            " (runner, registration, job_name, started_at, provider,"
            "  forge_task_id) VALUES (?,?,?,?,?,?)",
            (runner, registration, job_name, started_at, provider,
             forge_task_id))
```

Add `apply_close` beside `close_run`:

```python
def apply_close(run_id, ended_at, result):
    """Close one run by id.

    close_run() matches on (runner, job_name) because a GitHub completion line
    is all the log gives. Forgejo's end comes from the API against a run we
    already know the id of, so matching again would only add a way to close
    the wrong row.
    """
    with _lock, _conn() as c:
        row = c.execute("SELECT started_at FROM runs WHERE id=?",
                        (run_id,)).fetchone()
        if not row:
            return
        c.execute(
            "UPDATE runs SET ended_at=?, result=?, duration_s=?"
            " WHERE id=? AND ended_at IS NULL",
            (ended_at, result,
             _seconds_between(row["started_at"], ended_at), run_id))
```

Finally, `pending_enrichment` needs two changes. It names its columns
explicitly, so the new ones have to be added — and its `ended_at IS NOT NULL`
filter would otherwise exclude every Forgejo run that exists, because a Forgejo
run has no end until this very sweep supplies one:

```python
def pending_enrichment(limit=20):
    """Runs still awaiting a forge lookup.

    A GitHub run must have ended first: find_job() matches within the window
    between its start and its end. A Forgejo run is the opposite case - it is
    still open precisely because only the API can close it, so requiring an end
    here would mean no Forgejo run were ever enriched or ever closed.
    """
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, runner, registration, job_name, started_at, ended_at,"
            " provider, forge_task_id"
            " FROM runs WHERE gh_checked=0"
            "   AND (ended_at IS NOT NULL OR provider='forgejo')"
            " ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_history_provider.py tests/test_orphaned_runs.py -q`
Expected: PASS. `test_orphaned_runs.py` proves the four-argument form survived.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/history.py dashboard/tests/test_history_provider.py
git commit -m "feat: record which forge a run belongs to"
```

---

### Task 7: Forgejo history and enrichment

**Files:**
- Modify: `dashboard/history.py` (add `parse_forgejo_events`)
- Modify: `dashboard/app.py` (`_record_history`, `_backfill`, `_enricher`)
- Test: `dashboard/tests/test_forgejo_history.py`

**Interfaces:**
- Consumes: `history.open_run(..., provider=, forge_task_id=)`, `history.apply_close`, `forgejo_api.Forgejo.find_task`.
- Produces: `history.parse_forgejo_events(text) -> [(kind, iso_time, job, task_id)]` where `kind` is always `"start"`.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_history.py
"""Forgejo logs a task starting and never logs it finishing.

Captured verbatim from `docker logs forgejo_runner` on 2026-08-25. The absence
of a completion line is the whole reason the Forgejo path differs: starts come
from the log, ends come from the API.
"""
import history

REAL_LOG = (
    'time="2026-08-25T14:33:15Z" level=info msg="task 829 repo is '
    'FiLL/nomercy-torrent-plugin https://data.forgejo.org http://forgejo:3000"\n'
    'time="2026-08-25T14:19:09Z" level=info msg="UpdateTask returned task '
    'result RESULT_CANCELLED for a task that was in local state '
    'RESULT_UNSPECIFIED - beginning local task termination" task_id=825\n'
    'time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is '
    'FiLL/nomercy-torrent-plugin https://data.forgejo.org http://forgejo:3000"\n'
)


def test_starts_are_extracted_with_task_and_repo():
    got = history.parse_forgejo_events(REAL_LOG)
    assert got == [
        ("start", "2026-08-25T14:33:15Z", "FiLL/nomercy-torrent-plugin", 829),
        ("start", "2026-08-25T14:38:55Z", "FiLL/nomercy-torrent-plugin", 830),
    ]


def test_no_line_is_ever_read_as_an_end():
    """If a future runner version adds one, this test is where that gets
    noticed - rather than the API path silently going unused."""
    assert all(k == "start" for k, _, _, _ in
               history.parse_forgejo_events(REAL_LOG))


def test_unrelated_chatter_is_ignored():
    noise = ('time="2026-08-25T14:19:09Z" level=info msg="runner: '
             'beaststack-runner, with version: v12.0.1, with labels: [...]"\n')
    assert history.parse_forgejo_events(noise) == []


def test_a_github_log_yields_nothing_here():
    """The two parsers must not both claim the same line."""
    gh = "2026-08-20 15:02:11Z: Running job: build-base / docker-build\n"
    assert history.parse_forgejo_events(gh) == []
    assert history.parse_events(REAL_LOG) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_history.py -q`
Expected: FAIL — `history` has no attribute `parse_forgejo_events`.

- [ ] **Step 3: Write the parser**

In `dashboard/history.py`, beside the existing regexes:

```python
# time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/q ..."
#
# There is no matching completion line. forgejo-runner logs a task being
# picked up and nothing when it ends, which is why the Forgejo path closes
# runs from the API instead of from the log.
RE_FORGEJO_START = re.compile(
    r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z".*?'
    r'msg="task (\d+) repo is (\S+)')


def parse_forgejo_events(text):
    """Extract (kind, iso_time, job, task_id). kind is always "start"."""
    events = []
    for line in text.splitlines():
        m = RE_FORGEJO_START.search(line)
        if m:
            events.append(("start", m.group(1) + "Z", m.group(3),
                           int(m.group(2))))
    return events
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && python -m pytest tests/test_forgejo_history.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Wire it into the collector**

In `dashboard/app.py`, `_record_history` currently calls `history.parse_events` for every runner. Split on the provider, which `collect()` now puts on each row:

```python
        try:
            if r.get("provider") == "forgejo":
                for _, when, job, task_id in history.parse_forgejo_events(
                        ops.logs_since(name, 45)):
                    history.open_run(name, r.get("registration"), job, when,
                                     provider="forgejo", forge_task_id=task_id)
            else:
                for kind, when, job, result in history.parse_events(
                        ops.logs_since(name, 45)):
                    if kind == "start":
                        history.open_run(name, r.get("registration"), job, when)
                    else:
                        history.close_run(name, job, when, result)
        except Exception as e:  # noqa: BLE001
            print(f"[history:{name}] {e}")
```

Apply the same split in `_backfill()`, iterating `ops.list_runners()` instead of `ops.list_runner_names()` so the provider is in hand:

```python
        for name, provider in ops.list_runners():
            text = ops.logs_since(name, 7 * 24 * 3600)
            if provider.key == "forgejo":
                events = history.parse_forgejo_events(text)
                for _, when, job, task_id in events:
                    history.open_run(name, None, job, when,
                                     provider="forgejo", forge_task_id=task_id)
            else:
                events = history.parse_events(text)
                for kind, when, job, result in events:
                    if kind == "start":
                        history.open_run(name, None, job, when)
                    else:
                        history.close_run(name, job, when, result)
            if events:
                print(f"[backfill] {name}: {len(events)} events")
```

- [ ] **Step 6: Make the enricher forge-aware**

Replace the body of `_enricher()`'s inner loop so each pending run goes to its own forge. The Forgejo branch closes the run as well as enriching it, because the API is the only place the end time exists:

```python
def _enricher():
    """Fill in repo / workflow / branch / commit, and - for Forgejo - the end.

    Separate from the collector: an API sweep can take seconds and must not
    delay telemetry. Best-effort throughout.
    """
    while True:
        try:
            env = read_env()
            clients = {}
            # Compared as strings: both sides are the same fixed ISO format,
            # which is how started_at is already compared elsewhere in this
            # codebase.
            stale_before = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 24 * 3600))
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
                    if found:
                        # Forgejo's task carries the end, which no log line
                        # does. Close first, then enrich: a crash between the
                        # two leaves a closed run to be enriched next sweep,
                        # rather than an enriched run that never ends.
                        if found.get("ended_at"):
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
                        # unknown and stop asking.
                        history.apply_close(run["id"], run["started_at"],
                                            "unknown")
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
        except Exception as e:  # noqa: BLE001
            print(f"[enricher] {e}")
        time.sleep(90)
```

Add `import providers` to `app.py`'s imports, and remove the local `import providers` added in Task 2 Step 5.

For the Forgejo branch, `run["job_name"]` holds the repository (`FiLL/q`) because that is what the start line gives — which is exactly what `find_task` needs as its `repo` argument.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/history.py dashboard/app.py dashboard/tests/test_forgejo_history.py
git commit -m "feat: Forgejo run history, closed from the API"
```

---

### Task 8: Creating and removing Forgejo runners

**Files:**
- Modify: `dashboard/docker_ops.py` (`create`, `remove`)
- Test: `dashboard/tests/test_forgejo_lifecycle.py`

**Interfaces:**
- Consumes: `providers.Provider.container_env`, `providers.Provider.forge_client`, `forgejo_api.Forgejo.runner_ids`, `forgejo_api.Forgejo.delete_runner`.
- Produces:
  - `docker_ops.create(index, env, provider=None) -> (ok, name, message)`
  - `docker_ops.remove(name, provider=None, env=None) -> (ok, out, err)`

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_lifecycle.py
"""Creating and removing a Forgejo runner.

Two properties matter. A created container must carry the provider label, or
it resolves only by prefix and the label is dead weight. And a removed runner
must be deregistered, or Forgejo keeps showing a runner that no longer exists -
the same ghost the GitHub side deregisters on SIGTERM to avoid.
"""
import docker_ops
import providers


class _FakeForge:
    def __init__(self, fail_delete=False):
        self.deleted = []
        self.fail_delete = fail_delete

    def registration_token(self):
        return "REG-123"

    def runner_ids(self):
        return {"uuid-of-1": 7}

    def delete_runner(self, runner_id):
        self.deleted.append(runner_id)
        return not self.fail_delete


def _capture(monkeypatch, ok=True):
    seen = []

    def call(*args, **kwargs):
        seen.append(args)
        return (ok, "", "" if ok else "boom")

    monkeypatch.setattr(docker_ops, "_docker", call)
    return seen


def test_a_created_forgejo_runner_is_labelled(monkeypatch):
    forge = _FakeForge()
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    seen = _capture(monkeypatch)
    ok, name, _ = docker_ops.create(
        2, {"FORGEJO_INSTANCE_URL": "https://forgejo.example",
            "FORGEJO_API_TOKEN": "t"}, providers.FORGEJO)
    assert ok and name == "forgejo-runner-2"
    args = seen[0]
    assert "--label" in args
    assert f"{providers.LABEL_PROVIDER}=forgejo" in args
    assert "FORGEJO_RUNNER_REGISTRATION_TOKEN=REG-123" in args


def test_a_created_github_runner_is_labelled_too(monkeypatch):
    seen = _capture(monkeypatch)
    ok, name, _ = docker_ops.create(3, {"GH_TOKEN": "t"}, providers.GITHUB)
    assert ok and name == "github-runner-3"
    assert f"{providers.LABEL_PROVIDER}=github" in seen[0]


def test_create_reports_a_forge_that_will_not_mint_a_token(monkeypatch):
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)
    seen = _capture(monkeypatch)
    ok, _, err = docker_ops.create(2, {}, providers.FORGEJO)
    assert ok is False
    assert "FORGEJO" in err
    assert seen == [], "no container may be created without a token"


def test_remove_deregisters_before_deleting(monkeypatch):
    forge = _FakeForge()
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    monkeypatch.setattr(docker_ops, "_runner_file",
                        lambda n, p: {"uuid": "uuid-of-1", "id": 7})
    _capture(monkeypatch)
    ok, _, _ = docker_ops.remove("forgejo-runner-1", providers.FORGEJO, {})
    assert ok is True
    assert forge.deleted == [7]


def test_removal_proceeds_even_when_forgejo_will_not_answer(monkeypatch):
    """A container the operator wants gone must be removable regardless."""
    forge = _FakeForge(fail_delete=True)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    monkeypatch.setattr(docker_ops, "_runner_file",
                        lambda n, p: {"uuid": "uuid-of-1", "id": 7})
    seen = _capture(monkeypatch)
    ok, _, _ = docker_ops.remove("forgejo-runner-1", providers.FORGEJO, {})
    assert ok is True
    assert any(a[0] == "rm" for a in seen), "the container must still be removed"


def test_removing_a_github_runner_asks_no_forge(monkeypatch):
    def boom(env):
        raise AssertionError("the GitHub path must not build a forge client")

    monkeypatch.setattr(providers.FORGEJO, "forge_client", boom)
    _capture(monkeypatch)
    assert docker_ops.remove("github-runner-1")[0] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_lifecycle.py -q`
Expected: FAIL — `create()` takes 2 positional arguments.

- [ ] **Step 3: Write the implementation**

Replace `create` and `remove` in `dashboard/docker_ops.py`:

```python
def create(index, env, provider=None):
    """Create a runner container. Returns (ok, name, message)."""
    provider = provider or providers.GITHUB
    name = provider.name_for(index)

    container_env, err = provider.container_env(env)
    if err:
        # No container is started on a half-built environment: a Forgejo
        # runner without a registration token boots, fails, and restarts for
        # ever under `restart: unless-stopped`.
        return False, name, err

    args = [
        "run", "-d",
        "--name", name,
        "--label", LABEL,
        "--label", f"{providers.LABEL_PROVIDER}={provider.key}",
        "--privileged",
        "--restart", "unless-stopped",
        "--stop-timeout", "60",
        "--tmpfs", "/tmp",
    ]
    if provider is providers.GITHUB:
        args += ["-v", f"{REPO_HOST_PATH}/scripts/start.sh:/root/start.sh:ro"]
    for k, v in container_env.items():
        args += ["-e", f"{k}={v}"]

    cpu = (env.get("RUNNER_CPU_LIMIT") or "0").strip()
    mem = (env.get("RUNNER_MEM_LIMIT") or "0").strip()
    if cpu not in ("", "0"):
        args += ["--cpus", cpu]
    if mem not in ("", "0"):
        args += ["--memory", mem]
    args.append(provider.image)

    ok, out, err = _docker(*args, timeout=180)
    return ok, name, (err or out)


def remove(name, provider=None, env=None):
    """Remove a runner, deregistering it from its forge first where needed.

    start.sh deregisters a GitHub runner on SIGTERM, so that side needs
    nothing here. forgejo-runner does not deregister on its own, and a removed
    container would leave a runner sitting at "offline" in Forgejo for ever.

    A failed deregistration does not block the removal. The operator asked for
    the container to be gone, and a forge that is not answering must not be
    able to veto that; the stale registration is visible and deletable in
    Forgejo's own UI.
    """
    provider = provider or providers.GITHUB
    if provider is providers.FORGEJO:
        try:
            client = provider.forge_client(env or {})
            if client is not None:
                rf = _runner_file(name, provider)
                # The id in .runner is written at registration. Falling back
                # to the live map covers a file that is missing or stale.
                runner_id = rf.get("id")
                if not runner_id and rf.get("uuid"):
                    runner_id = (client.runner_ids() or {}).get(rf["uuid"])
                if runner_id:
                    client.delete_runner(runner_id)
        except Exception as e:  # noqa: BLE001 - never blocks the removal
            print(f"[forgejo:deregister:{name}] {e}")

    # 180s: these containers hold a nested daemon, and tearing that down on
    # removal has been observed to take ~110s.
    ok, out, err = _docker("rm", "-f", name, timeout=180)
    set_draining(name, False)
    return ok, out, err
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_forgejo_lifecycle.py -q`
Expected: PASS, 6 tests.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/docker_ops.py dashboard/tests/test_forgejo_lifecycle.py
git commit -m "feat: create and remove Forgejo runners, deregistering on the way out"
```

---

### Task 9: Configuration and masking

**Files:**
- Modify: `dashboard/app.py` (`EDITABLE`)
- Modify: `dashboard/runner_detail.py` (`SECRET_KEYS`)
- Modify: `.env.example`
- Test: `dashboard/tests/test_forgejo_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `FORGEJO_INSTANCE_URL`, `FORGEJO_API_TOKEN`, `FORGEJO_RUNNER_LABELS` settable from the settings page, with the token masked everywhere a value is rendered.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_config.py
"""The admin token is a credential and must be treated as one.

FORGEJO_API_TOKEN can mint registration tokens and delete runners. Rendering
it unmasked on a page anyone with dashboard access can open would hand that
over, and the token is one .env line away from GH_TOKEN, which is masked.
"""
import runner_detail


def test_the_admin_token_is_masked():
    assert "FORGEJO_API_TOKEN" in runner_detail.SECRET_KEYS


def test_the_instance_url_is_not_a_secret():
    """It appears in the runner's own log lines. Masking it would only make
    the settings page harder to check without hiding anything."""
    assert "FORGEJO_INSTANCE_URL" not in runner_detail.SECRET_KEYS


def test_masking_leaves_a_token_recognisable_but_unusable():
    masked = runner_detail.mask("forgejo-abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnop" not in masked


def test_the_settings_page_can_write_the_forgejo_keys():
    import app as dash
    for key in ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN",
                "FORGEJO_RUNNER_LABELS"):
        assert key in dash.EDITABLE, key


def test_editable_is_still_an_allowlist():
    """A typo'd or injected key in .env is a foothold, not a cosmetic bug."""
    import app as dash
    assert "PATH" not in dash.EDITABLE
    assert "OIDC_CLIENT_SECRET" not in dash.EDITABLE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_config.py -q`
Expected: FAIL — `FORGEJO_API_TOKEN` not in `SECRET_KEYS`.

- [ ] **Step 3: Write the implementation**

In `dashboard/runner_detail.py`:

```python
# Values that must never be rendered. FORGEJO_API_TOKEN mints registration
# tokens and deletes runners, so it is exactly as sensitive as GH_TOKEN.
SECRET_KEYS = {"GH_TOKEN", "FORGEJO_API_TOKEN"}
```

In `dashboard/app.py`:

```python
EDITABLE = {
    "GH_TOKEN", "GITHUB_ORG", "RUNNER_LABELS",
    "RUNNER_GROUP", "RUNNER_CPU_LIMIT", "RUNNER_MEM_LIMIT",
    "FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN", "FORGEJO_RUNNER_LABELS",
}
```

Append to `.env.example`:

```bash
# --- Forgejo runners ------------------------------------------------------
# The public URL, not http://forgejo:3000 - that compose network does not
# exist on the runners' distro - and not the host gateway 172.28.192.1, which
# changes across a reboot.
FORGEJO_INSTANCE_URL=https://forgejo.example.tld

# An admin API token. Used to read runner status, close out run history,
# deregister a removed runner, and mint a registration token per new runner.
# Because the dashboard mints those itself, there is no static registration
# token to keep here.
FORGEJO_API_TOKEN=

# Label-to-image mapping for job containers, exactly as forgejo-runner
# register --labels expects it.
FORGEJO_RUNNER_LABELS=ubuntu-latest:docker://data.forgejo.org/oci/node:lts,ubuntu-22.04:docker://node:20-bookworm-slim,ubuntu-24.04:docker://node:22-noble
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_forgejo_config.py tests/test_masking.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/app.py dashboard/runner_detail.py .env.example dashboard/tests/test_forgejo_config.py
git commit -m "feat: Forgejo settings, with the admin token masked"
```

---

### Task 10: Routes for two fleets

**Files:**
- Modify: `dashboard/app.py` (`_detail_target`, `runner_page`, `_target`, `api_runner`, `api_add`, `api_recreate`, `api_prune_all`, `api_runner_prune`)
- Test: `dashboard/tests/test_forgejo_routes.py`

**Interfaces:**
- Consumes: `providers.valid_name`, `providers.for_name`, `providers.by_key`, `docker_ops.list_runners`.
- Produces: every runner route accepting `forgejo-runner-N`; `/api/runner/add` and `/api/recreate` requiring `provider` in the JSON body.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_forgejo_routes.py
"""Routes must reach both fleets, and destructive ones must not guess.

"Recreate fleet" removes and rebuilds every runner it is given. With two
fleets on one engine, a request that does not say which one is a request to
destroy the wrong one - so it is rejected rather than defaulted.
"""
import docker_ops
import providers
import runner_detail

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    runner_detail._cache.clear()
    yield


PS = ("github-runner-1\t\n"
      "forgejo-runner-1\tforgejo\n")


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, PS, ""))


def test_a_forgejo_name_is_accepted_by_the_guard(client, fleet):
    r = client.post("/api/runner/start", json={"name": "forgejo-runner-1"})
    assert r.status_code != 400


def test_an_unknown_forgejo_index_is_still_rejected(client, fleet):
    r = client.post("/api/runner/start", json={"name": "forgejo-runner-9"})
    assert r.status_code == 404


def test_a_crafted_name_is_still_refused(client, fleet):
    for bad in ("forgejo-runner-1;id", "gitlab-runner-1", "forgejo-runner-"):
        r = client.post("/api/runner/start", json={"name": bad})
        assert r.status_code == 400, bad


def test_add_requires_a_provider(client, fleet):
    r = client.post("/api/runner/add", json={})
    assert r.status_code == 400
    assert "provider" in r.get_json()["error"]


def test_add_rejects_an_unknown_provider(client, fleet):
    r = client.post("/api/runner/add", json={"provider": "gitlab"})
    assert r.status_code == 400


def test_recreate_requires_a_provider(client, fleet):
    """The one that would otherwise destroy the wrong fleet."""
    r = client.post("/api/recreate", json={})
    assert r.status_code == 400


def test_prune_all_requires_a_provider(client, fleet):
    """Two per-fleet buttons posting to one fleet-blind endpoint would make
    one of them a lie."""
    r = client.post("/api/prune-all", json={})
    assert r.status_code == 400


def test_add_passes_the_provider_through(client, fleet, monkeypatch):
    seen = {}

    def fake_create(index, env, provider=None):
        seen["provider"] = provider
        return True, provider.name_for(index), ""

    monkeypatch.setattr(docker_ops, "create", fake_create)
    r = client.post("/api/runner/add", json={"provider": "forgejo"})
    assert r.status_code == 200
    assert seen["provider"] is providers.FORGEJO
    assert r.get_json()["name"] == "forgejo-runner-2"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_forgejo_routes.py -q`
Expected: FAIL — the guard rejects `forgejo-runner-1` with 400.

- [ ] **Step 3: Write the implementation**

In `dashboard/app.py`, replace the three name guards. They keep validating against a fixed allowlist; only the allowlist widened:

```python
def _detail_target(name):
    """Validate a runner name from the URL, and resolve its provider.

    Checked before any docker call so a crafted name can never reach the
    command line.
    """
    if not providers.valid_name(name):
        return None, None, (jsonify(ok=False, error="bad runner name"), 400)
    for existing, provider in ops.list_runners():
        if existing == name:
            return name, provider, None
    return None, None, (jsonify(ok=False, error="no such runner"), 404)


def _detail(name, fn, *args, **kwargs):
    name, _provider, err = _detail_target(name)
    if err:
        return err
    result = fn(name, *args, **kwargs)
    return jsonify(result), (200 if result.get("ok") else 500)


def _target():
    name = (request.json or {}).get("name", "")
    if not providers.valid_name(name):
        return None, None, (jsonify(error="bad runner name"), 400)
    for existing, provider in ops.list_runners():
        if existing == name:
            return name, provider, None
    return None, None, (jsonify(error="no such runner"), 404)
```

`runner_page` uses `providers.valid_name(name)` in place of its inline regex.

Update the callers to unpack three values. In `api_runner`, pass the provider to `remove`:

```python
@app.route("/api/runner/<action>", methods=["POST"])
def api_runner(action):
    name, provider, err = _target()
    if err:
        return err
    ...
    elif action == "remove":
        ok, _, e = ops.remove(name, provider, read_env())
```

Add a helper and rewrite `api_add`:

```python
def _requested_provider():
    """The fleet a body-carrying request is about.

    Required, never defaulted. Both callers are destructive or creative at
    fleet scale, and guessing wrong acts on the fleet the operator did not
    mean.
    """
    key = (request.json or {}).get("provider")
    p = providers.by_key(key)
    if p is None:
        return None, (jsonify(ok=False,
                              error="a provider is required: "
                                    "github or forgejo"), 400)
    return p, None


@app.route("/api/runner/add", methods=["POST"])
def api_add():
    provider, err = _requested_provider()
    if err:
        return err
    idx = ops.next_free_index(provider)
    ok, name, msg = ops.create(idx, read_env(), provider)
    return jsonify(ok=ok, name=name, error=None if ok else msg), \
        (200 if ok else 500)
```

In `api_recreate`, take the provider the same way and restrict the loop to that fleet:

```python
@app.route("/api/recreate", methods=["POST"])
def api_recreate():
    provider, err = _requested_provider()
    if err:
        return err
    names = [n for n, p in ops.list_runners() if p is provider]
    # ... existing body, iterating `names`, passing `provider` to
    #     ops.remove(name, provider, env) and ops.create(idx, env, provider)
```

In `api_runner_prune`, pass the provider and the environment through both calls,
so a Forgejo runner's idleness is read from the forge rather than from a GitHub
log pattern that will never match it:

```python
    name, provider, err = _detail_target(name)
    if err:
        return err
    env = read_env()
    if not ops.is_idle(name, provider, env=env):
        return jsonify(ok=False, error=f"{name} is busy running a job"), 409
    result = ops.prune(name, provider=provider, env=env)
```

`api_prune_all` takes a provider too, and prunes only that fleet:

```python
@app.route("/api/prune-all", methods=["POST"])
def api_prune_all():
    provider, err = _requested_provider()
    if err:
        return err
    env = read_env()
    targets = [n for n, p in ops.list_runners() if p is provider]
    # ... existing body, over `targets`, passing provider and env into
    #     ops.is_idle(name, provider, env=env) and
    #     ops.prune(name, provider=provider, env=env)
```

Without the provider it would clear both fleets' caches whichever button was
pressed — which is exactly the ambiguity two sections exist to remove, and it
would silently make one of the two buttons a lie.

`_drain_watcher` also calls `ops.is_idle(name)`, and it works from names alone
because the drain state is a list of names. Resolve the provider from the name
there — the containers it looks at are by definition still present, and
`providers.for_name` is exactly this lookup:

```python
            for name in list(ops.load_state().get("draining", [])):
                if name not in ops.list_runner_names():
                    ops.set_draining(name, False)
                    continue
                if ops.is_idle(name, providers.for_name(name), env=read_env()):
                    print(f"[drain] {name} idle - stopping")
                    ops.stop(name)
                    ops.set_draining(name, False)
```

Without this the drain watcher would read every Forgejo runner as unknown, and
a drained Forgejo runner would stay marked draining and never stop.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_forgejo_routes.py tests/test_routes.py -q`
Expected: PASS. `test_routes.py` is the one proving the guard still refuses crafted names.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/app.py dashboard/tests/test_forgejo_routes.py
git commit -m "feat: routes for both fleets, with no defaulted provider"
```

---

### Task 11: Two sections on the status page

**Files:**
- Modify: `dashboard/templates/index.html`
- Test: `dashboard/tests/test_two_sections.py`

**Interfaces:**
- Consumes: the `provider` key on each runner in the `/api/status` and `/ws/fleet` payload (Task 4).
- Produces: two headed sections, each with its own grid and action row; fleet-wide counters unchanged.

- [ ] **Step 1: Write the failing test**

```python
# dashboard/tests/test_two_sections.py
"""Each fleet gets its own grid and its own buttons.

The point is not cosmetic. "Recreate fleet" and "Clear all cache" are
destructive and fleet-specific; a single global button above a mixed grid
leaves it ambiguous which runners it is about to take out.
"""
import os
import re

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "index.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


def test_there_is_a_grid_per_provider():
    html = _html()
    assert 'id="grid-github"' in html
    assert 'id="grid-forgejo"' in html


def test_each_fleet_has_its_own_destructive_buttons():
    html = _html()
    for key in ("github", "forgejo"):
        assert f'data-provider="{key}"' in html
        assert f'id="btn-add-{key}"' in html
        assert f'id="btn-recreate-{key}"' in html


def test_every_fleet_action_names_its_provider():
    """A POST to add or recreate without a provider is a 400 by design, so a
    button that omits it is a button that cannot work."""
    html = _html()
    for m in re.finditer(r"/api/(runner/add|recreate)", html):
        window = html[max(0, m.start() - 400):m.start() + 400]
        assert "provider" in window, m.group(0)


def test_the_counters_stay_fleet_wide():
    html = _html()
    assert 'id="s-online"' in html
    assert 'id="s-busy"' in html
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && python -m pytest tests/test_two_sections.py -q`
Expected: FAIL — no `grid-github`.

- [ ] **Step 3: Write the implementation**

Replace the single grid in `dashboard/templates/index.html`:

```html
<div class="fleet" data-provider="github">
  <h2 class="fleet-head">GitHub</h2>
  <div class="actions">
    <button id="btn-add-github" class="primary" data-provider="github">+ Add runner</button>
    <button id="btn-recreate-github" class="warn" data-provider="github">Recreate fleet</button>
    <button id="btn-prune-github" class="warn" data-provider="github">Clear all cache</button>
  </div>
  <div class="grid" id="grid-github"><div class="skel">Awaiting telemetry</div></div>
</div>

<div class="fleet" data-provider="forgejo">
  <h2 class="fleet-head">Forgejo</h2>
  <div class="actions">
    <button id="btn-add-forgejo" class="primary" data-provider="forgejo">+ Add runner</button>
    <button id="btn-recreate-forgejo" class="warn" data-provider="forgejo">Recreate fleet</button>
    <button id="btn-prune-forgejo" class="warn" data-provider="forgejo">Clear all cache</button>
  </div>
  <div class="grid" id="grid-forgejo"><div class="skel">Awaiting telemetry</div></div>
</div>
```

In the render function, route each runner to the grid its `provider` names, and hide a section that has no runners so a GitHub-only deployment does not grow an empty Forgejo heading:

```js
const GRIDS = {github: $('grid-github'), forgejo: $('grid-forgejo')};

function render(rs) {
  const byProvider = {github: [], forgejo: []};
  rs.forEach(r => (byProvider[r.provider] || byProvider.github).push(r));

  Object.keys(GRIDS).forEach(key => {
    const grid = GRIDS[key];
    const list = byProvider[key];
    // An empty fleet hides its whole section, heading and buttons included:
    // a "Recreate fleet" button above nothing is a button with nothing safe
    // to do.
    grid.closest('.fleet').style.display = list.length ? '' : 'none';
    // ... existing per-card diffing, against `grid` rather than the old
    //     single `grid` constant, and keyed by r.name as before
  });
}
```

Every fleet button posts its provider:

```js
document.querySelectorAll('[id^="btn-add-"]').forEach(b =>
  b.addEventListener('click', () =>
    post('/api/runner/add', {provider: b.dataset.provider})));

document.querySelectorAll('[id^="btn-recreate-"]').forEach(b =>
  b.addEventListener('click', () => confirmThen(
    `Recreate the ${b.dataset.provider} fleet?`,
    () => post('/api/recreate', {provider: b.dataset.provider}))));
```

Keep the existing confirm-dialog helper — `test_confirm_dialogs.py` asserts destructive actions go through it, and that test must stay green.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd dashboard && python -m pytest tests/test_two_sections.py tests/test_confirm_dialogs.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd dashboard && python -m pytest tests/ -q
git add dashboard/templates/index.html dashboard/tests/test_two_sections.py
git commit -m "feat: a section per fleet, so a destructive button names its own"
```

---

### Task 12: Deploy and migrate

**Files:**
- Modify: `docker-compose.runners.yml`
- Modify: `README.md`
- Create: `docs/forgejo-runner-migration.md`

**Interfaces:**
- Consumes: everything above.
- Produces: Forgejo runners serving jobs from the isolated distro, and BeastStack's `forgejo_runner` retired.

This task changes running infrastructure. Do not begin it until Tasks 1–11 are committed and the full suite is green.

- [ ] **Step 1: Add the compose service**

In `docker-compose.runners.yml`, after the six `github-runner-N` services:

```yaml
  # Forgejo Actions runner. Own nested daemon, exactly like the GitHub
  # runners, so a Forgejo job filling a disk cannot reach anything else.
  #
  # Reached over the public URL rather than http://forgejo:3000: that compose
  # network is on Docker Desktop's engine and does not exist here.
  forgejo-runner-1:
    image: ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest
    container_name: forgejo-runner-1
    restart: unless-stopped
    privileged: true
    labels:
      nomercy.runner: "true"
      nomercy.provider: "forgejo"
    environment:
      FORGEJO_INSTANCE_URL: ${FORGEJO_INSTANCE_URL}
      FORGEJO_RUNNER_REGISTRATION_TOKEN: ${FORGEJO_RUNNER_REGISTRATION_TOKEN:-}
      FORGEJO_RUNNER_LABELS: ${FORGEJO_RUNNER_LABELS}
      FORGEJO_RUNNER_NAME: forgejo-runner-1
    volumes:
      - forgejo-runner-1-data:/data
    tmpfs:
      - /tmp
    stop_grace_period: 60s
```

and register the volume under the existing `volumes:` block:

```yaml
  forgejo-runner-1-data:
```

The registration token is optional here because it is only read on first boot; runners the dashboard creates get a freshly minted one instead.

- [ ] **Step 2: Set the environment**

Add to `.env` (values, not placeholders):

```
FORGEJO_INSTANCE_URL=https://forgejo.phillippepelzer.me
FORGEJO_API_TOKEN=<an admin API token from Forgejo: Settings > Applications>
FORGEJO_RUNNER_LABELS=<copy the --labels string from BeastStack/forgejo/docker-compose.yml>
FORGEJO_RUNNER_REGISTRATION_TOKEN=<from Forgejo: Site Administration > Actions > Runners>
```

- [ ] **Step 3: Verify the API answers before starting anything**

```bash
wsl -d github-runners -u root -- bash -lc \
  'curl -s -H "Authorization: token $FORGEJO_API_TOKEN" \
   https://forgejo.phillippepelzer.me/api/v1/admin/actions/runners | head -c 400'
```

Expected: a JSON **array** of runners, including `beaststack-runner`. If this returns `{"message":...}` the token lacks admin rights — fix that before continuing; every Forgejo feature in the dashboard depends on it.

- [ ] **Step 4: Start the new runner alongside the old one**

```bash
wsl -d github-runners -u root -- bash -lc \
  "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d forgejo-runner-1 dashboard"
```

Expected: `docker logs forgejo-runner-1` shows the nested daemon starting, then a registration, then the daemon polling. Both runners are now live; Forgejo distributes tasks between them, so nothing stalls.

- [ ] **Step 5: Verify the dashboard**

Open the dashboard. Expected:
- A Forgejo section appears with `forgejo-runner-1` in it.
- Its state reads `idle`, and flips to `busy` while a Forgejo job runs.
- The GitHub section still shows all eight runners — they carry no provider label, so this is the check that the prefix fallback works in production, not just in a test.
- After a Forgejo job completes, it appears on the history page within ~90s with a result, a branch and a link.

- [ ] **Step 6: Retire the old runner**

Only once step 5 has shown a completed Forgejo run in the history:

```bash
docker compose -f /d/docker-compose/BeastStack/forgejo/docker-compose.yml stop forgejo_runner
docker compose -f /d/docker-compose/BeastStack/forgejo/docker-compose.yml rm -f forgejo_runner
```

Then deregister `beaststack-runner` in Forgejo: Site Administration → Actions → Runners → delete. Leaving it registered leaves a permanent offline entry.

Backing out is not doing this step. Until it is done, the old runner is still there and still working.

- [ ] **Step 7: Comment out the retired service**

In `BeastStack/forgejo/docker-compose.yml`, comment out the `forgejo_runner` service with a note pointing at the new home, rather than deleting it — so the next person to read that file learns where the runner went instead of concluding there never was one.

- [ ] **Step 8: Write the migration note and update the README**

Create `docs/forgejo-runner-migration.md` recording: what moved, why (the shared production socket), the new URL, where the admin token comes from, and how to roll back by restarting `forgejo_runner` in BeastStack.

In `README.md`, add a short section noting that the dashboard now covers both GitHub and Forgejo runners and pointing at the design doc.

- [ ] **Step 9: Commit**

```bash
git add docker-compose.runners.yml README.md docs/forgejo-runner-migration.md
git commit -m "feat: run the Forgejo runner on the isolated engine"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Provider seam, `providers.py` | 1 |
| Identity: label with prefix fallback | 1, 2, 8 |
| `docker_ops` generalisation | 2, 4, 8 |
| `app.py` name guards, provider in add/recreate | 10 |
| Busy/idle from `ActionRunner.status`, matched on uuid | 4 |
| Unknown-is-not-idle preserved | 4 |
| History: start from log, end from API | 3, 6, 7 |
| `ActionTask.id` verification with start-time fallback | 3 (both paths implemented and tested) |
| Schema: `provider`, `forge_task_id` | 6 |
| Backfill and interrupted runs | 7 |
| Deregistration on remove | 8 |
| Configuration keys, `EDITABLE`, `SECRET_KEYS` | 9 |
| Network: public URL | 5, 12 |
| Presentation: two sections | 11 |
| Migration without a gap | 12 |
| Testing constraints | Global Constraints, and Tasks 4, 6, 10, 11 each re-run the specific pre-existing file they could break |

No spec section is unimplemented.

**Placeholder scan:** No TBDs. The one open question from the spec — whether `ActionTask.id` is the runner's task number — is resolved by implementing *both* match strategies in Task 3, each with its own test, so no task waits on an answer.

**Type consistency:** `Provider` attribute and method names are used identically in Tasks 1, 2, 4, 8, 10. `find_task(repo, task_id, started_at)` returns the `history.apply_enrichment` key set plus `ended_at`, consumed exactly that way in Task 7. `_job_state(name, provider=None, runner_file=None, forge_status=None)`, `is_idle(name, provider=None, forge_status=None, env=None)` and `prune(name, timeout=300, provider=None, env=None)` all keep the positional forms `test_docker_ops.py` and `test_job_state.py` use. `list_runners()` returns `(name, provider)` tuples, unpacked that way in Tasks 2, 7, 10.

**Three defects the review caught, fixed inline:**

1. `pending_enrichment` filters `ended_at IS NOT NULL`. A Forgejo run has no end until the enrichment sweep supplies one, so every Forgejo run would have been invisible to the only thing that could close it — the history would have shown starts and nothing else, for ever. Task 6 now widens the filter and Task 6's tests pin both sides of it.
2. `is_idle` and `prune` had no way to obtain a Forgejo status, so both would have read `unknown`, which reads as not idle. Prune and drain would have been refused for the Forgejo fleet permanently. Both now take `env` and fetch when the caller has none; Task 4 has a test for it, and Task 10 fixes the drain watcher the same way.
3. The Forgejo enrichment path had no give-up rule, so an unmatchable run would have been retried every 90 seconds for the life of the deployment. Task 7 closes it as `unknown` after a day.
