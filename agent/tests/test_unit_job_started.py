"""The job-started hook puts back what a previous job took, and refuses a job
only when the disk is too full for it to finish.

GitHub #13: nomercy-whisper-models runs jlumbroso/free-disk-space with
`android: true` on beast-unit, which is `rm -rf /usr/local/lib/android`. A
unit's root is writable and outlives its jobs, so every runner one of those
jobs landed on lost the SDK until it was recreated (2026-10-04, -1 -3 -4 -8).
GitHub #7: a full disk crashed a runner mid-release with nothing in its log.
"""
import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "images/linux/unit/runner/job_started.py"
spec = importlib.util.spec_from_file_location("unit_job_started", SCRIPT)
job_started = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_started)

GB = 1000 ** 3


def sdk(root):
    (root / "sdk" / "platforms" / "android-35").mkdir(parents=True)
    (root / "sdk" / "platforms" / "android-36").mkdir(parents=True)
    (root / "sdk" / "platforms" / "android-35" / "android.jar").write_text("35")
    (root / "sdk" / "platforms" / "android-36" / "android.jar").write_text("36")
    (root / "sdk" / "build-tools").mkdir()
    (root / "sdk" / "build-tools" / "aapt2").write_text("tool")
    return root


class TestRestore:
    def test_a_wiped_sdk_comes_back_whole(self, tmp_path):
        pristine = sdk(tmp_path / "pristine")
        live = tmp_path / "live"
        restored = job_started.restore_tree(pristine, live)
        assert restored == 3
        assert (live / "sdk/platforms/android-35/android.jar").read_text() == "35"
        assert (live / "sdk/build-tools/aapt2").read_text() == "tool"

    def test_one_missing_platform_is_all_that_is_restored(self, tmp_path):
        pristine = sdk(tmp_path / "pristine")
        live = sdk(tmp_path / "live")
        (live / "sdk/platforms/android-35/android.jar").unlink()
        (live / "sdk/platforms/android-35").rmdir()
        assert job_started.restore_tree(pristine, live) == 1
        assert (live / "sdk/platforms/android-35/android.jar").exists()

    def test_what_a_job_changed_or_added_is_left_alone(self, tmp_path):
        pristine = sdk(tmp_path / "pristine")
        live = sdk(tmp_path / "live")
        (live / "sdk/build-tools/aapt2").write_text("updated by sdkmanager")
        (live / "sdk/platforms/android-37").mkdir()
        assert job_started.restore_tree(pristine, live) == 0
        assert (live / "sdk/build-tools/aapt2").read_text() == "updated by sdkmanager"
        assert (live / "sdk/platforms/android-37").is_dir()

    def test_a_restored_file_does_not_change_the_pristine_copy(self, tmp_path):
        pristine = sdk(tmp_path / "pristine")
        live = tmp_path / "live"
        job_started.restore_tree(pristine, live)
        # Written in place, as sdkmanager rewrites its package.xml: a restore
        # by hard link would carry this into the copy it restores from.
        (live / "sdk/platforms/android-36/android.jar").write_text("changed")
        assert (pristine / "sdk/platforms/android-36/android.jar").read_text() == "36"

    def test_links_are_restored_as_links(self, tmp_path):
        pristine = sdk(tmp_path / "pristine")
        try:
            (pristine / "sdk" / "latest").symlink_to("build-tools")
        except OSError:
            pytest.skip("symlink creation unavailable")
        live = tmp_path / "live"
        job_started.restore_tree(pristine, live)
        assert os.readlink(live / "sdk" / "latest") == "build-tools"

    def test_no_pristine_copy_restores_nothing(self, tmp_path):
        assert job_started.restore_tree(tmp_path / "absent", tmp_path / "live") == 0
        assert not (tmp_path / "live").exists()


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
    code = job_started.check_disk(["/runner/work", "/"], env or {},
                                  free_bytes=disk.free_bytes, cleanup=disk.cleanup)
    return code, capsys.readouterr().out


