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
