"""The Windows runner's job hooks: room for the job before it starts, and the
runner's temp cleared after it ends (GitHub #7).

The same rule as the Linux unit's hook: under 15 GB free the hook clears
what is rebuildable, under 10 GB it warns, and under 3 GB it refuses the job
with an `::error` before a full disk can take the runner down. Only that
refusal fails a job. A fault in the hook itself never does.

The work is in `agent/hooks/windows/runner_disk.py`; the `.ps1` files are
what GitHub runs, because it runs nothing but `.ps1`, `.sh` and `.js`. They
start the agent's own Python, which measures the volume under the runner's
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


# ---- the .ps1 files GitHub runs ---------------------------------------------

SHELLS = [s for s in ("pwsh", "powershell") if shutil.which(s)]


def run_hook(shell, name, env):
    """As the runner does it: `pwsh -command ". '<path>'"`, or Windows
    PowerShell when there is no pwsh."""
    path = HOOKS / name
    full = dict(os.environ, **env)
    return subprocess.run([shutil.which(shell), "-NoLogo", "-NoProfile", "-NonInteractive",
                           "-Command", f". '{path}'"], env=full, capture_output=True,
                          text=True, timeout=300)


@pytest.fixture(params=SHELLS or [None])
def shell(request):
    if request.param is None:
        pytest.skip("no PowerShell on this machine")
    return request.param


class TestPowerShell:
    def test_plenty_of_room_lets_the_job_run(self, shell, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook(shell, "job-started.ps1", {
            "RUNNER_HOOK_PYTHON": sys.executable, "RUNNER_WORK_DIR": str(work),
            "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "0", "RUNNER_DISK_WARN_BELOW_GB": "0",
            "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Disk free before the job:" in result.stdout
        assert (work / "lib").exists()

    def test_a_disk_too_full_fails_the_job_with_an_error(self, shell, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook(shell, "job-started.ps1", {
            "RUNNER_HOOK_PYTHON": sys.executable, "RUNNER_WORK_DIR": str(work),
            "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "1000000000", "RUNNER_DISK_FAIL_BELOW_GB": "1000000000"})
        assert result.returncode == 1, result.stdout + result.stderr
        assert "::error title=Runner disk full::" in result.stdout
        assert not (work / "lib").exists(), "it cleaned before it refused"

    def test_low_disk_warns_and_runs(self, shell, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook(shell, "job-started.ps1", {
            "RUNNER_HOOK_PYTHON": sys.executable, "RUNNER_WORK_DIR": str(work),
            "GITHUB_WORKSPACE": str(work / "app" / "app"),
            "RUNNER_DISK_CLEAN_BELOW_GB": "0", "RUNNER_DISK_WARN_BELOW_GB": "1000000000",
            "RUNNER_DISK_FAIL_BELOW_GB": "0"})
        assert result.returncode == 0 and "::warning title=Runner disk low::" in result.stdout

    @pytest.mark.parametrize("name", ["job-started.ps1", "job-completed.ps1"])
    def test_no_python_never_fails_the_job(self, shell, name, tmp_path):
        result = run_hook(shell, name, {"RUNNER_HOOK_PYTHON": str(tmp_path / "python.exe"),
                                        "RUNNER_WORK_DIR": str(tmp_path)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning title=Runner hook::" in result.stdout

    @pytest.mark.parametrize("name", ["job-started.ps1", "job-completed.ps1"])
    def test_a_python_that_fails_never_fails_the_job(self, shell, name, tmp_path):
        broken = tmp_path / "broken.cmd"
        broken.write_text("@echo off\r\necho something went wrong 1>&2\r\nexit /b 9\r\n")
        result = run_hook(shell, name, {"RUNNER_HOOK_PYTHON": str(broken),
                                        "RUNNER_WORK_DIR": str(tmp_path)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ended with status 9" in result.stdout

    def test_a_hook_run_with_stop_on_error_still_never_fails_the_job(self, shell, tmp_path):
        """The runner's own PowerShell steps run with ErrorActionPreference
        Stop; a hook that inherits it must not turn a stderr line into a
        failed job."""
        broken = tmp_path / "broken.cmd"
        broken.write_text("@echo off\r\necho noise 1>&2\r\nexit /b 3\r\n")
        path = HOOKS / "job-started.ps1"
        result = subprocess.run(
            [shutil.which(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"$ErrorActionPreference = 'Stop'; . '{path}'"],
            env=dict(os.environ, RUNNER_HOOK_PYTHON=str(broken), RUNNER_WORK_DIR=str(tmp_path)),
            capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_after_a_job_the_temp_is_cleared(self, shell, tmp_path):
        work = runner_tree(tmp_path)
        result = run_hook(shell, "job-completed.ps1", {
            "RUNNER_HOOK_PYTHON": sys.executable, "RUNNER_WORK_DIR": str(work)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (work / "_temp" / "build.log").exists()
        assert (work / "_temp" / "_runner_file_commands").exists()
