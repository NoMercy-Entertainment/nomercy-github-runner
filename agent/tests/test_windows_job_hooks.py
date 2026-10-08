"""The Windows runner's job hooks: room for the job before it starts, and the
runner's temp cleared after it ends (GitHub #7).

The same rule as the Linux unit's hook: under 15 GB free the hook clears
what is rebuildable, under 10 GB it warns, and under 3 GB it refuses the job
with an `::error` before a full disk can take the runner down. Only that
refusal fails a job. A fault in the hook itself never does.

The work is in `agent/hooks/windows/runner_disk.py`; the `.js` files are
what GitHub runs, under its own node, because it runs nothing but `.js`,
`.sh` and `.ps1`, and a `.ps1` is refused by the ARM64 guest's execution
policy. They start the agent's own Python, which measures the volume under the runner's
work directory correctly even when that volume is the runner's own disk,
mounted at a folder rather than a drive letter.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from . import origin_cases

HOOKS = Path(__file__).parents[1] / "hooks" / "windows"
spec = importlib.util.spec_from_file_location("windows_runner_disk", HOOKS / "runner_disk.py")
runner_disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_disk)

GB = 1000 ** 3
WINDOWS = sys.platform == "win32"


def junction(link, target):
    """A directory junction, the link the workspace alias is and the one
    pnpm fills node_modules with. Needs no privilege, unlike a symlink."""
    if not WINDOWS:
        pytest.skip("junctions are Windows-only")
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def runner_tree(tmp_path):
    """A runner's work directory as GitHub leaves it after a few jobs."""
    work = tmp_path / "runners" / "rid" / "work"
    for name in ("_tool/node/20", "_actions/actions/checkout", "_temp/_github_workflow",
                 "_temp/_runner_file_commands", "_PipelineMapping/org/app",
                 ".cache/pip", "app/app/src", "lib/lib/src", "old/old/bin"):
        (work / name).mkdir(parents=True)
    for name in ("app/app/src/main.c", "lib/lib/src/lib.c", "old/old/bin/big.dll",
                 "_tool/node/20/node.exe", "_actions/actions/checkout/action.yml",
                 "_temp/_github_workflow/event.json", "_temp/build.log",
                 "_temp/_runner_file_commands/set_env_1", ".gitconfig", ".cache/pip/wheel"):
        (work / name).write_text(name)
    return work


class FakeDisk:
    def __init__(self, free_gb, after_cleanup_gb=None):
        self.free = free_gb * GB
        self.after = None if after_cleanup_gb is None else after_cleanup_gb * GB
        self.cleaned = 0

    def free_bytes(self, path):
        return self.free

    def cleanup(self):
        self.cleaned += 1
        if self.after is not None:
            self.free = self.after


def check(disk, capsys, env=None):
    code = runner_disk.check_disk([r"D:\runners\rid\work"], env or {},
                                  free_bytes=disk.free_bytes, cleanup=disk.cleanup)
    return code, capsys.readouterr().out


class TestDisk:
    """Thresholds and wording are the Linux unit's (images/linux/unit)."""

    def test_plenty_of_room_says_so_and_does_nothing(self, capsys):
        disk = FakeDisk(80)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 0
        assert "::" not in out and "Disk free before the job: 80 GB" in out

    def test_under_fifteen_cleans_first_and_carries_on(self, capsys):
        disk = FakeDisk(14, after_cleanup_gb=40)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 1
        assert "::warning" not in out and "Disk free after cleanup: 40 GB" in out

    def test_under_ten_after_cleaning_warns(self, capsys):
        disk = FakeDisk(9, after_cleanup_gb=8)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 1
        assert "::warning title=Runner disk low::only 8 GB free on this runner." in out

    def test_under_three_after_cleaning_refuses_the_job(self, capsys):
        disk = FakeDisk(2, after_cleanup_gb=2)
        code, out = check(disk, capsys)
        assert code == runner_disk.REFUSE
        assert "::error title=Runner disk full::only 2.0 GB free on this runner" in out

    def test_thresholds_come_from_the_environment(self, capsys):
        disk = FakeDisk(50, after_cleanup_gb=50)
        code, _ = check(disk, capsys, {"RUNNER_DISK_CLEAN_BELOW_GB": "60",
                                       "RUNNER_DISK_FAIL_BELOW_GB": "55"})
        assert disk.cleaned == 1 and code == runner_disk.REFUSE

    def test_a_cleanup_that_fails_does_not_stop_the_check(self, capsys):
        disk = FakeDisk(2)

        def broken():
            raise RuntimeError("access denied")
        code = runner_disk.check_disk(["D:\\"], {}, free_bytes=disk.free_bytes,
                                      cleanup=broken)
        assert code == runner_disk.REFUSE
        assert "access denied" in capsys.readouterr().out

    def test_a_volume_that_cannot_be_read_lets_the_job_run(self, capsys):
        def unreadable(path):
            raise OSError("no such volume")
        assert runner_disk.check_disk(["Q:\\nothing"], {}, free_bytes=unreadable,
                                      cleanup=lambda: None) == 0

    def test_the_real_volume_is_read(self, tmp_path, capsys):
        """shutil.disk_usage asks Windows for the volume holding the path,
        which is the runner's own disk when it is mounted at its folder."""
        code = runner_disk.check_disk([str(tmp_path)], {}, cleanup=lambda: None)
        assert code == 0 and "Disk free before the job:" in capsys.readouterr().out


