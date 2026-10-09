"""The job-started hook's first check: whose code is this job about to run?

The runner group lets every repository in the org use these runners, public
ones included, and 26 public repositories do. Until now GitHub's "Approve and
run" click was all that stood between a stranger's fork and a self-hosted
runner. `runner_guard.py` trusts the source, not the person: a pull request
runs only when its head repository is owned by the org or by an account in
RUNNER_TRUSTED_OWNERS; anything else is refused, and the job ended, before
any step - checkout included - has run.

It refuses only what it has positively identified. An event it cannot read,
or a fault of its own, lets the job run with a warning: a bug here must never
stop every job on the fleet.

The cases are shared with the Windows and macOS hooks (origin_cases.py).
"""
import importlib.util
from pathlib import Path

import pytest

from . import origin_cases

GUARD = Path(__file__).parents[2] / "images/linux/unit/runner/runner_guard.py"
spec = importlib.util.spec_from_file_location("linux_runner_guard", GUARD)
runner_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_guard)


@pytest.mark.parametrize("case", origin_cases.CASES, ids=lambda c: c["id"])
def test_every_case(case, tmp_path, capsys):
    ended = []
    env = origin_cases.hook_env(case, tmp_path)
    env["RUNNER_LOG_DIR"] = str(tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    code = runner_guard.check(env, end=ended.append)
    last = origin_cases.read_last(tmp_path / "logs")
    assert last["result"] == origin_cases.last_result(case)
    assert last["version"] == runner_guard.GUARD_VERSION and last["at"].endswith("Z")
    assert bool(ended) == (not case["allowed"]), "a refusal, and only a refusal, ends the job"
    out = capsys.readouterr().out
    assert code == (0 if case["allowed"] else runner_guard.REFUSE), out
    for line in case["out"]:
        assert line in out, out
    for line in case["absent"]:
        assert line not in out, out


def test_the_refusal_is_the_disk_refusals_status():
    """75, which job-started.sh turns into the hook's exit 1."""
    assert runner_guard.REFUSE == 75


def test_an_allowed_job_gets_one_line(tmp_path, capsys):
    case = origin_cases.by_id("push")
    runner_guard.check(origin_cases.hook_env(case, tmp_path))
    assert capsys.readouterr().out.splitlines() == [
        f"Origin: push from {origin_cases.ORG}/app by bob — allowed"]


def test_a_fault_in_the_guard_lets_the_job_run(tmp_path, capsys, monkeypatch):
    def boom(*a, **k):
        raise KeyError("surprise")
    monkeypatch.setattr(runner_guard, "decide", boom)
    case = origin_cases.by_id("fork-by-outsider")
    assert runner_guard.check(origin_cases.hook_env(case, tmp_path)) == 0
    out = capsys.readouterr().out
    assert "::warning title=Runner guard::" in out and "origin not checked" in out
    assert "::error" not in out


def test_an_unreadable_event_file_lets_the_job_run(tmp_path, capsys):
    env = {"GITHUB_EVENT_NAME": "pull_request",
           "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")}
    assert runner_guard.check(env) == 0
    assert origin_cases.UNREAD in capsys.readouterr().out


def test_what_the_payload_says_cannot_add_a_workflow_command(tmp_path, capsys):
    """A login or repository name is printed; one that is not what GitHub
    allows in them is printed with its other characters replaced, so it can
    never end the line or start a `::` command of its own."""
    payload = origin_cases.pull_request(author="mal\n::warning::x",
                                        head="evil/app\n::error::y")
    case = dict(origin_cases.by_id("fork-by-outsider"), payload=payload)
    assert runner_guard.check(origin_cases.hook_env(case, tmp_path),
                              end=lambda env: True) == runner_guard.REFUSE
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and out.count("::") == 2


def test_trusted_owners_are_read_without_case_split_on_commas_or_spaces():
    assert runner_guard.trusted_owners(" Alice,,BOB , carol dave") == {
        "alice", "bob", "carol", "dave"}
    assert runner_guard.trusted_owners("") == set()
    assert runner_guard.trusted_owners(None) == set()


def test_the_guard_says_which_version_it_is():
    """What a unit reports, so the dashboard can tell a runner whose hook
    carries the guard from one made before it."""
    assert isinstance(runner_guard.GUARD_VERSION, int) and runner_guard.GUARD_VERSION >= 1


def test_it_needs_nothing_but_the_standard_library():
    import ast
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"ctypes", "json", "os", "re", "signal", "sys", "time"}, imported


