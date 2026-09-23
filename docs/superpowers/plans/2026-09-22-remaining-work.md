# Finishing the uniform runner platform - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the platform does not yet do as `uniform.md` and the design describe, done and proven on the live fleet, plus the labels a workflow can use on every dashboard card.

**Architecture:** The controller (dashboard/control) drives every runner through worker agents (agent/) over mTLS; the dashboard (dashboard/api_v2.py, templates) reads the same SQLite store. Code changes are tested locally, deployed as images/agents, then proven live. Live steps go through the controller's own service, never by hand.

**Tech Stack:** Python 3.12/3.13, Flask, SQLite, pytest; PowerShell 5.1 (Windows agent, installers); Docker on the Linux worker; QEMU/Docker-OSX on the macOS host VM; Hyper-V.

**Spec:** `docs/superpowers/specs/2026-09-22-remaining-work-design.md`

## Global Constraints

- No job is ever aborted: destructive steps only through the controller, which drains first and acts on idle/drained runners.
- One runner of a fleet out of service at a time.
- Deploy order: worker agent, then control plane, then dashboard; never while the controller is rebuilding runners.
- Every live change announced first; changes to live infrastructure, registrations or production configuration need the operator's go at the time. Already given on 2026-09-22: W1 rollout after one successful job; creating the GitHub macOS runner; macOS one guest per runner at 24 GiB.
- Back up `control.db` and `history.db` (SQLite backup API, `docker exec -i`) before every controller deploy.
- Tests first (red, then green); full agent and dashboard suites green before any deploy.
- Commits in the operator's name, no AI attribution, not pushed.
- Out of scope: the `nomercy-ffmpeg` repository; buying a Windows Server licence.
- Backslashes: write any file containing Windows paths with the Write/Edit tools, never through a Bash heredoc (the Bash tool collapses `\\`).

## Environment facts every task needs

- Repo: `D:\docker-compose\GithubRunners`. Tests: in Git Bash, `export PATH="/c/Program Files/Git/bin:$PATH" PYTHONDONTWRITEBYTECODE=1`, then `cd agent && python -m pytest -q -p no:cacheprovider` and `cd dashboard && python -m pytest -q -p no:cacheprovider` (dashboard suite ~8 min).
- SSH (Git Bash): `K="-i D:/HyperV/runner-platform/ssh/id_ed25519 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=D:/HyperV/runner-platform/ssh/known_hosts -o BatchMode=yes"`; control plane `rnr-admin@10.77.0.10`, Linux worker `rnr-admin@10.77.0.20`, via `/c/Windows/System32/OpenSSH/ssh.exe $K ...`. macOS host: `ssh -i C:/Users/phill/.ssh/macos_runner -o IdentitiesOnly=yes runner@172.19.136.46`.
- Controller service in the container: `sudo docker exec -i rnr-controller python -` with `import api_v2; svc,_ = api_v2.control_plane()` (the same service the dashboard buttons use). Audit every operator change with `from control import audit; audit.record("/data/control.db", <verb>, "accepted", actor="operator (<why>)", ...)`.
- Controller deploy: build `nomercy/runner-dashboard:<hash>` on the control plane from `dashboard/` (without `tests/`), then `bash /tmp/deploy-cp.sh <hash>` (script in the session scratchpad; it backs up both databases, swaps both `image:` lines, restarts controller then dashboard, prints the rollback line).
- Windows agent deploy: the operator runs `D:\tmp\runner-fix-2.ps1` elevated (it runs `infra\hyperv\Install-WindowsWorker.ps1 -WindowsStorage` from HEAD).

---

### Task 1: W1 - roll the hook fix out to the GitHub Linux fleet

**Files:** none (live operation). Evidence: `docs/superpowers/plans/2026-09-17-uniform-evidence.md`.

**Interfaces:** Consumes the fleet's unit template `nomercy/runner-unit-github:e6b7a117356a` (already set) and `RunnerService.recreate(runner_id, requested_by=...)`.

- [ ] **Step 1: Wait for the proof job.** Poll every 30 s until a run on `github-linux-x64-1` after `2026-09-22T12:59Z` has ended:

```bash
/c/Windows/System32/OpenSSH/ssh.exe $K rnr-admin@10.77.0.10 "sudo docker exec rnr-dashboard python -c \"
import sqlite3; c=sqlite3.connect('/data/history.db'); print(c.execute(\\\"select runner,gh_repo,result,gh_url from runs where runner='github-linux-x64-1' and started_at>='2026-09-22T12:59' and ended_at is not null\\\").fetchall())\""
```

Expected: at least one row. If its result is `Failed`, open its log with `gh run view <id> -R <repo> --log-failed` and confirm the failing step is not "Complete runner" and that no line says "is not a valid path to a script". A failing workflow step is the workflow's; a "Complete runner" failure stops this task.

- [ ] **Step 2: Recreate the other nine through the controller.** Only idle ones; the controller takes turns (one replacement per fleet):

```python
import api_v2
svc,_ = api_v2.control_plane()
for s in svc.specs.list(fleet_id="github-linux-x64"):
    if s.get("deleted_at") or s["runtime_template"] == "nomercy/runner-unit-github:e6b7a117356a":
        continue
    print(s["display_name"], svc.recreate(s["runner_id"], requested_by="operator (job-completed hook fix)"))
```

- [ ] **Step 3: Watch until done.** Every 20 s list `display_name, actual_state, last_error` for the fleet until every runner is `idle`/`busy` with no `current_operation`. Any `last_error` stops the task for diagnosis.

- [ ] **Step 4: Verify units.** On the Linux worker:

```bash
for c in $(sudo docker ps -q --filter name=rnr-); do sudo docker inspect -f "{{.Name}} {{.Config.Image}} {{.HostConfig.CpusetCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.ReadonlyRootfs}}" $c; done
```

Expected: ten GitHub units on image id `sha256:fd6589ed...` with cpusets unchanged from before (0-15, 4-19, 8-23, 38-53, 30-45, 12-27, 24-39, 17-32, 34-49, 25-40), memory 34359738368, swap 68719476736, read-only true. And GitHub's API lists all ten online with `self-hosted, Linux, X64, beast-unit`.

- [ ] **Step 5: Record evidence** (a dated section in the evidence file: the proof run URL, the recreate times, the inspect output) and commit:

```bash
git add docs/superpowers/plans/2026-09-17-uniform-evidence.md
git commit -m "docs(evidence): the GitHub Linux fleet on the fixed job-completed hook"
```

---

### Task 2: W2a - the provider says which labels a record and a fleet carry

**Files:**
- Modify: `dashboard/providers.py` (the `Provider` base class near line 299, `_GitHub` near line 457, `_Forgejo` near line 739)
- Test: `dashboard/tests/test_workflow_labels.py` (create)

**Interfaces:**
- Produces: `Provider.record_labels(record) -> list[str] | None` (None when the record says nothing about labels); `Provider.workflow_labels(spec_or_fleet, env) -> list[str]` (what `runs-on:` can name for a runner registered from this spec or fleet).

- [ ] **Step 1: Write the failing tests**

```python
"""The labels a workflow can put in runs-on, as the forge has them."""
import providers as P

GH = P.by_key("github")
FJ = P.by_key("forgejo")


def test_github_record_labels_are_the_label_names():
    record = {"id": 7, "labels": [{"name": "self-hosted", "type": "read-only"},
                                  {"name": "Linux", "type": "read-only"},
                                  {"name": "beast-unit", "type": "custom"}]}
    assert GH.record_labels(record) == ["self-hosted", "Linux", "beast-unit"]


def test_forgejo_record_labels_keep_only_the_name():
    record = {"id": 3, "labels": ["ubuntu-latest", "ubuntu-22.04:docker://node:20"]}
    assert FJ.record_labels(record) == ["ubuntu-latest", "ubuntu-22.04"]


def test_forgejo_record_labels_as_objects():
    record = {"id": 3, "labels": [{"name": "windows-latest"}]}
    assert FJ.record_labels(record) == ["windows-latest"]


def test_a_record_without_labels_says_nothing():
    assert GH.record_labels({"id": 1}) is None
    assert FJ.record_labels(None) is None


def test_github_workflow_labels_add_what_github_gives_every_runner():
    fleet = {"platform": "linux", "architecture": "x64", "labels": []}
    assert GH.workflow_labels(fleet, {"RUNNER_LABELS": "beast-unit"}) == \
        ["self-hosted", "Linux", "X64", "beast-unit"]
    win = {"platform": "windows", "architecture": "x64", "labels": ["beast-unit"]}
    assert GH.workflow_labels(win, {}) == ["self-hosted", "Windows", "X64", "beast-unit"]


def test_forgejo_workflow_labels_are_the_names_of_the_fleet_labels():
    fleet = {"platform": "linux", "architecture": "x64",
             "labels": ["ubuntu-latest:docker://node:lts", "ubuntu-24.04:docker://node:22"]}
    assert FJ.workflow_labels(fleet, {}) == ["ubuntu-latest", "ubuntu-24.04"]


def test_workflow_labels_never_repeat_a_label():
    fleet = {"platform": "linux", "architecture": "x64",
             "labels": ["self-hosted", "Linux", "X64", "beast-unit"]}
    assert GH.workflow_labels(fleet, {}) == ["self-hosted", "Linux", "X64", "beast-unit"]
```