class TestEarlierWorkspaces:
    def test_earlier_workspaces_go_and_the_current_one_stays(self, tmp_path):
        work = runner_tree(tmp_path)
        removed = runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app")})
        assert removed == 2
        assert (work / "app/app/src/main.c").exists()
        assert not (work / "lib").exists() and not (work / "old").exists()

    def test_what_the_runner_and_the_profile_keep_is_never_touched(self, tmp_path):
        """_tool and _actions are the runner's; _temp holds this job's event
        payload and file commands; HOME is the work directory, so a dot
        entry is the runner account's profile."""
        work = runner_tree(tmp_path)
        runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app")})
        for kept in ("_tool/node/20/node.exe", "_actions/actions/checkout/action.yml",
                     "_temp/_github_workflow/event.json", "_temp/build.log",
                     "_PipelineMapping/org/app", ".gitconfig", ".cache/pip/wheel"):
            assert (work / kept).exists(), kept

    def test_the_current_workspace_is_known_through_the_short_alias(self, tmp_path):
        """GitHub is configured with the junction alias as its work folder
        (agent/windows_workspace.py), so GITHUB_WORKSPACE is under the alias,
        not under RUNNER_WORK_DIR."""
        work = runner_tree(tmp_path)
        (tmp_path / "w").mkdir()
        alias = tmp_path / "w" / "rid"
        junction(alias, work)
        removed = runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "RUNNER_JOB_WORK_DIR": str(alias),
             "GITHUB_WORKSPACE": str(alias / "app" / "app")})
        assert removed == 2 and (work / "app/app/src/main.c").exists()

    @pytest.mark.parametrize("workspace", [None, "", r"Q:\elsewhere\app\app"])
    def test_without_a_current_workspace_it_knows_of_nothing_is_removed(self, tmp_path, workspace):
        work = runner_tree(tmp_path)
        env = {"RUNNER_WORK_DIR": str(work)}
        if workspace is not None:
            env["GITHUB_WORKSPACE"] = workspace
        assert runner_disk.remove_earlier_workspaces(env) == 0
        assert (work / "lib/lib/src/lib.c").exists() and (work / "old").exists()

    def test_a_link_inside_an_old_workspace_is_removed_not_followed(self, tmp_path):
        work = runner_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        junction(work / "lib" / "lib" / "node_modules", outside)
        runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app")})
        assert not (work / "lib").exists()
        assert (outside / "precious").read_text() == "keep"

    def test_a_link_in_the_work_directory_itself_is_left_alone(self, tmp_path):
        work = runner_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        junction(work / "linked", outside)
        runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app")})
        assert (work / "linked").exists() and (outside / "precious").exists()

    def test_a_work_directory_that_is_a_link_is_not_walked(self, tmp_path):
        work = runner_tree(tmp_path)
        linked = tmp_path / "linked-work"
        junction(linked, work)
        assert runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(linked), "GITHUB_WORKSPACE": str(linked / "app" / "app")}) == 0
        assert (work / "lib").exists()

    def test_read_only_files_git_left_are_removed(self, tmp_path):
        work = runner_tree(tmp_path)
        objects = work / "lib" / "lib" / ".git" / "objects" / "ab"
        objects.mkdir(parents=True)
        (objects / "cdef").write_text("blob")
        os.chmod(objects / "cdef", stat.S_IREAD)
        runner_disk.remove_earlier_workspaces(
            {"RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app")})
        assert not (work / "lib").exists()