class TestDisk:
    def test_plenty_of_room_says_so_and_does_nothing(self, capsys):
        disk = FakeDisk(80)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 0
        assert "::" not in out and "80" in out

    def test_under_fifteen_cleans_first_and_carries_on(self, capsys):
        disk = FakeDisk(14, after_cleanup_gb=40)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 1
        assert "::warning" not in out

    def test_under_ten_after_cleaning_warns(self, capsys):
        disk = FakeDisk(9, after_cleanup_gb=8)
        code, out = check(disk, capsys)
        assert code == 0 and disk.cleaned == 1
        assert "::warning" in out and "8" in out

    def test_under_three_after_cleaning_refuses_the_job(self, capsys):
        disk = FakeDisk(2, after_cleanup_gb=2)
        code, out = check(disk, capsys)
        assert code == job_started.REFUSE
        assert "::error" in out

    def test_thresholds_come_from_the_environment(self, capsys):
        disk = FakeDisk(50, after_cleanup_gb=50)
        code, out = check(disk, capsys, {"RUNNER_DISK_CLEAN_BELOW_GB": "60",
                                          "RUNNER_DISK_FAIL_BELOW_GB": "55"})
        assert disk.cleaned == 1 and code == job_started.REFUSE

    def test_a_cleanup_that_fails_does_not_stop_the_check(self, capsys):
        disk = FakeDisk(2)
        def broken():
            raise RuntimeError("docker is gone")
        code = job_started.check_disk(["/"], {}, free_bytes=disk.free_bytes,
                                      cleanup=broken)
        assert code == job_started.REFUSE
        assert "docker is gone" in capsys.readouterr().out


class TestMain:
    def test_a_fault_in_the_hook_never_fails_the_job(self, capsys, monkeypatch):
        def boom(*a, **k):
            raise OSError("unreadable")
        monkeypatch.setattr(job_started, "restore_tree", boom)
        monkeypatch.setattr(job_started, "check_disk", boom)
        assert job_started.main({}) == 0
        assert "::warning" in capsys.readouterr().out

    def test_a_restore_is_announced_to_the_job(self, tmp_path, capsys, monkeypatch):
        pristine = sdk(tmp_path / "pristine")
        live = tmp_path / "live"
        monkeypatch.setattr(job_started, "check_disk", lambda *a, **k: 0)
        code = job_started.main({"RUNNER_ANDROID_PRISTINE": str(pristine),
                                 "RUNNER_ANDROID_LIVE": str(live)})
        out = capsys.readouterr().out
        assert code == 0 and (live / "sdk/build-tools/aapt2").exists()
        assert "::warning" in out and "Android SDK" in out

    def test_the_refusal_is_passed_on(self, monkeypatch, tmp_path):
        monkeypatch.setattr(job_started, "check_disk", lambda *a, **k: job_started.REFUSE)
        assert job_started.main({"RUNNER_ANDROID_PRISTINE": str(tmp_path / "x")}) == job_started.REFUSE


SHELL_HOOK = Path(__file__).parents[2] / "images/linux/unit/runner/job-started.sh"


@pytest.mark.parametrize("python_status,hook_status", [(0, 0), (2, 0), (124, 0), (75, 1)])
def test_only_the_refusal_fails_the_job_under_the_runners_errexit(tmp_path, python_status, hook_status):
    """The runner runs a .sh hook as `bash -e -o pipefail`. Without guarding
    the call, any status other than 0 ended the script at once and failed
    the job - the refusal only by accident, a crash or timeout as well."""
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        pytest.skip("needs a real bash")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "timeout").write_text(f"#!/usr/bin/env bash\nexit {python_status}\n", newline="\n")
    (fake / "timeout").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}")
    if os.name == "nt":
        env["PATH"] = "/" + fake.as_posix().replace(":", "", 1) + ":" + "/usr/bin:/bin"
    script = tmp_path / "job-started.sh"
    script.write_text(SHELL_HOOK.read_text(encoding="utf-8"), newline="\n")
    done = subprocess.run([bash, "-e", "-o", "pipefail", script.as_posix()],
                          capture_output=True, text=True, env=env)
    assert done.returncode == hook_status, done.stdout + done.stderr