- [ ] **Step 2: Run it to see it fail.** `cd dashboard && python -m pytest -q -p no:cacheprovider tests/test_workflow_labels.py` - expected: AttributeError `record_labels`.

- [ ] **Step 3: Implement.** In the `Provider` base class, after `record_for`:

```python
    def record_labels(self, record):
        """The label names this record carries, as `runs-on:` takes them, or
        None when the record says nothing about labels - unknown, which is not
        the same as none."""
        if not isinstance(record, dict) or not isinstance(record.get("labels"), list):
            return None
        names = []
        for label in record["labels"]:
            name = label.get("name") if isinstance(label, dict) else label
            name = self._label_name(str(name or ""))
            if name and name not in names:
                names.append(name)
        return names

    def _label_name(self, text):
        return text.strip()

    def workflow_labels(self, spec, env=None):
        """What a workflow can name in `runs-on:` for a runner registered from
        this spec or fleet."""
        text = _labels(spec, self.default_labels(
            (spec or {}).get("platform") or LINUX,
            (spec or {}).get("architecture") or X64, env))
        names = []
        for part in self._automatic(spec) + text.split(","):
            name = self._label_name(part)
            if name and name not in names:
                names.append(name)
        return names

    def _automatic(self, spec):
        return []
```

In `_GitHub`, add:

```python
    #: What GitHub gives every self-hosted runner, in its own spelling.
    _OS_LABEL = {LINUX: "Linux", WINDOWS: "Windows", MACOS: "macOS"}
    _ARCH_LABEL = {X64: "X64", ARM64: "ARM64"}

    def _automatic(self, spec):
        spec = spec or {}
        return ["self-hosted",
                self._OS_LABEL.get(spec.get("platform") or LINUX, ""),
                self._ARCH_LABEL.get(spec.get("architecture") or X64, "")]
```

In `_Forgejo`, add:

```python
    def _label_name(self, text):
        """A Forgejo label is `name`, `name:host` or `name:docker://image`;
        a workflow names only `name`."""
        return text.strip().partition(":")[0].strip()
```

If `ARM64` is not a module constant in `providers.py`, use the constant the module defines for arm64 (grep `ARM64 =`).

- [ ] **Step 4: Run the tests to see them pass**, then the provider suites: `python -m pytest -q -p no:cacheprovider tests/test_workflow_labels.py tests/test_providers.py tests/test_github_platforms.py tests/test_forgejo_platforms.py`.

- [ ] **Step 5: Commit** `git add dashboard/providers.py dashboard/tests/test_workflow_labels.py && git commit -m "feat(providers): the labels a workflow can name, from a record and from a fleet"`

---

### Task 3: W2b - the controller stores the labels the forge lists

**Files:**
- Modify: `dashboard/store/schema.py` (SCHEMA `runner_specs` block near line 97-125, `_migrate` near line 276)
- Modify: `dashboard/store/specs.py` (`JSON_FIELDS` near line 60)
- Modify: `dashboard/control/provision.py` (`__init__` near line 174; `observe` near line 746)
- Modify: `dashboard/control/reconciler.py` (`_record_forge` near line 974)
- Test: `dashboard/tests/test_forge_labels_observed.py` (create)

**Interfaces:**
- Consumes: `Provider.record_labels(record)` (Task 2).
- Produces: columns `runner_specs.forge_labels TEXT` (JSON list or NULL) and `runner_specs.forge_labels_at TEXT`; `SpecStore.get()` returns `forge_labels` decoded as a list or None.

- [ ] **Step 1: Write the failing tests**, using the reconciler world of `tests/test_reconciler.py` (fixture `world`, helpers `converge`, `live`, constant `GH`):

```python
"""What the forge lists as a runner's labels is kept on its spec."""
import sqlite3

from store import schema
from tests.test_reconciler import GH, converge, live, world  # noqa: F401


def test_an_observed_runner_keeps_the_labels_its_forge_lists(world, monkeypatch):
    service, executor, reconciler = world
    service.scale_up(GH)
    converge(service, reconciler)
    spec = live(service)[0]
    executor.world[spec["runner_id"]] = "idle"
    executor.labels_seen = {spec["runner_id"]: ["self-hosted", "Linux", "X64", "beast-unit"]}
    reconciler.pass_once()
    stored = service.specs.get(spec["runner_id"])
    assert stored["forge_labels"] == ["self-hosted", "Linux", "X64", "beast-unit"]
    assert stored["forge_labels_at"]


def test_unknown_labels_leave_what_was_stored(world):
    service, executor, reconciler = world
    service.scale_up(GH)
    converge(service, reconciler)
    spec = live(service)[0]
    executor.world[spec["runner_id"]] = "idle"
    executor.labels_seen = {spec["runner_id"]: ["a"]}
    reconciler.pass_once()
    executor.labels_seen = {spec["runner_id"]: None}
    reconciler.pass_once()
    assert service.specs.get(spec["runner_id"])["forge_labels"] == ["a"]