class TestTemp:
    def test_the_runner_temp_is_cleared_but_its_file_commands_stay(self, tmp_path):
        """The completion hook is itself a step: the runner reads its file
        commands once it has finished."""
        work = runner_tree(tmp_path)
        (work / "_temp" / "big").mkdir()
        (work / "_temp" / "big" / "image.iso").write_text("x" * 1000)
        assert runner_disk.clear_temp({"RUNNER_WORK_DIR": str(work)}) == 3
        assert sorted(p.name for p in (work / "_temp").iterdir()) == ["_runner_file_commands"]
        assert (work / "app/app/src/main.c").exists()

    def test_a_link_in_temp_is_removed_not_followed(self, tmp_path):
        work = runner_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        junction(work / "_temp" / "linked", outside)
        runner_disk.clear_temp({"RUNNER_WORK_DIR": str(work)})
        assert not os.path.lexists(work / "_temp" / "linked")
        assert (outside / "precious").read_text() == "keep"

    def test_a_file_in_use_is_skipped_and_the_rest_goes(self, tmp_path, monkeypatch):
        work = runner_tree(tmp_path)
        real = runner_disk._remove

        def held(path):
            if os.path.basename(path) == "build.log":
                raise PermissionError("in use")
            real(path)
        monkeypatch.setattr(runner_disk, "_remove", held)
        assert runner_disk.clear_temp({"RUNNER_WORK_DIR": str(work)}) == 1
        assert (work / "_temp" / "build.log").exists()
        assert not (work / "_temp" / "_github_workflow").exists()

    def test_no_work_directory_clears_nothing(self, tmp_path):
        assert runner_disk.clear_temp({}) == 0
        assert runner_disk.clear_temp({"RUNNER_WORK_DIR": str(tmp_path / "absent")}) == 0


