"""Cleanup operates only on old owned files between jobs, and is bounded."""
import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "images/linux/unit/runner/cleanup.py"
spec = importlib.util.spec_from_file_location("unit_cleanup", SCRIPT)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def old(path, now=1000000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("important data")
    os.utime(path, (now - 1000, now - 1000))
    return path


def test_cleanup_preserves_current_recent_and_foreign_trees(tmp_path):
    root = tmp_path / "work"
    outdated = old(root / "old" / "artifact")
    current = old(root / "current" / "source")
    recent = old(root / "recent" / "file", now=1001000)
    foreign = old(tmp_path / "other-runner" / "artifact")
    for directory in (outdated.parent, current.parent, recent.parent):
        os.utime(directory, (999000, 999000))
    cleanup.remove_old(root, 100, [current.parent], now=1000000)
    assert not outdated.exists()
    assert current.exists() and recent.exists() and foreign.exists()


def test_cleanup_does_not_traverse_links(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    foreign = old(tmp_path / "other-runner" / "artifact")
    try:
        (root / "link").symlink_to(foreign.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    cleanup.remove_old(root, 100, now=1000000)
    assert foreign.exists() and (root / "link").is_symlink()


def test_live_nested_container_prevents_all_cleanup(tmp_path, monkeypatch):
    artifact = old(tmp_path / "old-file")
    monkeypatch.setattr(cleanup, "docker", lambda *args: "container-still-running")
    cleanup.cleanup({"RUNNER_WORK_DIR": str(tmp_path)})
    assert artifact.exists()


def test_cache_policy_cap_and_scopes_are_honored(monkeypatch):
    calls = []
    def docker(*args):
        calls.append(args)
        return ""
    monkeypatch.setattr(cleanup, "docker", docker)
    cleanup.cleanup({"RUNNER_CLEANUP_SCOPES": "engine-build-cache",
                     "RUNNER_BUILD_CACHE_GC": "123456B"})
    assert calls == [("ps", "-q"), ("builder", "prune", "-af", "--filter",
                                    "until=168h", "--keep-storage", "123456B")]
    calls.clear()
    cleanup.cleanup({"RUNNER_CLEANUP_ENABLED": "0"})
    assert calls == []