def test_the_cleanup_before_a_job_leaves_temp_alone(monkeypatch):
    """_temp already holds the starting job's event.json and file-command
    files when the hook runs; clearing it there breaks that job."""
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return None

    monkeypatch.setattr(job_started.subprocess, "run", run)
    monkeypatch.setenv("RUNNER_CLEANUP_SCOPES", "workspace,temp,engine-build-cache")
    job_started._cleanup()
    assert seen["RUNNER_CLEANUP_SCOPES"].split(",") == ["workspace", "engine-build-cache"]
    monkeypatch.delenv("RUNNER_CLEANUP_SCOPES")
    job_started._cleanup()
    assert "temp" not in seen["RUNNER_CLEANUP_SCOPES"].split(",") and "workspace" in seen["RUNNER_CLEANUP_SCOPES"]


# ---- whose code: the guard runs first (runner_guard.py) ---------------------

from . import origin_cases  # noqa: E402


class TestTheOriginGuardFirst:
    def test_a_refused_job_touches_nothing_else(self, monkeypatch, capsys, tmp_path):
        """Not the SDK, not the disk: an outside fork's job is stopped before
        anything is done for it."""
        done = []
        monkeypatch.setattr(job_started, "restore_tree", lambda *a: done.append("sdk") or 0)
        monkeypatch.setattr(job_started, "check_disk", lambda *a, **k: done.append("disk") or 0)
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        assert job_started.main(env) == job_started.REFUSE
        assert done == []
        assert origin_cases.REFUSED in capsys.readouterr().out

    def test_an_allowed_job_goes_on_to_the_sdk_and_the_disk(self, monkeypatch, capsys, tmp_path):
        done = []
        monkeypatch.setattr(job_started, "restore_tree", lambda *a: done.append("sdk") or 0)
        monkeypatch.setattr(job_started, "check_disk", lambda *a, **k: done.append("disk") or 0)
        env = origin_cases.hook_env(origin_cases.by_id("push"), tmp_path)
        assert job_started.main(env) == 0
        assert done == ["sdk", "disk"]
        assert capsys.readouterr().out.startswith("Origin: push from ")

    def test_a_guard_that_cannot_be_loaded_lets_the_job_run(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(job_started, "GUARD", str(tmp_path / "missing.py"))
        monkeypatch.setattr(job_started, "check_disk", lambda *a, **k: 0)
        env = origin_cases.hook_env(origin_cases.by_id("fork-by-outsider"), tmp_path)
        assert job_started.main(env) == 0
        out = capsys.readouterr().out
        assert "::warning title=Runner guard::" in out and "origin not checked" in out

    def test_the_guard_is_the_one_beside_the_hook(self):
        assert Path(job_started.GUARD).name == "runner_guard.py"
        assert Path(job_started.GUARD).parent == SCRIPT.parent


def _bash():
    import shutil
    bash = shutil.which("bash")
    if os.name == "nt" and (not bash or "system32" in bash.lower()):
        # System32's bash.exe is WSL's launcher: never run a hook in a distro.
        bash = next((p for p in (r"C:\Program Files\Git\bin\bash.exe",
                                 r"C:\Program Files\Git\usr\bin\bash.exe")
                     if os.path.exists(p)), None)
    return bash


@pytest.mark.parametrize("name", origin_cases.END_TO_END)
def test_the_shell_hook_end_to_end_with_an_event(tmp_path, name):
    """job-started.sh as the runner starts it - `bash -e -o pipefail` - with
    the real job_started.py and runner_guard.py beside it, and an event file
    as the runner writes one. A refusal is the hook's exit 1."""
    import shutil
    import subprocess
    import sys
    bash = _bash()
    if not bash:
        pytest.skip("needs a real bash")
    case = origin_cases.by_id(name)
    hook = tmp_path / "runner"
    hook.mkdir()
    for f in ("job-started.sh", "job_started.py", "runner_guard.py"):
        (hook / f).write_text((SCRIPT.parent / f).read_text(encoding="utf-8"),
                              encoding="utf-8", newline="\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    python = Path(sys.executable).as_posix()
    if os.name == "nt":
        python = "/" + python.replace(":", "", 1)
    (fake / "python3").write_text(f'#!/usr/bin/env bash\nexec "{python}" "$@"\n', newline="\n")
    (fake / "python3").chmod(0o755)
    env = origin_cases.hook_env(case, tmp_path, os.environ)
    env.update(RUNNER_DISK_CLEAN_BELOW_GB="0", RUNNER_DISK_WARN_BELOW_GB="0",
               RUNNER_DISK_FAIL_BELOW_GB="0", PYTHONIOENCODING="utf-8",
               RUNNER_ANDROID_PRISTINE=str(tmp_path / "no-sdk"))
    if os.name == "nt":
        env["PATH"] = "/" + fake.as_posix().replace(":", "", 1) + ":/usr/bin:/bin"
    else:
        env["PATH"] = f"{fake}{os.pathsep}{os.environ.get('PATH', '')}"
    done = subprocess.run([bash, "-e", "-o", "pipefail", (hook / "job-started.sh").as_posix()],
                          capture_output=True, env=env, timeout=120)
    out = done.stdout.decode("utf-8")
    assert done.returncode == (0 if case["allowed"] else 1), out + done.stderr.decode()
    for line in case["out"]:
        assert line in out, out
    if case["allowed"]:
        assert "Disk free before the job" in out, "an allowed job still gets its disk check"
    else:
        assert "Disk free before the job" not in out


# ---- a refusal ends the job, for real (Linux) --------------------------------
#
# The runner's process tree rebuilt from bash under the runner's names - a
# link's name is the process's comm, as the runner's own apphost is - with
# the real job-started.sh, job_started.py and runner_guard.py. A refusal must
# end Runner.Worker, so no later step runs, and leave Runner.Listener alone.
# Linux only: it reads the real /proc. Run it in any throwaway container:
#   docker run --rm -v "$PWD:/src" -w /src python:3.13-slim \
#     sh -c "pip -q install pytest && cd agent && python -m pytest -q \
#            tests/test_unit_job_started.py -k ends_runner_worker"

LISTENER = r"""
"$TEST_WORKER" -c '
  bash -e -o pipefail "$TEST_HOOK"
  echo "$?" > "$TEST_SURVIVED"
' &
worker=$!
wait "$worker"
echo "$?" > "$TEST_OUT"
"""


@pytest.mark.skipif(not os.path.isdir("/proc/self") or os.name == "nt",
                    reason="needs Linux /proc")
@pytest.mark.parametrize("name,ended", [("fork-by-outsider", True), ("push", False)])
def test_a_refusal_ends_runner_worker_and_spares_runner_listener(tmp_path, name, ended):
    import shutil
    import subprocess
    case = origin_cases.by_id(name)
    hook = tmp_path / "runner"
    hook.mkdir()
    for f in ("job-started.sh", "job_started.py", "runner_guard.py"):
        (hook / f).write_text((SCRIPT.parent / f).read_text(encoding="utf-8"),
                              encoding="utf-8", newline="\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    bash = os.path.realpath(shutil.which("bash"))
    for name_ in ("Runner.Listener", "Runner.Worker"):
        (bins / name_).symlink_to(bash)
    env = origin_cases.hook_env(case, tmp_path, os.environ)
    env.update(RUNNER_DISK_CLEAN_BELOW_GB="0", RUNNER_DISK_WARN_BELOW_GB="0",
               RUNNER_DISK_FAIL_BELOW_GB="0", RUNNER_GUARD_KILL_DELAY="0",
               RUNNER_ANDROID_PRISTINE=str(tmp_path / "no-sdk"), PYTHONIOENCODING="utf-8",
               TEST_WORKER=str(bins / "Runner.Worker"), TEST_HOOK=str(hook / "job-started.sh"),
               TEST_SURVIVED=str(tmp_path / "survived"), TEST_OUT=str(tmp_path / "worker.status"))
    done = subprocess.run([str(bins / "Runner.Listener"), "-c", LISTENER], env=env,
                          capture_output=True, timeout=120)
    out = done.stdout.decode("utf-8")
    assert done.returncode == 0, out + done.stderr.decode()
    status = (tmp_path / "worker.status").read_text().strip()
    if ended:
        assert origin_cases.REFUSED in out
        assert not (tmp_path / "survived").exists(), "a step after the hook ran: " + out
        assert status == "137", status          # SIGKILL
    else:
        assert "Origin: push from " in out
        assert (tmp_path / "survived").read_text().strip() == "0"
        assert status == "0"