class TestMain:
    def test_before_a_job_it_measures_the_work_directory(self, tmp_path, monkeypatch):
        seen = {}

        def fake_check(paths, env, **kwargs):
            seen["paths"] = paths
            return 0
        monkeypatch.setattr(runner_disk, "check_disk", fake_check)
        assert runner_disk.main(["started"], {"RUNNER_WORK_DIR": str(tmp_path)}) == 0
        assert seen["paths"] == [str(tmp_path)]

    def test_a_job_start_does_not_touch_the_runner_temp(self, tmp_path):
        """The runner empties _temp itself when a job starts; what is in it
        then is the job's own event payload and file commands."""
        work = runner_tree(tmp_path)
        code = runner_disk.main(["started"], {
            "RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "1000000000", "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert code == 0
        assert (work / "_temp/_github_workflow/event.json").exists()
        assert not (work / "lib").exists()

    def test_the_refusal_is_passed_on(self, monkeypatch):
        monkeypatch.setattr(runner_disk, "check_disk", lambda *a, **k: runner_disk.REFUSE)
        assert runner_disk.main(["started"], {"RUNNER_WORK_DIR": "D:\\"}) == runner_disk.REFUSE

    @pytest.mark.parametrize("stage", ["started", "completed"])
    def test_a_fault_in_the_hook_never_fails_the_job(self, stage, capsys, monkeypatch):
        def boom(*a, **k):
            raise OSError("unreadable")
        monkeypatch.setattr(runner_disk, "check_disk", boom)
        monkeypatch.setattr(runner_disk, "clear_temp", boom)
        assert runner_disk.main([stage], {"RUNNER_WORK_DIR": "D:\\"}) == 0
        assert "::warning title=Runner hook::" in capsys.readouterr().out

    def test_after_a_job_the_temp_is_cleared(self, tmp_path):
        work = runner_tree(tmp_path)
        assert runner_disk.main(["completed"], {"RUNNER_WORK_DIR": str(work)}) == 0
        assert not (work / "_temp" / "build.log").exists()
        assert (work / "lib").exists()

    def test_an_unknown_stage_lets_the_job_run(self, capsys):
        assert runner_disk.main(["sideways"], {}) == 0
        assert "::warning" in capsys.readouterr().out


# ---- the .js files GitHub runs ----------------------------------------------
#
# GitHub starts a .ps1 hook as `pwsh -command ". '<path>'"`, and the ARM64
# guest's execution policy refuses to dot-source a script: the hook would
# fail before any line of it ran, and every job with it. A .js hook runs
# under the runner's own bundled node, which has no execution policy and
# starts in seconds where PowerShell takes minutes under emulation.

NODE = shutil.which("node")


def _environment(env, python):
    full = {k: v for k, v in os.environ.items()
            if not k.startswith(("RUNNER_", "GITHUB_"))}
    if python is not None:
        full["RUNNER_HOOK_PYTHON"] = str(python)
    full.update(env)
    return full


def run_hook(name, env, python=sys.executable, hooks=HOOKS):
    """As the runner does it: its node, the script's path, nothing else."""
    result = subprocess.run([NODE, str(hooks / name)], env=_environment(env, python),
                            capture_output=True, timeout=300)
    result.stdout = result.stdout.decode("utf-8")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result


def hooks_with(tmp_path, python_source):
    """A copy of the hooks whose runner_disk.py is `python_source`."""
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("__pycache__"))
    (hooks / "runner_disk.py").write_text(python_source)
    return hooks


@pytest.mark.skipif(NODE is None, reason="no node here")
class TestNode:
    def test_plenty_of_room_lets_the_job_run(self, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook("job-started.js", {
            "RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "0", "RUNNER_DISK_WARN_BELOW_GB": "0",
            "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Disk free before the job:" in result.stdout
        assert (work / "lib").exists()

    def test_a_disk_too_full_fails_the_job_with_an_error(self, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook("job-started.js", {
            "RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "1000000000", "RUNNER_DISK_FAIL_BELOW_GB": "1000000000"})
        assert result.returncode == 1, result.stdout + result.stderr
        assert "::error title=Runner disk full::" in result.stdout
        assert not (work / "lib").exists(), "it cleaned before it refused"

    def test_low_disk_warns_and_runs(self, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook("job-started.js", {
            "RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "0", "RUNNER_DISK_WARN_BELOW_GB": "1000000000",
            "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert result.returncode == 0 and "::warning title=Runner disk low::" in result.stdout

    @pytest.mark.parametrize("name", ["job-started.js", "job-completed.js"])
    def test_no_python_named_never_fails_the_job(self, name, tmp_path):
        result = run_hook(name, {"RUNNER_WORK_DIR": str(tmp_path)}, python=None)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning title=Runner hook::" in result.stdout

    @pytest.mark.parametrize("name", ["job-started.js", "job-completed.js"])
    def test_a_python_that_is_not_there_never_fails_the_job(self, name, tmp_path):
        result = run_hook(name, {"RUNNER_WORK_DIR": str(tmp_path)},
                          python=tmp_path / "python.exe")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning title=Runner hook::" in result.stdout

    @pytest.mark.parametrize("name", ["job-started.js", "job-completed.js"])
    def test_a_python_that_fails_never_fails_the_job(self, name, tmp_path):
        hooks = hooks_with(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(9)\n")
        result = run_hook(name, {"RUNNER_WORK_DIR": str(tmp_path)}, hooks=hooks)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ended with status 9" in result.stdout

    def test_a_missing_runner_disk_never_fails_the_job(self, tmp_path):
        hooks = hooks_with(tmp_path, "")
        (hooks / "runner_disk.py").unlink()
        result = run_hook("job-started.js", {"RUNNER_WORK_DIR": str(tmp_path)}, hooks=hooks)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning title=Runner hook::" in result.stdout

    def test_75_after_a_job_is_a_fault_not_a_refusal(self, tmp_path):
        hooks = hooks_with(tmp_path, "import sys\nsys.exit(75)\n")
        result = run_hook("job-completed.js", {"RUNNER_WORK_DIR": str(tmp_path)}, hooks=hooks)
        assert result.returncode == 0

    def test_non_ascii_output_reaches_the_log_intact(self, tmp_path):
        """A path can carry any character; the agent's Python writes UTF-8
        whatever the console's code page is."""
        work = runner_tree(tmp_path / "werkmap-é")
        result = run_hook("job-started.js", {
            "RUNNER_WORK_DIR": str(work), "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "0", "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "werkmap-é" in result.stdout

    def test_after_a_job_the_temp_is_cleared(self, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook("job-completed.js", {"RUNNER_WORK_DIR": str(work)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (work / "_temp" / "build.log").exists()
        assert (work / "_temp" / "_runner_file_commands").exists()


@pytest.mark.skipif(NODE is None, reason="no node here")
@pytest.mark.parametrize("name", origin_cases.END_TO_END)
def test_whose_code_end_to_end(tmp_path, name):
    """job-started.js as the runner starts it, with an event file as the
    runner writes one: the guard in runner_disk.py answers before the disk
    is looked at, and a refusal is the hook's exit 1."""
    case = origin_cases.by_id(name)
    work = runner_tree(tmp_path)
    env = origin_cases.hook_env(case, tmp_path)
    env.update(RUNNER_WORK_DIR=str(work), GITHUB_WORKSPACE=str(work / "app" / "app"),
               RUNNER_DISK_CLEAN_BELOW_GB="0", RUNNER_DISK_WARN_BELOW_GB="0",
               RUNNER_DISK_FAIL_BELOW_GB="0")
    result = run_hook("job-started.js", env)
    assert result.returncode == (0 if case["allowed"] else 1), result.stdout + result.stderr
    for line in case["out"]:
        assert line in result.stdout, result.stdout
    assert ("Disk free before the job" in result.stdout) == case["allowed"]


class TestWhoseCode:
    """runner_guard.py, the Linux unit's own, answers first."""

    def test_it_is_the_linux_units_guard_byte_for_byte(self):
        linux = Path(__file__).parents[2] / "images/linux/unit/runner/runner_guard.py"
        assert (HOOKS / "runner_guard.py").read_bytes() == linux.read_bytes()

    def test_a_refused_job_is_not_measured(self, tmp_path, monkeypatch, capsys):
        measured = []
        monkeypatch.setattr(runner_disk, "check_disk", lambda *a, **k: measured.append(a) or 0)
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        env["RUNNER_WORK_DIR"] = str(tmp_path)
        assert runner_disk.main(["started"], env) == runner_disk.REFUSE
        assert measured == []
        assert origin_cases.REFUSED in capsys.readouterr().out

    def test_without_a_work_directory_the_guard_still_answers(self, tmp_path, capsys):
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        assert runner_disk.main(["started"], env) == runner_disk.REFUSE

    def test_after_the_job_nobody_is_asked(self, tmp_path, capsys):
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        env["RUNNER_WORK_DIR"] = str(runner_tree(tmp_path))
        assert runner_disk.main(["completed"], env) == 0
        assert "Origin" not in capsys.readouterr().out

    def test_a_guard_that_cannot_be_loaded_lets_the_job_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(runner_disk, "GUARD", str(tmp_path / "missing.py"))
        monkeypatch.setattr(runner_disk, "check_disk", lambda *a, **k: 0)
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        env["RUNNER_WORK_DIR"] = str(tmp_path)
        assert runner_disk.main(["started"], env) == 0
        out = capsys.readouterr().out
        assert "::warning title=Runner guard::" in out and "origin not checked" in out

    def test_the_guard_is_the_one_beside_the_hook(self):
        assert Path(runner_disk.GUARD) == HOOKS / "runner_guard.py"


def test_the_hooks_need_nothing_but_node_itself():
    import re
    for name in ("job-started.js", "job-completed.js", "run_hook.js"):
        text = (HOOKS / name).read_text(encoding="utf-8")
        required = set(re.findall(r"require\(((?:[^()]|\([^()]*\))*)\)", text))
        assert required <= {'"child_process"', '"fs"', '"path"',
                            'path.join(__dirname, "run_hook.js")'}, required


def test_no_powershell_hook_is_left_to_be_wired_by_mistake():
    assert not list(HOOKS.glob("*.ps1"))