class TestTheLastResult:
    """What the agent reads to tell a hook that has run from one that never
    did, and one that could not read its event (agent/origin_guard.py)."""

    def test_it_is_written_before_the_job_is_ended(self, tmp_path):
        seen = []
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        env["RUNNER_LOG_DIR"] = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir()
        runner_guard.check(env, end=lambda e: seen.append(origin_cases.read_last(tmp_path / "logs")))
        assert seen[0]["result"] == "refused"

    def test_no_log_directory_writes_nothing_and_still_answers(self, tmp_path):
        env = origin_cases.hook_env(origin_cases.by_id("push"), tmp_path)
        env["RUNNER_LOG_DIR"] = str(tmp_path / "missing" / "deeper")
        assert runner_guard.check(env) == 0

    def test_a_fault_in_the_guard_is_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_guard, "decide", lambda *a: 1 / 0)
        env = origin_cases.hook_env(origin_cases.by_id("push"), tmp_path)
        env["RUNNER_LOG_DIR"] = str(tmp_path)
        assert runner_guard.check(env) == 0
        assert origin_cases.read_last(tmp_path)["result"] == "failed"


def test_the_file_is_ascii_so_any_locale_reads_it():
    GUARD.read_bytes().decode("ascii")


# ---- a refusal ends the job ---------------------------------------------------
#
# A failed job-started hook is only a failed step: the runner still runs every
# later step whose `if:` is always(), failure() or !cancelled() - and a fork
# writes its own workflow file. So after the ::error the guard kills
# Runner.Worker, the process running this one job, found among the hook's own
# parents. Runner.Listener, which takes the next job, is never touched.