def test_the_columns_are_added_to_an_existing_store(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript(schema.SCHEMA.replace("  forge_labels     TEXT,\n", "").replace(
        "  forge_labels_at  TEXT,\n", ""))
    c.close()
    schema.init(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(runner_specs)")}
    assert {"forge_labels", "forge_labels_at"} <= cols
```

The test world's executor is the fake in `tests/test_reconciler.py`; extend that fake so that `observe(spec)` does `self.forge_labels[spec["runner_id"]] = self.labels_seen.get(spec["runner_id"])` when `labels_seen` is set (`self.forge_labels = {}` and `self.labels_seen = {}` in its `__init__`). Read the fake first and mirror how it sets `forge_words`.

- [ ] **Step 2: Run to see them fail.** Expected: KeyError / missing column.

- [ ] **Step 3: Implement.**
  - `schema.py`: in SCHEMA's `runner_specs`, after `forge_seen_at    TEXT,` add `  forge_labels     TEXT,\n  forge_labels_at  TEXT,` (same indentation), and in `_migrate` extend the observation loop to `for column in ("telemetry", "forge_state", "forge_seen_at", "forge_labels", "forge_labels_at"):`.
  - `specs.py`: add `"forge_labels"` to `JSON_FIELDS`.
  - `provision.py` `__init__`: `self.forge_labels = {}` beside `self.forge_words = {}`. In `observe`, replace the records line so the records are kept:

```python
        records = self._records(provider, fresh=fresh)
        seen = provider.job_state(spec, records)
        self.forge_words[spec["runner_id"]] = seen
        if records is not None:
            self.forge_labels[spec["runner_id"]] = provider.record_labels(
                provider.record_for(spec, records))
```

  - `reconciler.py` `_record_forge`: after the `forge_state` update, inside the same `with` block:

```python
        labels = getattr(self.executor, "forge_labels", None)
        if isinstance(labels, dict) and spec["runner_id"] in labels:
            names = labels.pop(spec["runner_id"])
            if names is not None:
                import json
                c.execute("UPDATE runner_specs SET forge_labels = ?, forge_labels_at = ?"
                          " WHERE runner_id = ?",
                          (json.dumps(names), _iso(_now()), spec["runner_id"]))
```

  Note the early `return` in `_record_forge` when `forge_words` lacks the runner: move the labels block so it runs before that return, or restructure so both are read first.

- [ ] **Step 4: Run the new tests, then** `tests/test_reconciler.py tests/test_provisioning_flow.py tests/test_spec_store.py tests/test_telemetry_health.py`. All pass.

- [ ] **Step 5: Test the migration on a copy of the live store** (read-only on the live file): copy with the backup API inside `rnr-controller` to `/data/labels-migtest.db`, run `schema.init` on the copy from a throwaway container of the new image built in Task 4, compare row counts per table and every spec's `desired_state, actual_state, cpu_limit, memory_limit, memory_swap_limit, disk_limit, runtime_template, host_id` before and after, then delete the copy. Expected: identical.

- [ ] **Step 6: Commit** `git add dashboard/store dashboard/control/provision.py dashboard/control/reconciler.py dashboard/tests/test_forge_labels_observed.py dashboard/tests/test_reconciler.py && git commit -m "feat(control): keep the labels the forge lists for each runner"`

---

### Task 4: W2c - labels on the card, the detail page and the fleet header

**Files:**
- Modify: `dashboard/cards.py` (`FIELDS` line 31; `from_spec` line 436; the Forgejo-only card near line 264 and the legacy card near line 221 get `labels=None`)
- Modify: `dashboard/api_v2.py` (`fleet_list` near line 257: add `"labels"`)
- Modify: `dashboard/templates/_card.js` (`cardHTML` near line 97, `fleetHeadHTML` near line 128, `FIELDS` list line 17)
- Modify: `dashboard/templates/fleet_v2.html` and `runner_v2.html` only for CSS (`.clabels`, `.chip`, `.cwarn`)
- Test: `dashboard/tests/test_card_labels.py` (create); extend `tests/test_controller_dashboard_browser.py` if it renders cards

**Interfaces:**
- Consumes: spec `forge_labels` (Task 3), `Provider.workflow_labels` (Task 2).
- Produces: card key `labels` (list[str] | None) and `label_drift` (str | None); fleet dict key `labels` (list[str]).

- [ ] **Step 1: Write the failing tests**

```python
"""A card shows the labels its forge lists; a fleet the labels it registers with."""
import cards


def spec(**over):
    s = {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "display_name": "github-linux-x64-1",
         "provider": "github", "platform": "linux", "architecture": "x64",
         "host_id": "rnr-linux-1", "actual_state": "idle", "capabilities": {},
         "forge_labels": ["self-hosted", "Linux", "X64", "beast-unit"]}
    s.update(over)
    return s


def test_every_card_carries_labels():
    assert "labels" in cards.FIELDS


def test_a_card_shows_the_labels_the_forge_lists():
    assert cards.from_spec(spec())["labels"] == ["self-hosted", "Linux", "X64", "beast-unit"]


def test_labels_not_yet_seen_are_unknown_not_empty():
    assert cards.from_spec(spec(forge_labels=None))["labels"] is None


def test_a_registration_drift_note_is_a_warning():
    card = cards.from_spec(spec(last_note="2026-09-20T01:56:14Z registered with other labels than the fleet's: unexpected labels macos-13"))
    assert card["label_drift"].startswith("registered with other labels")
```

And in a Flask-client test (pattern of `tests/test_fleet_routes.py`): `GET /api/v2/fleets` returns each fleet with a `labels` list, `["self-hosted","Linux","X64","beast-unit"]` for github-linux-x64 when `RUNNER_LABELS=beast-unit`.

- [ ] **Step 2: Run to see them fail.**

- [ ] **Step 3: Implement.**
  - `cards.py`: add `"labels"` after `"architecture"` in `FIELDS`. In `from_spec` pass `labels=spec.get("forge_labels")` and `label_drift=_drift(spec.get("last_note"))` with

```python
def _drift(note):
    """The registration-drift sentence of a note, without its timestamp, or
    None. A runner registered with other labels than its fleet asks for
    still works - it takes the wrong jobs - so it is a warning, not an
    error."""
    text = str(note or "")
    at = text.find("registered with other labels")
    return text[at:] if at >= 0 else None
```

  Give the other card builders `labels=None`, `label_drift=None`.
  - `api_v2.py` `fleet_list`: in the fleet dict add
    `"labels": providers.by_key(provider_key).workflow_labels(row, service.env)` (use the fleet row already in scope; when the provider is unknown, `[]`).
  - `_card.js`: rename the worker line to start with `worker `:

```js
  const where = ['worker ' + (c.worker || '?'), c.runtime,
                 [c.platform, c.architecture].filter(Boolean).join('/'),
                 c.registration, c.uptime].filter(Boolean).join(' · ');
  const labels = c.labels == null
    ? `<div class="clabels"><span class="lhead">labels</span> unknown</div>`
    : `<div class="clabels"><span class="lhead">labels</span> ` +
      c.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('') + `</div>`;
  const drift = c.label_drift ? `<div class="cwarn">${esc(c.label_drift)}</div>` : '';
```

    and render `labels` right after the `creg` line and `drift` before `note`. Add `'labels'` to the JS `FIELDS` list. In `fleetHeadHTML`, after the title: `(f.labels && f.labels.length ? `<div class="flabels">runs-on: ${f.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('')}</div>` : '')`.
  - CSS in both templates: `.clabels,.flabels{margin-top:6px;font-size:11px;color:var(--text-faint)} .chip{display:inline-block;border:1px solid var(--border);padding:0 5px;margin:0 4px 2px 0;color:var(--text)} .cwarn{margin-top:6px;font-size:11px;color:#e0a040}` (use the templates' existing variable names; read the `<style>` block first).

- [ ] **Step 4: Run the new tests, then the full dashboard suite.** The grep tests forbidding platform conditionals in templates must stay green.

- [ ] **Step 5: Commit** `git commit -m "feat(page): every card shows the labels a workflow can name, and warns when they drift"`

- [ ] **Step 6: Deploy (announce first).** Build the controller image from `dashboard/`, run Task 3 Step 5 against it, then `bash /tmp/deploy-cp.sh <hash>`. Within two passes every spec has `forge_labels`. Check with the forge APIs that each card's labels equal what GitHub/Forgejo list for that runner, and screenshot the fleet page for the evidence file.

---

### Task 5: W5 - Forgejo Linux memory: evidence and the rule

**Files:** Modify `docs/operations/runner-platform.md` (new subsection 1.3).

- [ ] **Step 1: Re-measure** on the Linux worker for the three Forgejo units: `memory.events` (`max`, `oom_kill`), `memory.peak`, and `anon`/`file` from `memory.stat` (command in the spec's W5 evidence).
- [ ] **Step 2: Write subsection 1.3 "Forgejo Linux memory"**: the measured values with the date, "keep 6 GiB RAM + 6 GiB swap", and the rule "raise when any Forgejo unit shows `oom_kill` > 0 or anonymous memory above 4 GiB in a measured job; any raise must fit the Linux worker's admission of 72 GiB RAM + 640 GiB swap".
- [ ] **Step 3: Commit** `git commit -m "docs(operations): the Forgejo memory limit, what it was measured at, and when to raise it"`

---

### Task 6: W6a - the WSL scripts go

**Files:**
- Delete: `scripts/keepalive-distro.ps1`, `scripts/install-keepalive-task.ps1`, `scripts/publish-dashboard-lan.ps1`, `scripts/provision-distro.ps1`, `infra/fleet/Install-WslAgent.ps1`, `infra/fleet/Publish-WslAgent.ps1`, `docker-compose.yml`
- Modify: headers of `install/*.sh`, `install/*.ps1`, `scripts/start.sh`, `scripts/start-forgejo.sh`, `docker-compose.runners.yml`
- Test: `dashboard/tests/test_wsl_couplings.py` (read it first; update what it asserts about these files)

- [ ] **Step 1: List references before deleting.** `git grep -n -e keepalive-distro -e install-keepalive-task -e publish-dashboard-lan -e provision-distro -e Install-WslAgent -e Publish-WslAgent -e "docker-compose.yml"`. Every hit outside the files being deleted and outside `docs/superpowers/` (history) is updated or removed in this task. If a hit is in running code (not docs), stop and record why before going on.
- [ ] **Step 2: Check the host for live uses.** `Get-ScheduledTask | Where-Object TaskName -match 'WSL|Runner'` (the keepalive task is disabled since 2026-09-21; confirm) and the portproxy rule for the dashboard is the one `infra/hyperv` owns (`netsh interface portproxy show v4tov4`, read-only). Nothing is changed on the host in this task.
- [ ] **Step 3: Delete the files** with `git rm`.
- [ ] **Step 4: Deprecate the rest.** For each install script add at the top, after the shebang, a comment block and a fail-fast:

```sh
# DEPRECATED 2026-09-22. Runners are built and driven by the controller on
# Hyper-V workers; see docs/operations/runner-platform.md. This script
# provisioned the retired WSL fleet and refuses to run.
echo "deprecated: see docs/operations/runner-platform.md" >&2; exit 1
```

  and the PowerShell equivalent (`Write-Error '...'; exit 1`) written with the Write tool. `scripts/start*.sh` and `docker-compose.runners.yml` get only the comment (the unit images do not use them; `git grep` in Step 1 confirms).
- [ ] **Step 5: Run the agent and dashboard suites**; update `test_wsl_couplings.py` so it asserts the scripts are gone or deprecated, not present.
- [ ] **Step 6: Commit** `git commit -m "chore: the WSL scripts go, the installers say where the fleet is now"`

---

### Task 7: W6b - retire a worker

**Files:**
- Modify: `dashboard/control/inventory.py` (add `retire`)
- Modify: `dashboard/control/main.py` (CLI subcommand `retire-worker`)
- Test: `dashboard/tests/test_retire_worker.py` (create)

**Interfaces:** Produces `Inventory.retire(host_id, specs) -> None`, raising `ValueError` when any non-deleted spec names the worker.

- [ ] **Step 1: Failing tests**

```python
import pytest

from control import inventory as inv
from store import schema
from store.specs import SpecStore


def make(tmp_path):
    path = str(tmp_path / "c.db")
    schema.init(path)
    return path, inv.Inventory(path), SpecStore(path)


def test_a_worker_with_no_runners_is_retired(tmp_path):
    path, i, specs = make(tmp_path)
    i.register_worker("wsl-linux-1", inv.HYPERV_LINUX, capabilities={"kind": "linux-container"})
    i.retire("wsl-linux-1", specs)
    assert all(w["host_id"] != "wsl-linux-1" for w in i.list())


def test_a_worker_a_runner_names_is_refused(tmp_path):
    path, i, specs = make(tmp_path)
    i.register_worker("w1", inv.HYPERV_LINUX, capabilities={"kind": "linux-container"})
    specs.create(provider="github", platform="linux", host_id="w1",
                 desired_state="running", actual_state="idle")
    with pytest.raises(ValueError, match="names this worker"):
        i.retire("w1", specs)
```

  (If `Inventory` has no `list()`, use the method it has for all workers - read the class first.)
- [ ] **Step 2: Run, see it fail.**
- [ ] **Step 3: Implement** `retire`: count specs with `host_id == host_id` and `deleted_at IS NULL` and `actual_state != 'absent'`; refuse with `ValueError(f"{n} runner(s) names this worker")`; else `DELETE FROM workers WHERE host_id = ?`. Add `retire-worker <host_id>` to `main.py`'s argument handling, printing what it did, and audit it (`audit.record(db, "retire_worker", "accepted", actor="cli", parameters={"host_id": ...})`).
- [ ] **Step 4: Run the new tests and `tests/test_controller_process.py`.**
- [ ] **Step 5: Commit** `git commit -m "feat(control): retire a worker no runner names"`
- [ ] **Step 6 (after the next controller deploy, announced):** `docker exec rnr-controller python -m control retire-worker wsl-linux-1`; confirm it is gone from `python -m control status`. Delete its pinned certificate files under `/data/control-tls/workers/wsl-linux-1` only after listing them.

---

### Task 8: W6c - the v1 dashboard and the Docker-socket path go

**Files:**
- Modify: `dashboard/app.py` (remove the v1 routes and the WSL collector), `dashboard/control/service.py` (`RUNTIMES` default), `dashboard/Dockerfile` (COPY line)
- Delete: `dashboard/docker_ops.py`, `dashboard/runtime/docker_adapter.py`, `dashboard/external_telemetry.py`, `dashboard/runner_detail.py`, `dashboard/templates/index.html`, `dashboard/templates/runner.html`, `dashboard/templates/settings.html`, and the tests that exist only for them
- Test: the full dashboard suite

**Interfaces:** `/` redirects to `/v2`; `/history`, `/users`, auth routes, `/api/history*`, `/api/v2/*`, `/ws/v2/fleet`, `/v2`, `/runners/<id>` stay.

- [ ] **Step 1: Prove where history comes from.** `grep -n "history\." dashboard/control/*.py dashboard/api_v2.py dashboard/app.py`. The controller-runner history must be written by the controller path (Astra's fix: runs keyed by `runner_id`). If the only writer is inside the v1 collector in `app.py`, move that writer (with its tests) into a v2 module first, as its own commit, before any removal.
- [ ] **Step 2: Map what goes.** In `app.py`, the routes to delete are: `/` (becomes `return redirect("/v2")`), `/settings` (v1), `/ws/fleet`, `/api/status`, `/runner/<name>`, every `/api/runner/<name>/...`, `/api/prune-all`, `/api/runner/<action>`, `/api/runner/add`, `/api/settings`, `/api/recreate`, `/api/control/workers` if only v1 uses it (grep the templates). List every helper each uses; a helper also used by a kept route stays.
- [ ] **Step 3: Switch the service default.** In `control/service.py` set `RUNTIMES = {}` with a comment that the controller and the dashboard always pass `agent_runtime.TABLE`; run the dashboard suite and fix tests that relied on the Docker default by passing their fake runtimes explicitly (the pattern of `tests/test_provisioning_flow.py`'s `ALL_CELLS`).
- [ ] **Step 4: Delete the v1 code and the tests that exercise only it.** Candidate test files (verify each by reading it): `test_docker_adapter.py`, `test_docker_argv_isolation.py`, `test_docker_logs_stream_merge.py`, `test_docker_ops.py`, `test_elsewhere_markup.py`, `test_elsewhere_section.py`, `test_external_telemetry.py`, `test_list_runners.py`, `test_runner_cpu_ceiling.py`, `test_runner_detail.py`, `test_runner_memory.py`, `test_runner_volume_lifecycle.py`, `test_settings_forgejo_fields.py`, `test_settings_recreate_per_fleet.py`, `test_add_button_empty_fleet.py`, `test_orphaned_runs.py` (only if it tests the v1 collector), `test_runtime_contract.py` (only the Docker adapter's case). Files that test v2 or providers and merely import a v1 helper are fixed, not deleted.
- [ ] **Step 5: Dockerfile.** Remove the deleted modules from the `COPY` line and `docker-cli` from `apk add` if nothing uses it (grep `docker` in the kept modules).
- [ ] **Step 6: Run the full dashboard suite and the grep tests.** Then run the app locally against a copy of a store (`DASH_DATA` temp dir) and load `/`, `/v2`, `/history`, `/runners/<id>` with the Flask test client.
- [ ] **Step 7: Commit** `git commit -m "refactor(page): the v1 dashboard and its Docker socket go; / is the fleet page"`
- [ ] **Step 8: Deploy (announce first)** with Task 7 Step 6 in the same window. Check the public URL answers, `/` lands on the fleet page, history loads.

---

### Task 9: W3a - the macOS host installer writes a pool configuration

**Files:**
- Modify: `infra/hyperv/Install-ApplianceHost.ps1` (params and the `$agentConfig` block near line 107) - edit with the Edit tool
- Test: `agent/tests/test_appliance_host_config.py` (create) - validates the JSON the installer would write with the agent's own loader

**Interfaces:** Consumes `agent.config.load` (read `agent/config.py` for the loader's name and the `appliance_pool` keys it accepts: image, base_disk, base_system, data_root, templates, base_guests_disabled, image_uid, image_gid, ssh_port_base, boot_timeout, shutdown_timeout).

- [ ] **Step 1: Failing test.** Build the config dict the installer will write for the pool (the README example with the real values: image `sha256:ab06f62364694bc5c49b41ea9644d42da03f7af2b36d86acec008a725ae6e745`, base `/var/lib/runner-appliances/bases/clean-base-<date>.qcow2` as a parameter, BaseSystem path, data root `/var/lib/runner-appliances/instances`, both template names, `base_guests_disabled: true`, uid/gid 1000, `ssh_port_base` 51000, capacity `max_runners` 2 and `memory_bytes` 21474836480) and assert the agent's loader accepts it and rejects it with `base_guests_disabled: false` or a placeholder image.
- [ ] **Step 2: Run, see the parts that do not exist yet fail** (the installer has no switch; the test fails on the helper that renders it, `infra/hyperv/appliance_pool_config.py`, created in Step 3).
- [ ] **Step 3: Implement.** Create `infra/hyperv/appliance_pool_config.py` with `render(image, base_disk, base_system, data_root, templates, uid=1000, gid=1000, port_base=51000, max_runners=2, memory_bytes=20*1024**3) -> dict` (returns `{"appliance_pool": {...}, "capacity": {...}}`) and a `__main__` that takes the same values as `--image`, `--base-disk`, `--base-system` and prints the JSON. It is the single source of those fields. Add to `Install-ApplianceHost.ps1`: `[switch] $AppliancePool`, `[string] $PoolImage`, `[string] $PoolBaseDisk`, `[string] $PoolBaseSystem`; when `-AppliancePool`, refuse unless `$PoolImage -match '^sha256:[0-9a-f]{64}$'`, run `python infra\hyperv\appliance_pool_config.py --image $PoolImage --base-disk $PoolBaseDisk --base-system $PoolBaseSystem`, `ConvertFrom-Json` the output and merge its `appliance_pool` and `capacity` into `$agentConfig` before it is written. The test calls `render` and feeds the result, merged into a minimal macOS agent config, to the agent's loader.
- [ ] **Step 4: Run the test and `agent/tests/test_macos_pool.py tests/test_appliance_host.py`.**
- [ ] **Step 5: Commit** `git commit -m "feat(infra): the macOS host installer can configure one guest per runner"`

---

### Task 10: W3b - a clean macOS base from the copy

**Files:** evidence only. Live: macOS host VM. Operator's go needed at the time (it boots a guest).

- [ ] **Step 1: Inspect.** On the macOS host: `sudo ls -la /var/lib/runner-appliances/bases/`, `sudo qemu-img info` on `clean-base-20260921.qcow2` (must have no backing file), record its SHA-256 (`sudo sha256sum`, it is ~92 GB - run in background). Confirm `macos-sequoia`'s own disk is not referenced.
- [ ] **Step 2: Boot a throwaway guest** from an overlay: `qemu-img create -f qcow2 -b <copy> -F qcow2 /var/lib/runner-appliances/stage/clean.qcow2`, started with the r3 image (`docker run` with the same mounts the pool uses, `--network` that cannot reach the forges: a Docker network with `--internal`, plus the SSH port published to localhost only). Wait for SSH (timeout 600 s; if it does not answer, read the guest console with the noVNC container or `docker logs`, fix in `images/macos/appliance/`, rebuild, retry).
- [ ] **Step 3: Clean inside the guest** over SSH: stop and remove the old runner's launchd job (`launchctl bootout` and delete its plist from `/Library/LaunchDaemons` or the user agents dir as found), delete the old runner directory's `.runner`, `.credentials*`, adopted marker and caches (paths found with `ls` first, recorded), `launchctl disable` for its label, confirm no runner process (`pgrep -fl runner`), confirm both templates exist under `/Users/runner/templates/` and are executable, then `sudo shutdown -h now`.
- [ ] **Step 4: Flatten** the overlay into a new standalone base: `qemu-img convert -O qcow2 clean.qcow2 /var/lib/runner-appliances/bases/pool-base-20260922.qcow2`, `chmod 0644`, owner root, record SHA-256. Delete the overlay.
- [ ] **Step 5: Record evidence** (commands, what was removed, both hashes) and commit the evidence file.

---

### Task 11: W3c - boot proof of a pool guest

**Files:** evidence only; any appliance fix goes to `images/macos/appliance/` with its own test and commit.

- [ ] **Step 1:** Start one guest the way `MacAppliancePoolRuntime` does (read `agent/runtimes/macos_pool.py` `_boot` for the exact `docker run`), from `pool-base-20260922.qcow2`, 4 vCPU, 8 GiB, on an overlay in a stage dir, with forge access blocked.
- [ ] **Step 2: Prove:** SSH answers with the pool key; `sw_vers` prints macOS; both templates executable; no runner process; `sudo /sbin/shutdown -h now` powers off within 180 s (container exits).
- [ ] **Step 3:** Measure the host's free memory with the guest running (`free -m`). Expected: more than 12 GiB available (room for the second guest).
- [ ] **Step 4:** Delete the stage overlay; record evidence; commit.

---

### Task 12: W3d - migrate the macOS runners onto their own guests

**Files:** `/etc/runner-platform/controller.env` and `dashboard.env` on the control plane (live); evidence. Operator's go at the time (Forgejo macOS is out for a few minutes).

- [ ] **Step 1: Controller settings.** Add `FORGEJO_RUNNER_ARTIFACT_MACOS=forgejo-runner-v13.1.0-macos-r20260921 sha256:<sha of the template's binary>` (the sha from `images/macos/manifest.json`, or measured in the guest) to both env files (backups first), redeploy the same image (env only). Check `svc.fleets.get("forgejo-macos-x64")["available"]` is true and `svc.buildable(...)` true once the pool agent is live (Step 5).
- [ ] **Step 2: Drain and remove the adopted runner.** `svc.act(<beaststack runner_id>, "drain", requested_by=...)`, wait until `drained`; then `svc.set_capacity("forgejo-macos-x64", 0, ...)`; wait until the spec is gone and Forgejo no longer lists runner 4.
- [ ] **Step 3: Stop the old guest** on the macOS host: `sudo docker stop -t 180 macos-sequoia` (its restart policy is already `no`). Its disk is left untouched.
- [ ] **Step 4: Deploy the pool agent**: `.\infra\hyperv\Install-ApplianceHost.ps1 -AppliancePool -PoolImage sha256:ab06f626... -PoolBaseDisk /var/lib/runner-appliances/bases/pool-base-20260922.qcow2 -PoolBaseSystem <path>` (not elevated). Confirm the worker is healthy and its capabilities say `appliance_per_runner: true`, `memory_enforcement: true`.
- [ ] **Step 5: Fleet defaults** for both macOS fleets: `set_defaults(fid, {"cpu_limit": 4, "memory_limit": 8*1024**3})`; Forgejo labels `["macos-15:host","macos-14:host","macos-13:host","macos-latest:host"]`; GitHub labels `["beast-unit"]`, unit template `actions-runner-v2.336.0-macos-r20260921` (already set).
- [ ] **Step 6: Create**, one after the other: `set_capacity("forgejo-macos-x64", 1)`, wait for `idle`; then `set_capacity("github-macos-x64", 1)`, wait for `idle`.
- [ ] **Step 7: Verify** both forges list them with the expected labels, each card shows `resource_enforcement`, and each takes a real job (Task 15's trigger). Record evidence; commit.

**Rollback:** set both macOS fleets to 0; reinstall the agent without `-AppliancePool`; `sudo docker start macos-sequoia`; `python -m control adopt forgejo-macos-x64 beaststack-macos-sequoia macos-appliance-1 org.forgejo.runner`.

---

### Task 13: W4 - merge the Hyper-V checkpoints

**Files:** `D:\tmp\runner-fix-3.ps1` (read-only listing) and `D:\tmp\runner-fix-4.ps1` (removal), written with the Write tool; evidence.

- [ ] **Step 1: Listing script** for the operator to run elevated:

```powershell
$out = 'D:\tmp\runner-fix-3.out.txt'
Start-Transcript -Path $out -Force | Out-Null
foreach ($vm in 'rnr-control','rnr-linux-1','macos-runner') {
    Get-VMSnapshot -VMName $vm | Select-Object VMName, Name, SnapshotType, CreationTime, ParentSnapshotName | Format-Table -AutoSize | Out-Host
    Get-VM $vm | Select-Object Name, CheckpointType, AutomaticCheckpointsEnabled | Format-Table -AutoSize | Out-Host
    Get-VMHardDiskDrive -VMName $vm | ForEach-Object { Get-VHD $_.Path | Select-Object Path, VhdType, ParentPath, FileSize } | Format-Table -AutoSize | Out-Host
}
Stop-Transcript | Out-Null
```

- [ ] **Step 2:** Read the output with the operator; they decide per checkpoint.
- [ ] **Step 3: Removal script**, one VM per run, only the checkpoints named: `Remove-VMSnapshot -VMName <vm> -Name '<name>'`, then poll `Get-VM <vm>` until `Status` no longer says merging and `Get-VMHardDiskDrive` lists `.vhdx`; `Set-VM -Name <vm> -AutomaticCheckpointsEnabled $false`. For `rnr-linux-1` only when no runner is busy (check with the controller first).
- [ ] **Step 4: Verify** no container on `rnr-linux-1` restarted (`StartedAt` unchanged) and the controller is healthy. Record evidence; commit.

---

### Task 14: W7a - regression tests in CI

**Files:**
- Delete: `.github/workflows/build-image.yml` (builds the retired WSL runner image from `dockerfile`)
- Create: `.github/workflows/tests.yml`

- [ ] **Step 1: Check** nothing else uses the image that workflow builds: `git grep -n "nomercy-github-runner\|ghcr.io/nomercy-entertainment/nomercy-github-runner"`; hits only in retired files or docs history.
- [ ] **Step 2: Write the workflow**

```yaml
name: Tests

on:
  push:
    branches: [master]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  tests:
    runs-on: [self-hosted, Linux, X64, beast-unit]
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Dependencies
        run: python -m pip install flask==3.0.3 flask-sock==0.7.0 cryptography==48.0.1 pytest
      - name: Agent tests
        working-directory: agent
        run: python -m pytest -q -p no:cacheprovider
      - name: Dashboard tests
        working-directory: dashboard
        run: python -m pytest -q -p no:cacheprovider
```

- [ ] **Step 3:** If the suites need something the runner image lacks (PowerShell tests skip on Linux; bash is present), fix in the workflow, not by skipping tests.
- [ ] **Step 4: Commit** `git commit -m "ci: the agent and dashboard suites run on every push; the WSL image build goes"`. The workflow runs when the operator pushes; its first run is recorded in Task 15.

---

### Task 15: W7b - the acceptance run and record

**Files:**
- Create: `.github/workflows/acceptance-probe.yml` (a `workflow_dispatch` job per platform label set, doing `echo`, `uname`/`ver` and a 1 MB file write in the workspace)
- Modify: `docs/superpowers/plans/2026-09-17-uniform-acceptance.md` (the table)

- [ ] **Step 1: Probe workflow**

```yaml
name: Acceptance probe

on:
  workflow_dispatch:
    inputs:
      target:
        description: 'linux | windows | macos'
        required: true

permissions:
  contents: read

jobs:
  probe:
    runs-on: ${{ fromJSON(format('["self-hosted","{0}","beast-unit"]', inputs.target == 'linux' && 'Linux' || inputs.target == 'windows' && 'Windows' || 'macOS')) }}
    timeout-minutes: 10
    steps:
      - name: Where am I
        shell: bash
        run: |
          echo "runner: $RUNNER_NAME"; uname -a || ver
          head -c 1048576 /dev/zero > probe.bin && ls -la probe.bin
```

  (On Windows the default `bash` is Git Bash from the runner; if absent, add a `pwsh` step guarded by `runner.os == 'Windows'`.)
- [ ] **Step 2: Forgejo probe.** Ask the operator which Forgejo repository may carry an equivalent `.forgejo/workflows/acceptance-probe.yml`; add it there only with their go.
- [ ] **Step 3: Per cell** (github/forgejo x linux/windows/macos), through `svc` with a note per step: scale up by 1 (new runner idle, forge lists it with the expected labels), trigger the probe (the job lands on a runner of the cell and succeeds), drain + cancel drain, restart, clear cache (record before/after bytes from the operation result), recreate (same labels, limits and cpuset/disk), scale down by 1 (forge record gone, unit and storage gone). Existing runners are never the ones removed: remove the one just added.
- [ ] **Step 4: Write the table** in the acceptance file: ACC row, command, output excerpt, date, pass or not-run with reason.
- [ ] **Step 5: Section 23 checks**: `pytest` for the conformance suite (`agent/tests/contract`), the import-isolation tests and the template grep tests; record the output.
- [ ] **Step 6: Commit** `git commit -m "docs(acceptance): the six cells, run end to end"`

---

## Self-review

- Spec coverage: W1 -> Task 1; W2 -> Tasks 2-4; W3 -> Tasks 9-12; W4 -> Task 13; W5 -> Task 5; W6 -> Tasks 6-8 (the distro itself deliberately excluded, per the spec); W7 -> Tasks 14-15. Order follows the spec: 1, 2-4, 5, 6-8, 9-12, 13, 14-15.
- Names used across tasks: `record_labels`, `workflow_labels` (Task 2) are the ones Tasks 3 and 4 consume; `forge_labels`/`forge_labels_at` (Task 3) are what Task 4 reads; `Inventory.retire` (Task 7) is what the CLI calls.

---

### Task 16: W8 - the Linux unit's root filesystem is writable again

**Files:**
- Modify: `agent/config.py` (the `storage` block's allowed keys and defaults, around lines 260-272)
- Modify: `agent/runtimes/linux_container.py` (create around line 312, `_storage_container` around line 481, `_readonly_image` around line 500, the maintenance helper around line 832)
- Test: `agent/tests/test_linux_storage.py` (add), `agent/tests/test_config.py` (add)

**Interfaces:**
- Produces: agent config `storage.readonly_root` (bool, default True). `LinuxContainerRuntime` passes `--read-only --tmpfs <READONLY_TMPFS>` and enforces the image label and the existing container's read-only root ONLY when it is true.

- [ ] **Step 1: Write the failing tests.** In `agent/tests/test_config.py`, following the file's existing style: `storage` accepts `readonly_root` and defaults it to True; a non-boolean value is a `ConfigError`; `storage may name only root, default_bytes and readonly_root` is the refusal for an unknown key. In `agent/tests/test_linux_storage.py` (read it first for its fake-docker harness): with `readonly_root` false, `create` builds an argv WITHOUT `--read-only` and without the `/run` tmpfs, and an image with no `nomercy.readonly_root` label is accepted; with it true (the default), the argv has `--read-only` and the label is still required; and with it false, `clear_cache`'s maintenance helper argv also has no `--read-only`.

- [ ] **Step 2: Run them and see them fail.** `export PATH="/c/Program Files/Git/bin:$PATH" PYTHONDONTWRITEBYTECODE=1; cd agent && python -m pytest -q -p no:cacheprovider tests/test_config.py tests/test_linux_storage.py`

- [ ] **Step 3: Implement.** `config.py`: allow the key, validate `type(...) is bool`, default True, and carry it in the returned storage mapping. `linux_container.py`: read it once into the runtime (e.g. `self._readonly_root = bool((storage or {}).get("readonly_root", True))` where the storage backend is set up), and guard the four places named above with it. Keep every other check (owned volumes, their identity, the filesystem) unchanged.

- [ ] **Step 4: Run the new tests, then the whole agent suite** (`python -m pytest -q -p no:cacheprovider`, was 771 passed / 6 skipped).

- [ ] **Step 5: Commit**

```bash
git add agent/config.py agent/runtimes/linux_container.py agent/tests/test_config.py agent/tests/test_linux_storage.py
git commit -m "feat(agent): a worker can run its Linux units with a writable root"
```

- [ ] **Step 6 (controller session): deploy and recreate.** Set `"readonly_root": false` in the Linux worker's `/etc/runner-agent/agent.json` storage block, deploy the agent to `/opt/runner-agent/agent`, restart `runner-agent` (runners keep running), then recreate all thirteen Linux runners through the controller, one at a time. Verify `ReadonlyRootfs: false` with cpuset, memory, swap and volumes unchanged, and re-run the `nomercy-docs` workflow that failed.

---

### Task 17: W2d - a drift warning is shown once, not twice

**Files:**
- Modify: `dashboard/templates/_card.js` (`cardHTML`, the `note` line around line 123)
- Test: `dashboard/tests/test_generic_card.py` (`TestTheRenderer`)

**Why:** the registration-drift sentence lives in `last_note`, and Task 4 added
`label_drift` derived from it. A card with drift now shows the same sentence
twice - once as the amber warning, once as the grey note (seen live on
`beaststack-macos-sequoia`, 2026-09-22).

**Interfaces:** consumes the card keys `last_note` and `label_drift` from Task 4.

- [ ] **Step 1: Write the failing test** in `TestTheRenderer`, in the file's style: a card whose `last_note` contains the drift sentence and whose `label_drift` is set renders the sentence exactly once, in the `.cwarn` line, and no `.cnote` line; a card with a `last_note` that is not drift (and `label_drift` null) still renders the `.cnote` line as before.
- [ ] **Step 2: Run it and see it fail.**
- [ ] **Step 3: Implement**: render the note only when there is no `label_drift` - `const note = (c.last_note && !c.label_drift) ? ... : '';`
- [ ] **Step 4: Run the renderer tests, then the full dashboard suite.**
- [ ] **Step 5: Commit** `git commit -m "fix(page): a label-drift warning is shown once, not twice"`

---

### Task 18: W9 - the Windows heartbeat keeps up with the page

**Files:**
- Modify: `agent/runtimes/windows_process.py` (`telemetry`, around lines 454-480; and the deep/storage path the heartbeat calls)
- Test: `agent/tests/test_windows_storage.py` or `agent/tests/test_windows_runtime.py` (whichever holds the telemetry tests - read both)

**Why (measured 2026-09-22):** the page reads a unit's telemetry as unknown
when it is older than 30 s (`cards.HEARTBEAT_FRESH`), and counts a runner as
running only when it is reachable, which needs the same freshness. Windows
telemetry arrived every 26-39 s, so both Windows cards flapped between their
values and "unknown / not reachable", and the header counted 14 of 16.
The agent sends every 10 s, but measures on its own loop, and on Windows
each unit's light telemetry calls the storage helper - a PowerShell process
that waits up to 8 s for the host-wide storage lock. Two runners put the
measurement past the page's freshness window. Linux measures its storage
only on the deep probe (every thirtieth beat) and stays fresh.

**Interfaces:** `WindowsProcessRuntime.telemetry(runner_id)` keeps its shape
(the same keys); what moves is where the disk figures come from.

- [ ] **Step 1: Write the failing test.** With a storage backend configured,
  `telemetry(rid)` does NOT call the storage helper (assert on the fake's
  recorded calls) and returns `disk_*` keys as None/absent, while the deep
  probe the heartbeat uses (`storage_bytes`/`disk_*` - read `agent/heartbeat.py`'s
  `_depth` to see exactly which method it calls) still reports them from the
  storage backend. Follow the fakes already in the Windows tests.
- [ ] **Step 2: Run it and see it fail.**
- [ ] **Step 3: Implement:** move the `self._storage.verify(...)` block out of
  `telemetry` into the method the deep probe calls (add one if the deep probe
  goes through a different entry point), so a light beat costs no PowerShell.
  Keep the values and their names identical where they are reported.
- [ ] **Step 4: Run the Windows tests, then the whole agent suite.**
- [ ] **Step 5: Commit** `git commit -m "fix(agent): a Windows beat does not wait for a disk check"`
- [ ] **Step 6 (controller session):** deploy the agent (the operator runs the elevated installer), then confirm on the page: both Windows cards show CPU, memory and storage, say "reachable", and the header counts 16.

---

### Task 19: W9b - reading a job name does not wait for a disk check either

**Files:**
- Modify: `agent/runtimes/windows_process.py` (`jobs` line ~494, `logs` line ~497)
- Test: `agent/tests/test_windows_storage.py`

**Why (measured 2026-09-22, after Task 18):** the Windows telemetry age still
crossed the page's 30 s freshness window (10-34 s cycles). What remains is
`jobs()`, which runs on every light beat and calls `logs()`, and `logs()`
verifies the runner's volume through the storage helper - the same PowerShell
process with the host-wide lock that Task 18 took out of `telemetry()`. With
two runners that is two more helper runs per beat.

**Interfaces:** `logs(runner_id, since_seconds, max_bytes)` keeps verifying -
it is what an operator's log request goes through. `jobs(runner_ids)` reads
the same file without the verify.

- [ ] **Step 1: Write the failing test.** With a storage backend configured:
  `jobs([rid])` records no call on the fake storage backend (assert on its
  events list) and still returns the job name parsed from the log; `logs(rid, ...)`
  still calls `verify` exactly once. Follow the fakes already in that file.
- [ ] **Step 2: Run it and see it fail.**
- [ ] **Step 3: Implement**: give the class a private reader (say `_tail_log(runner_id, since_seconds, max_bytes)`) holding what `logs` does after the verify, have `logs` verify and then call it, and have `jobs` call it directly. Nothing else changes.
- [ ] **Step 4: Run the Windows tests, then the whole agent suite.**
- [ ] **Step 5: Commit** `git commit -m "fix(agent): reading a job name does not wait for a disk check"`
- [ ] **Step 6 (controller session):** deploy and confirm the telemetry age stays under 30 s and both Windows cards read reachable.

---

### Task 20: the fleet page is the root page

**Files:**
- Modify: `dashboard/app.py` (`/` around line 641, `/v2` around line 813)
- Modify: `dashboard/templates/fleet_v2.html`, `history.html`, `runner_v2.html`, `settings_v2.html`, `users.html` (the nav links that point at `/v2`)
- Test: the tests that assert the redirect (grep `"/v2"` under `dashboard/tests/`)

**Why:** the operator asked for the fleet page at `/` itself, not a redirect to `/v2`.

- [ ] **Step 1: Find every place that depends on the current behaviour.** `git grep -n '"/v2"' dashboard | cat` and the tests that assert a 302 from `/`.
- [ ] **Step 2: Write the failing tests:** `GET /` returns 200 and renders the fleet page (assert on a marker of that template, not just the status), and `GET /v2` still answers for an old link - a 302 to `/` is the required behaviour, so assert that.
- [ ] **Step 3: Run them and see them fail.**
- [ ] **Step 4: Implement:** `/` renders what `/v2` rendered; `/v2` redirects to `/`. Keep the same auth guard and role handling on both. Update the nav links in the five templates to `/`.
- [ ] **Step 5: Run the full dashboard suite** (baseline 1718 passed / 3 skipped / 3 xfailed).
- [ ] **Step 6: Commit** `git commit -m "feat(page): the fleet page is the root page"`
- [ ] **Step 7 (controller session):** deploy and confirm `/` renders the fleet for a signed-in browser and `/v2` still lands somewhere sensible.

---

### Task 21: W10a - a Windows runner gets its own window of cores

**Files:**
- Modify: `dashboard/control/service.py` (`PINNED_CPU_PLATFORMS`, `_host_cores`)
- Test: `dashboard/tests/test_cpu_windows.py`

**Why:** the Windows runners are held to a memory ceiling and their own Job
Object, but nothing bounds their CPU: the card reads "0.0 / 56 cores" and a
build can take the whole machine, which is what the Linux cpusets exist to
prevent. The Windows agent already supports both a CPU rate cap (`cpus`) and
an affinity mask (`cpuset`) through the Job Object
(`agent/jobhost.py:cpu_rate`, `affinity_mask`), and `agent_runtime.unit_spec`
already sends a window as `cpuset` - only the controller refuses to allocate
one for a Windows fleet.

**Interfaces:** unchanged - a whole-number fleet `cpu_limit` becomes a
staggered cores window on the platforms that pin.

- [ ] **Step 1: Write the failing tests** in `dashboard/tests/test_cpu_windows.py`, in its style: a whole-number `cpu_limit` on a WINDOWS fleet gives each planned runner its own window of that width, windows on the same worker overlap as little as possible, a recreate keeps a window of the right width, and a fraction is still a quota. Note the affinity limit the agent has: a window may not name a processor at or beyond 64 (`agent/jobhost.py:affinity_mask` refuses it), so with a host of 56 the wrap-around form ("47-55,0-6") must still be produced, which the existing `cpusets.window` already does.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement**: add `providers.WINDOWS` to `PINNED_CPU_PLATFORMS`, and make `_host_cores` read the core count for the platforms that pin (the Linux worker declares `host_cores` in its capabilities; the Windows worker does not yet, so the telemetry fallback must cover it - check what `WindowsProcessRuntime.telemetry` reports as `host_cores`).
- [ ] **Step 4: Run the new tests, then the full dashboard suite** (baseline 1720 passed / 3 skipped / 3 xfailed).
- [ ] **Step 5: Commit** `git commit -m "feat(control): a Windows runner gets its own window of cores"`
- [ ] **Step 6 (controller session):** deploy, set `cpu_limit` 16 on both Windows fleets, recreate both Windows runners, and check the card reads 16 cores and the Job Object carries the affinity.

---

### Task 22: W10b-1 - the Windows guest exists and answers

**Files:**
- Modify: `infra/hyperv/settings.psd1` (a `rnr-windows-1` entry beside the others, and the memory budget line, which still describes the pre-migration sizes)
- Create: `infra/hyperv/New-WindowsGuest.ps1` (elevated: makes the VM, its two disks and its adapters, attaches the install ISO, starts it)
- Create: `infra/hyperv/Initialize-WindowsGuest.ps1` (elevated: configures the installed guest over PowerShell Direct - no network needed - with its static address, OpenSSH, and the settings a runner host needs)
- Evidence: `docs/superpowers/plans/2026-09-17-uniform-evidence.md`

**Why this shape.** Windows Setup takes an answer file only from removable
media, and building that ISO here would need the WSL distro this work is
retiring. The operator is at the console anyway to enter the licence key, so
Setup is done by hand once; everything after it is scripted through
PowerShell Direct, which reaches a Windows guest without any network.

**Interfaces:** produces the VM `rnr-windows-1`, reachable over SSH at
10.77.0.30 with an administrator account; Task 23 installs the agent in it.

- [ ] **Step 1: Read** `infra/hyperv/New-RunnerPlatformVMs.ps1`, `infra/hyperv/lib.ps1` and `infra/hyperv/settings.psd1`, and follow their conventions exactly: `Test-Elevated`, `ShouldProcess`, `Get-CommitHeadroomGB` before reserving memory, paths under `$s.Root`, the `rnr-internal` switch with a static address, a second adapter on the uplink switch, static MAC addresses, `AutomaticStartAction Start`, `AutomaticStopAction ShutDown`.
- [ ] **Step 2: settings.psd1.** Add:

```
        'rnr-windows-1' = @{
            Role      = 'windows-worker'
            MemoryGB  = 16
            Cpus      = 8
            DiskGB    = 200
            DataDiskGB = 200
            Address   = '10.77.0.30'
            MgmtMac   = '00155D771E0A'
            UplinkMac = '00155D771E0B'
            MaxRunners  = 2
            RunnerMemGB = 8
        }
```

  and correct the memory budget: `rnr-linux-1` is 80 GB and 56 vCPU live since the move (settings still says 16/8), so `VmBudgetGB` becomes 110 with a comment naming the measurement it is based on - host commit 169 of 256 GB used on 2026-09-23, 87 GB free, and `CommitReserveGB` still enforced per VM.
- [ ] **Step 3: New-WindowsGuest.ps1.** Parameters: `-Iso <path>` (required, must exist), `-Name rnr-windows-1`. Refuses unelevated; refuses if the VM exists; checks commit headroom the way the other script does. Creates the VM (Generation 2, `-NoVHD`), a dynamic system VHDX of `DiskGB` and a second dynamic VHDX of `DataDiskGB` under `$s.Rootms\<name>`, both adapters with their MACs, static memory, `Set-VMProcessor -Count`, `Enable-VMTPM` after `Set-VMKeyProtector -NewLocalKeyProtector` (Windows 11 requires a TPM), Secure Boot on with the `MicrosoftWindows` template, the ISO in a DVD drive as first boot device, `Set-VM` for the automatic start and stop actions, then starts it and prints: how to open the console (`vmconnect.exe localhost rnr-windows-1`), that the operator installs Windows 11 Pro and enters their licence, that the local administrator must be called `rnr-admin`, and what to run next.
- [ ] **Step 4: Initialize-WindowsGuest.ps1.** Parameters: `-Name rnr-windows-1`, `-Credential` (the guest's administrator). Over `Invoke-Command -VMName` (PowerShell Direct): set the mgmt adapter's static address from settings (identify it by its MAC), set the DNS servers from settings, install and start `OpenSSH.Server` and set its firewall rule, set the timezone to the host's, turn off sleep/hibernate, enable RDP, and report back the guest's `Get-ComputerInfo` essentials (edition, version, activation status is NOT read or recorded here). Idempotent: run it again and it changes nothing.
- [ ] **Step 5:** Syntax-check both scripts (`[System.Management.Automation.Language.Parser]::ParseFile`) and `Import-PowerShellDataFile` the settings file.
- [ ] **Step 6: Commit** `git commit -m "feat(infra): a Windows guest for the Windows runners"`
- [ ] **Step 7 (operator, elevated):** run `New-WindowsGuest.ps1 -Iso <path>`, install Windows at the console, enter the licence and activate, create `rnr-admin`; then the session runs `Initialize-WindowsGuest.ps1` and verifies SSH from the control plane.

### Task 23: W10b-2 - the guest becomes the Windows worker and takes the runners over

**What the live run of Task 22 established.** `Install-WindowsWorker.ps1` was
written for the physical host and cannot run inside a guest as it stands:

- it refuses unless `10.77.0.1` (the host's own rnr-internal address) is on
  the machine, and the guest has `10.77.0.30`;
- it enrols by SSH-ing into the control plane with the platform key under
  `D:\HyperVunner-platform\ssh`, which does not exist in the guest;
- its NSSM and Forgejo-runner sources are host paths
  (`C:orgejo-runner
ssm.exe`, `D:\HyperVunner-platformrtefacts\...`);
- it builds the agent with `git archive` from a clone, which the guest has not
  got either.

So the host stays the orchestrator - the same shape the Linux workers already
use, where `Initialize-RunnerPlatform.ps1` enrols and pushes the bundle - and
the installer becomes able to run on the machine it is installing.

**Files:**
- Modify: `infra/hyperv/Install-WindowsWorker.ps1` (parameters, so it can install a worker that is not this host)
- Create: `infra/hyperv/Install-WindowsGuestWorker.ps1` (host side: carry everything into the guest over PowerShell Direct, then run the installer there)
- Modify: `infra/hyperv/settings.psd1` (the guest's worker block: host id, capacity, the storage root inside the guest)
- Modify: `docs/operations/runner-platform.md` (section 1, once the runners serve from the guest)
- Evidence: `docs/superpowers/plans/2026-09-17-uniform-evidence.md`

**Interfaces:** produces the worker `rnr-windows-1` reporting healthy to the
controller, with the two Windows fleets placed on it.

- [ ] **Step 1: Read** `Install-WindowsWorker.ps1` whole, plus `Initialize-RunnerPlatform.ps1`'s enrolment block (the `control enrol` / `docker cp` / `tar` sequence) and `lib.ps1`'s `Invoke-Guest` / `Receive-FromGuest`.
- [ ] **Step 2: Parameterise the installer.** Add `-HostId` (default `$s.Windows.HostId`), `-ListenAddress` (default `$s.HostAddress`), `-TlsBundle <path>` (when given, import this tar instead of enrolling over SSH), `-NssmSource` and `-RunnerBinary` (default to the settings values). The address check becomes: the listen address must be on this machine, whichever it is, with the message naming what it looked for. Everything else - Python, icacls, the firewall rule, the service, the health check - stays as it is, except that the health check needs the control plane, so skip it with a printed note when `-TlsBundle` is used and the control plane cannot be reached from here.
- [ ] **Step 3: Write the host-side script.** `Install-WindowsGuestWorker.ps1 -Name rnr-windows-1 -Credential <guest admin>`: refuses unelevated; opens one `New-PSSession -VMName`; creates the target directories in the guest; copies with `Copy-Item -ToSession` the repository archive (made here with `git archive HEAD`), the pinned NSSM, the Forgejo runner binary and the Windows templates; enrols the guest's host id with the controller from here and copies the TLS bundle in; then runs the installer inside the guest with `-HostId`, `-ListenAddress` and `-TlsBundle`; finally waits for `control status` to show the worker healthy. Idempotent: running it again re-copies and re-deploys, exactly as the host installer already does with `app.previous`.
- [ ] **Step 4: Settings.** Give `rnr-windows-1` its worker block - host id `rnr-windows-1`, `MaxRunners` 2, `RunnerMemGB` 8, the storage root on the guest's data disk (`D:unner-disks`, which is the second VHDX) - beside the existing `Windows` block, which keeps describing the physical host until it is retired.
- [ ] **Step 5 (operator, elevated):** run it. Confirm the worker is healthy in `control status`, that its capabilities name the templates and `disk_quota`, and that the dashboard shows it.
- [ ] **Step 6: Move the Forgejo Windows runner.** Raise that fleet's capacity to 2 so a runner is created on the new worker beside the old one, wait until it is idle at the forge, and let a real job run on it.
- [ ] **Step 7:** Capacity back to 1 and confirm the reconciler drains the host's runner, not the guest's, before letting it run.
- [ ] **Step 8:** The same for the GitHub Windows fleet.
- [ ] **Step 9:** When both fleets serve from the guest and each has run a real job, remove the host's agent: stop and delete the `rnr-agent` service on BEAST-UNIT, keep `C:\ProgramData
omercy` until the stability period ends, and retire the worker with `python -m control retire-worker beast-unit`.
- [ ] **Step 10: Record** the evidence and update `docs/operations/runner-platform.md` section 1: every runner of the platform now runs in a Hyper-V guest.

### Task 24: runner storage without a hypervisor under it

**What the live run established.** Raising the Forgejo Windows fleet to 2
placed a runner on `rnr-windows-1` and it failed at once:

```
provision: create_unit: 500: RuntimeError: runner storage:
The term 'New-VHD' is not recognized as the name of a cmdlet
```

`agent/runtimes/windows_storage.ps1` creates each runner's fixed VHDX with
`New-VHD` and attaches it with `Mount-VHD`, and those come from the Hyper-V
module: present on the physical host because it is the Hyper-V host, absent
in every guest. Per-runner disk quotas are what the design asked for, so the
answer is not to drop them but to stop depending on a hypervisor being
installed under the runner.

**Files:**
- Modify: `agent/runtimes/windows_storage.ps1`
- Modify: `agent/tests/test_windows_storage.py`
- Modify: `docs/windows-runner-storage.md`

**Interfaces:** unchanged - the helper keeps its verbs, its JSON output and
its exit codes; only how it makes and attaches an image changes.

- [ ] **Step 1: Read** `agent/runtimes/windows_storage.ps1` whole, plus its tests and `docs/windows-runner-storage.md`, and list every Hyper-V cmdlet it uses and what each one is for.
- [ ] **Step 2: Replace them with the storage stack every Windows carries.** Create the image with `diskpart` (`create vdisk file=<path> maximum=<MB> type=fixed`), attach with `Mount-DiskImage -ImagePath <path> -NoDriveLetter`, detach with `Dismount-DiskImage -ImagePath <path>`, and read its state and sizes with `Get-DiskImage`. No fallback to the Hyper-V cmdlets: one path, working in both places, is worth more than two paths of which one is rarely exercised.
- [ ] **Step 3: Keep the behaviour identical** - the same mutex, the same lock waits, the same verify step, the same JSON, the same messages where they still apply. A caller must not be able to tell which cmdlets did the work.
- [ ] **Step 4: Tests.** Update the existing tests for the new cmdlets and add one that fails if any Hyper-V cmdlet name (`New-VHD`, `Get-VHD`, `Mount-VHD`, `Dismount-VHD`, `Optimize-VHD`) appears in the helper at all - that is the regression this task exists to prevent.
- [ ] **Step 5:** Run the agent's test suite in full and report the counts.
- [ ] **Step 6: Commit.**
- [ ] **Step 7 (live, after review):** redeploy the agent into the guest, raise the fleet to 2, and confirm the runner is created with its own disk under `D:\runner-disks`.

### Task 25: a runner's CPU window is cut from its own host

**What the live run established.** With the storage fix in, the runner
placed on `rnr-windows-1` was pinned to `32-47` - sixteen cores that do not
exist in an eight-processor guest - and its job host died at once:

```
provision: create_unit: 500: RuntimeError: rnr-<id>:
Unexpected status SERVICE_STOPPED in response to START control
```

Two faults behind one symptom:

1. No Windows worker declares `host_cores` in its capabilities (only the
   Linux one does; `rnr-linux-1` says 56, `beast-unit` and `rnr-windows-1`
   say nothing), so the controller fell back to what the *runners* last
   measured - and those runners sit on the physical host, which has 56.
2. `_host_cores` is scoped to a platform, not to a host. Two Windows
   workers with different processor counts are pooled with `min()`, which
   is wrong in both directions: it can cut the big host's windows down to
   the small guest's size, or - as here, since the guest declares nothing -
   hand the small guest a window off the big host's numbering.

**Files:**
- Modify: `agent/runtimes/windows_process.py` (declare `host_cores` in capabilities)
- Modify: `dashboard/control/service.py` (`_host_cores`, `_cpu_window`)
- Modify: `dashboard/control/cpusets.py` if `allocate` does not already refuse a width wider than the host
- Modify: `agent/tests/test_windows_runtime.py`, `dashboard/tests/` (whichever cover capabilities and windows)
- Modify: `infra/hyperv/settings.psd1` (`rnr-windows-1` gets 16 vCPU, so a runner there has a real sixteen-core window like the ones it replaces)

**Interfaces:** `_host_cores(platform, host_id)` - the count for one host, from that worker's own declaration, else from the telemetry of specs placed on it.

- [ ] **Step 1: Read** `dashboard/control/service.py`'s `_pinned_width`, `_host_cores`, `_cpu_window`, `dashboard/control/cpusets.py`, and `agent/runtimes/windows_process.py`'s capabilities and telemetry.
- [ ] **Step 2: Write the failing tests first.** A worker with 8 cores and a worker with 56 of the same platform, each with a runner: the 8-core host's window must fall inside 0-7, the 56-core host's must keep its own numbering, and neither may be cut by the other's count. A window wider than the host's cores must be refused with a message naming both numbers, never silently narrowed.
- [ ] **Step 3: Make them pass.** `host_id` becomes part of how the count is found: the worker's declared `host_cores` first, then the telemetry of specs on that host, then a refusal naming the host.
- [ ] **Step 4:** Declare `host_cores` in the Windows runtime's capabilities, the way the Linux runtime does, with a test.
- [ ] **Step 5:** Give `rnr-windows-1` 16 vCPU in `settings.psd1`, and say in the comment why: its runners are pinned sixteen wide, as the ones on the physical host are.
- [ ] **Step 6:** Run both suites in full and report the counts.
- [ ] **Step 7: Commit.**
- [ ] **Step 8 (live, after review):** deploy the controller, resize the guest, redeploy its agent, raise the fleet to 2, and confirm the runner comes up pinned inside its own guest's cores.
