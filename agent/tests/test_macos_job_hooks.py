"""The macOS runner's job hooks: room for the job before it starts, and what
only grows cleared after it ends (GitHub #7).

The same rule as the Linux unit's hook: under 15 GB free on the Data volume
holding the runner's work directory the hook clears what is rebuildable,
under 10 GB it warns, and under 3 GB it refuses the job with an `::error`.
Only that refusal fails a job; a fault in the hook never does.

Plain bash, as macOS ships it (3.2), so the agent can put the scripts into a
guest that has nothing else of its own. Run here under a `df` that answers
what each test needs: the macOS one reads 1024-byte blocks, available in the
fourth column of `df -Pk`.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).parents[1] / "hooks" / "macos"
GIB_KIB = 1000 ** 3 // 1024


def _bash():
    found = shutil.which("bash")
    if sys.platform == "win32":
        # System32's bash.exe is WSL's launcher: never run a hook in a distro.
        if not found or "system32" in found.lower():
            found = next((p for p in (r"C:\Program Files\Git\bin\bash.exe",
                                      r"C:\Program Files\Git\usr\bin\bash.exe")
                          if os.path.exists(p)), None)
    return found


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash here")


def posix(path):
    return Path(path).as_posix()


class Guest:
    """A runner's tree, a home with Xcode's DerivedData, a Gradle home, and a
    `df` that reports one free figure after another."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.work = tmp_path / "runners" / "rid" / "work"
        self.home = tmp_path / "Users" / "runner"
        self.gradle = tmp_path / "runners" / "rid" / "cache" / "gradle"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("_tool/node", "_actions/actions/checkout", "_temp/_github_workflow",
                     "_temp/_runner_file_commands", ".home/Library", "app/app/src",
                     "lib/lib/src", "old/old/bin"):
            (self.work / name).mkdir(parents=True)
        for name in ("app/app/src/main.swift", "lib/lib/src/lib.swift", "old/old/bin/big",
                     "_tool/node/node", "_actions/actions/checkout/action.yml",
                     "_temp/_github_workflow/event.json", "_temp/build.log",
                     "_temp/_runner_file_commands/set_env_1", ".home/.gitconfig"):
            (self.work / name).write_text(name)
        derived = self.home / "Library" / "Developer" / "Xcode" / "DerivedData"
        for name, age_days in (("Old-abc", 5), ("Recent-def", 0)):
            (derived / name).mkdir(parents=True)
            (derived / name / "info.plist").write_text(name)
            stamp = time.time() - age_days * 86400
            os.utime(derived / name, (stamp, stamp))
        self.derived = derived
        (self.gradle / "caches" / "build-cache-1").mkdir(parents=True)
        (self.gradle / "caches" / "build-cache-1" / "entry").write_text("cached")
        (self.gradle / "caches" / "modules-2").mkdir()
        self.free([80])

    def free(self, gigabytes):
        """What `df` answers, call after call; the last one repeats."""
        (self.bin / "free").write_text("\n".join(str(int(g * GIB_KIB)) for g in gigabytes) + "\n")
        df = self.bin / "df"
        df.write_text(
            "#!/bin/bash\n"
            "here=$(dirname \"$0\")\n"
            "value=$(head -n 1 \"$here/free\")\n"
            "if [ \"$(wc -l < \"$here/free\")\" -gt 1 ]; then\n"
            "  tail -n +2 \"$here/free\" > \"$here/free.next\" && mv \"$here/free.next\" \"$here/free\"\n"
            "fi\n"
            "echo \"$*\" >> \"$here/df.calls\"\n"
            "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
            "echo \"/dev/disk3s5 1000000000 1 $value 50% /System/Volumes/Data\"\n")
        df.chmod(0o755)

    def env(self, **extra):
        env = dict(os.environ)
        env.pop("GITHUB_WORKSPACE", None)
        env.update({"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "RUNNER_WORK_DIR": posix(self.work),
                    "GITHUB_WORKSPACE": posix(self.work / "app" / "app"),
                    "HOME": posix(self.work / ".home"),
                    "RUNNER_HOOK_USER_HOME": posix(self.home),
                    "GRADLE_USER_HOME": posix(self.gradle)})
        env.update(extra)
        return env

    def run(self, name, **extra):
        # As the runner does it: bash with -e and pipefail on.
        return subprocess.run([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail",
                               posix(HOOKS / name)], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)


@pytest.fixture
def guest(tmp_path):
    return Guest(tmp_path)


def test_every_hook_is_a_script_the_runner_accepts():
    for name in ("job-started.sh", "job-completed.sh"):
        assert (HOOKS / name).is_file()


