"""Bounded cleanup at startup or the synchronous GitHub completion hook.

Never called by an independent timer while a listener can accept jobs.
All file deletion stays in this unit's work/log roots, without following links.
"""
import os
from pathlib import Path
import shutil
import subprocess
import time


def remove_old(root, age_seconds, protected=(), now=None):
    """Remove only complete old trees, never links, mountpoints or protected paths."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return
    cutoff = (time.time() if now is None else now) - age_seconds
    protected = [Path(p).resolve() for p in protected if p]
    for child in root.iterdir():
        resolved = child.resolve()
        if child.is_symlink() or child.is_mount() or any(
                resolved == p or resolved in p.parents or p in resolved.parents
                for p in protected):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                eligible = True
                for base, dirs, files in os.walk(child, followlinks=False):
                    entries = [Path(base) / item for item in dirs + files]
                    if any(p.is_symlink() or p.is_mount() or
                           p.stat().st_mtime >= cutoff for p in entries):
                        eligible = False
                        break
                if eligible:
                    shutil.rmtree(child)
            elif child.is_file():
                child.unlink()
        except FileNotFoundError:
            continue


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=120, check=True).stdout.strip()


def cleanup(env=None):
    env = os.environ if env is None else env
    if env.get("RUNNER_CLEANUP_ENABLED", "1") != "1":
        return
    scopes = set(env.get("RUNNER_CLEANUP_SCOPES",
                        "workspace,temp,diagnostics,engine-build-cache,engine-images-unused").split(","))
    # Containers that outlived the job may still own workspace files. Docker
    # itself can safely decide which build layers and images are unused.
    live_containers = bool(docker("ps", "-q"))
    days = max(1, int(env.get("RUNNER_CLEANUP_RETENTION_DAYS", "7")))
    work = Path(env.get("RUNNER_WORK_DIR", "/runner/work"))
    logs = Path(env.get("RUNNER_LOG_DIR", "/runner/logs"))
    current = env.get("GITHUB_WORKSPACE")
    if "workspace" in scopes and not live_containers:
        protected = [work / p for p in ("_tool", "_actions", "_temp", ".unit-tmp", ".home")]
        # The completed job's own workspace is still used by the runner while
        # the hook runs. Clear previous jobs now, without an age threshold.
        remove_old(work, 0, [*protected, current])
    if "temp" in scopes and not live_containers:
        for path in (work / ".unit-tmp", work / "_temp"):
            remove_old(path, 0, [current])
    if "diagnostics" in scopes:
        remove_old(logs / "diagnostics", days * 86400)
    if "engine-build-cache" in scopes:
        docker("builder", "prune", "-af")
    if "engine-images-unused" in scopes:
        docker("image", "prune", "-af")


if __name__ == "__main__":
    cleanup()
