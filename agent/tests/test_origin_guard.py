"""The job-started hook's first check: whose code is this job about to run?

The runner group lets every repository in the org use these runners, public
ones included, and 26 public repositories do. Until now GitHub's "Approve and
run" click was all that stood between a stranger's fork and a self-hosted
runner. `runner_guard.py` refuses a pull request from a fork unless its
author is trusted - OWNER, MEMBER or COLLABORATOR, or named in
RUNNER_TRUSTED_AUTHORS - before any step, checkout included, has run.

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
    code = runner_guard.check(origin_cases.hook_env(case, tmp_path))
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
    assert runner_guard.check(origin_cases.hook_env(case, tmp_path)) == runner_guard.REFUSE
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and out.count("::") == 2


def test_trusted_authors_are_read_without_case_or_spaces():
    assert runner_guard.trusted_authors(" Alice,,BOB , ") == {"alice", "bob"}
    assert runner_guard.trusted_authors("") == set()
    assert runner_guard.trusted_authors(None) == set()


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
    assert imported <= {"json", "os", "re", "sys"}, imported


def test_the_file_is_ascii_so_any_locale_reads_it():
    GUARD.read_bytes().decode("ascii")