class TestBeforeTheJob:
    def test_plenty_of_room_says_so_and_does_nothing(self, guest):
        result = guest.run("job-started.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Disk free before the job: 80 GB" in result.stdout
        assert "::" not in result.stdout
        assert (guest.work / "lib").exists() and (guest.derived / "Old-abc").exists()

    def test_the_volume_measured_is_the_one_holding_the_work_directory(self, guest):
        guest.run("job-started.sh")
        assert posix(guest.work) in (guest.bin / "df.calls").read_text()

    def test_under_fifteen_cleans_first_and_carries_on(self, guest):
        guest.free([14, 40])
        result = guest.run("job-started.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Under 15 GB: clearing" in result.stdout
        assert "Disk free after cleanup: 40 GB" in result.stdout
        assert "::warning" not in result.stdout

    def test_the_cleanup_takes_only_what_is_rebuildable(self, guest):
        guest.free([14, 40])
        guest.run("job-started.sh")
        work = guest.work
        assert not (work / "lib").exists() and not (work / "old").exists()
        assert (work / "app/app/src/main.swift").exists()
        for kept in ("_tool/node/node", "_actions/actions/checkout/action.yml",
                     "_temp/_github_workflow/event.json", ".home/.gitconfig"):
            assert (work / kept).exists(), kept
        assert not (guest.derived / "Old-abc").exists()
        assert (guest.derived / "Recent-def").exists()
        assert not (guest.gradle / "caches" / "build-cache-1").exists()
        assert (guest.gradle / "caches" / "modules-2").exists()

    def test_under_ten_after_cleaning_warns(self, guest):
        guest.free([9, 8])
        result = guest.run("job-started.sh")
        assert result.returncode == 0
        assert "::warning title=Runner disk low::only 8 GB free on this runner." in result.stdout

    def test_under_three_after_cleaning_refuses_the_job(self, guest):
        guest.free([2, 2])
        result = guest.run("job-started.sh")
        assert result.returncode == 1
        assert "::error title=Runner disk full::only 2.0 GB free on this runner" in result.stdout

    def test_thresholds_come_from_the_environment(self, guest):
        guest.free([50])
        result = guest.run("job-started.sh", RUNNER_DISK_CLEAN_BELOW_GB="60",
                           RUNNER_DISK_FAIL_BELOW_GB="55")
        assert result.returncode == 1 and not (guest.work / "lib").exists()

    def test_fractional_thresholds_are_read(self, guest):
        guest.free([0.4])
        result = guest.run("job-started.sh", RUNNER_DISK_CLEAN_BELOW_GB="0.1",
                           RUNNER_DISK_WARN_BELOW_GB="0.1", RUNNER_DISK_FAIL_BELOW_GB="0.5")
        assert result.returncode == 1

    def test_a_df_that_answers_nothing_lets_the_job_run(self, guest):
        (guest.bin / "df").write_text("#!/bin/bash\nexit 1\n")
        result = guest.run("job-started.sh")
        assert result.returncode == 0 and "::error" not in result.stdout

    @pytest.mark.parametrize("spelling", ["APP/app", "App/app"])
    def test_the_current_workspace_is_kept_however_its_name_is_cased(self, guest, spelling):
        """APFS is case-insensitive: GITHUB_WORKSPACE spelled with other
        capitals is still the current job's directory."""
        guest.free([14, 40])
        result = guest.run("job-started.sh",
                           GITHUB_WORKSPACE=posix(guest.work) + "/" + spelling)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (guest.work / "app/app/src/main.swift").exists()
        assert not (guest.work / "lib").exists()

    def test_without_a_current_workspace_no_workspace_is_removed(self, guest):
        guest.free([14, 40])
        result = guest.run("job-started.sh", GITHUB_WORKSPACE="")
        assert result.returncode == 0
        assert (guest.work / "lib").exists() and (guest.work / "old").exists()

    def test_a_link_is_removed_not_followed(self, guest, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        link = guest.work / "lib" / "lib" / "node_modules"
        top = guest.work / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
            top.symlink_to(outside, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            # No symlink privilege: a junction, which Git's bash reads as a
            # link too.
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
            _winapi.CreateJunction(str(outside), str(top))
        guest.free([14, 40])
        guest.run("job-started.sh")
        assert not (guest.work / "lib").exists()
        assert os.path.lexists(top)
        assert (outside / "precious").read_text() == "keep"

    def test_a_fault_in_the_hook_never_fails_the_job(self, guest):
        (guest.bin / "df").write_text("#!/bin/bash\necho 'Filesystem'\necho 'garbage line'\n")
        result = guest.run("job-started.sh", RUNNER_WORK_DIR="/nonexistent/work")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_missing_library_never_fails_the_job(self, guest, tmp_path):
        lone = tmp_path / "lone"
        lone.mkdir()
        shutil.copy(HOOKS / "job-started.sh", lone / "job-started.sh")
        result = subprocess.run([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail",
                                 posix(lone / "job-started.sh")], env=guest.env(),
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0
        assert "::warning title=Runner hook::" in result.stdout


class TestAfterTheJob:
    def test_the_runner_temp_and_stale_derived_data_are_cleared(self, guest):
        result = guest.run("job-completed.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        temp = guest.work / "_temp"
        assert sorted(p.name for p in temp.iterdir()) == ["_runner_file_commands"]
        assert not (guest.derived / "Old-abc").exists()
        assert (guest.derived / "Recent-def").exists()
        assert (guest.work / "lib").exists() and (guest.work / "app").exists()

    def test_a_fault_never_fails_the_job(self, guest):
        result = guest.run("job-completed.sh", RUNNER_WORK_DIR="/nonexistent/work",
                           RUNNER_HOOK_USER_HOME="/nonexistent/home")
        assert result.returncode == 0, result.stdout + result.stderr