def fake_proc(root, table):
    """A /proc with `table`: pid -> (ppid, comm, argv as one string)."""
    for pid, (ppid, comm, argv) in table.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "status").write_text(f"Name:\t{comm}\nState:\tS\nPid:\t{pid}\nPPid:\t{ppid}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv.split()))
    return root


RUNNER = {500: (400, "python3", "python3 /runner/job_started.py"),
          400: (300, "timeout", "timeout --kill-after=5s 600s python3 /runner/job_started.py"),
          300: (200, "bash", "bash -e -o pipefail /runner/job-started.sh"),
          200: (100, "Runner.Worker", "/runner/reg/actions-runner/bin/Runner.Worker"),
          100: (50, "Runner.Listener", "/runner/reg/actions-runner/bin/Runner.Listener"),
          50: (1, "bash", "/runner/run")}


class TestFindingTheWorker:
    def test_the_worker_among_the_hooks_parents(self, tmp_path):
        proc = fake_proc(tmp_path / "proc", RUNNER)
        chain = runner_guard.linux_ancestors(500, proc=str(proc))
        assert [entry[0] for entry in chain] == [400, 300, 200, 100, 50]
        assert runner_guard.find_worker(chain) == 200

    def test_the_listener_is_never_the_worker(self, tmp_path):
        table = {k: v for k, v in RUNNER.items() if k != 200}
        table[300] = (100, "bash", "bash -e /runner/job-started.sh")
        proc = fake_proc(tmp_path / "proc", table)
        assert runner_guard.find_worker(runner_guard.linux_ancestors(500, proc=str(proc))) is None

    def test_a_worker_started_under_another_name_is_found_by_its_argv0(self, tmp_path):
        table = {**RUNNER, 200: (100, "dotnet-host", "/x/bin/Runner.Worker --x")}
        proc = fake_proc(tmp_path / "proc", table)
        assert runner_guard.find_worker(runner_guard.linux_ancestors(500, proc=str(proc))) == 200

    @pytest.mark.parametrize("name", ["Runner.Worker.dll", "Runner.WorkerX", "XRunner.Worker",
                                      "runner-worker", "Runner.Listener"])
    def test_only_that_exact_name(self, name):
        hook = (8, ["node.exe"], [])
        assert runner_guard.find_worker([hook, (7, [name], [])]) is None

    def test_the_windows_name_without_case(self):
        chain = [(9, ["python.exe"], []), (8, ["node.exe"], []), (7, ["RUNNER.WORKER.EXE"], [])]
        assert runner_guard.find_worker(chain) == 7

    def test_a_hook_run_by_hand_inside_a_job_ends_nothing(self, tmp_path):
        """A test of the hook run in a CI job: the hook's parent is pytest,
        not Runner.Worker, though Runner.Worker is further up. Killing it
        would end the job that is testing the guard."""
        table = {500: (400, "python3", "python3 job_started.py"),
                 400: (300, "timeout", "timeout 600s python3 job_started.py"),
                 300: (250, "bash", "bash -e -o pipefail /tmp/x/job-started.sh"),
                 250: (240, "python3", "python3 -m pytest"),
                 240: (200, "bash", "bash -e /runner/work/_temp/step.sh"),
                 200: (100, "Runner.Worker", "/runner/bin/Runner.Worker"),
                 100: (1, "Runner.Listener", "/runner/bin/Runner.Listener")}
        proc = fake_proc(tmp_path / "proc", table)
        assert runner_guard.find_worker(runner_guard.linux_ancestors(500, proc=str(proc))) is None

    def test_on_windows_the_node_must_be_the_workers_own_child(self):
        chain = [(30, ["node.exe"], []), (25, ["python.exe"], []), (24, ["pwsh.exe"], []),
                 (10, ["Runner.Worker.exe"], [])]
        assert runner_guard.find_worker(chain) is None

    def test_a_shell_that_is_not_the_hook_is_not_the_hook(self, tmp_path):
        table = {**RUNNER, 300: (200, "bash", "bash -e /runner/work/_temp/step.sh")}
        proc = fake_proc(tmp_path / "proc", table)
        assert runner_guard.find_worker(runner_guard.linux_ancestors(500, proc=str(proc))) is None

    def test_a_walk_that_loops_or_breaks_ends(self, tmp_path):
        proc = fake_proc(tmp_path / "proc", {500: (400, "python3", "p"), 400: (500, "x", "x")})
        assert runner_guard.find_worker([]) is None
        assert len(runner_guard.linux_ancestors(500, proc=str(proc))) <= 64
        assert runner_guard.linux_ancestors(999, proc=str(proc)) == []

    def test_windows_stops_at_a_parent_younger_than_its_child(self):
        """Windows keeps a dead parent's pid in its children, and may give the
        pid to a new process: one created after the child is not its parent."""
        table = {30: (20, "python.exe"), 20: (10, "node.exe"), 10: (5, "Runner.Worker.exe"),
                 5: (0, "Runner.Listener.exe")}
        created = {30: 300, 20: 200, 10: 100, 5: 50}
        chain = runner_guard.windows_ancestors(30, table=table, created=created.get)
        assert runner_guard.find_worker(chain) == 10
        chain = runner_guard.windows_ancestors(30, table=table, created={**created, 10: 999}.get)
        assert runner_guard.find_worker(chain) is None


class TestEndingTheJob:
    def test_the_worker_is_killed_after_the_error_had_time_to_reach_github(self):
        done = []
        ended = runner_guard.end_the_job(
            {"RUNNER_GUARD_KILL_DELAY": "2"},
            ancestors=lambda: [(300, ["bash"], ["bash", "/runner/job-started.sh"]),
                               (200, ["Runner.Worker"], [])],
            kill=lambda pid: done.append(("kill", pid)), sleep=lambda s: done.append(("sleep", s)))
        assert ended is True and done == [("sleep", 2.0), ("kill", 200)]

    @pytest.mark.parametrize("value,delay", [(None, 5.0), ("x", 5.0), ("-3", 0.0), ("600", 30.0)])
    def test_the_delay_is_bounded(self, value, delay):
        slept = []
        env = {} if value is None else {"RUNNER_GUARD_KILL_DELAY": value}
        runner_guard.end_the_job(env, ancestors=lambda: [(2, ["node"], []),
                                                         (1, ["Runner.Worker"], [])],
                                 kill=lambda pid: None, sleep=slept.append)
        assert slept == [delay]

    def test_no_worker_found_is_said_and_nothing_is_killed(self, capsys):
        killed = []
        assert runner_guard.end_the_job({}, ancestors=lambda: [(2, ["node"], []),
                                                               (100, ["Runner.Listener"], [])],
                                        kill=killed.append, sleep=lambda s: None) is False
        assert killed == []
        out = capsys.readouterr().out
        assert "::warning title=Runner guard::" in out and "could not be ended" in out

    def test_a_kill_that_fails_is_said(self, capsys):
        def refuse(pid):
            raise PermissionError("denied")
        assert runner_guard.end_the_job({}, ancestors=lambda: [(3, ["node"], []),
                                                               (2, ["Runner.Worker"], [])],
                                        kill=refuse, sleep=lambda s: None) is False
        assert "could not be ended" in capsys.readouterr().out

    def test_check_ends_the_job_only_on_a_refusal_and_after_the_error_is_out(self, tmp_path, capsys):
        seen = []

        def end(env):
            seen.append(capsys.readouterr().out)
            return True
        refused = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        assert runner_guard.check(refused, end=end) == runner_guard.REFUSE
        assert len(seen) == 1 and origin_cases.REFUSED in seen[0]
        allowed = origin_cases.hook_env(origin_cases.by_id("push"), tmp_path)
        assert runner_guard.check(allowed, end=end) == 0
        assert len(seen) == 1

    def test_a_fault_while_ending_the_job_still_refuses_it(self, tmp_path, capsys):
        def boom(env):
            raise OSError("no /proc")
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        assert runner_guard.check(env, end=boom) == runner_guard.REFUSE
        assert "could not be ended" in capsys.readouterr().out
