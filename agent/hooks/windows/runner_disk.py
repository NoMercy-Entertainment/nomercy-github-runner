"""The Windows runner's job hooks: make sure there is room for a job before it
starts, and clear the runner's temp once it has ended.

A full disk crashed a runner mid-release with nothing useful in its log
(GitHub #7). Before each job this measures the volume under the runner's work
directory: under a threshold it clears earlier workspaces, lower down it
warns, and lower still it refuses the job, with a line that says why, before
the build can take the runner down with it. The thresholds and the wording
are the Linux unit's (images/linux/unit/runner/job_started.py).

Run by `job-started.js` and `job-completed.js`, which are what GitHub runs,
under its own node: it runs nothing but `.js`, `.sh` and `.ps1` hooks, and
the ARM64 guest's execution policy refuses a `.ps1`. They start this with the
agent's own Python, as `runner_disk.py started` or `runner_disk.py completed`.
Standard library only, and nothing from the agent: it runs as the runner's
account.

Nothing here follows a link. The runner's work folder is reached through a
short junction alias (agent/windows_workspace.py) and pnpm fills node_modules
with junctions; a link is removed as a link, never walked into.

Exit status: 0 to let the job run, REFUSE to fail it on purpose. A fault in
this hook itself is reported as a warning and never fails a job.
"""
import os
import shutil
import stat
import sys

REFUSE = 75
GB = 1000 ** 3

#: Entries of the work directory that are the runner's own and stay: its
#: tool cache, its downloaded actions, the job's temp and its bookkeeping all
#: start with an underscore. HOME is the work directory, so a dot entry is the
#: runner account's profile.
KEPT_PREFIXES = ("_", ".")

#: Kept in _temp: the completion hook is a step, and the runner reads the
#: step's file commands once the hook has finished.
KEPT_IN_TEMP = ("_runner_file_commands",)

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _free_bytes(path):
    # GetDiskFreeSpaceEx on the path itself: the runner's own disk is mounted
    # at its folder, which a drive letter would not find.
    return shutil.disk_usage(path).free


def is_link(path):
    """A symlink, a junction or a mount point: anything that leads elsewhere."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _writable_and_retry(func, path, *_):
    """git leaves .git/objects read-only, and Windows will not delete a
    read-only file: clear the bit and try once more."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def _remove(path):
    """One entry, without following it if it is a link."""
    if is_link(path):
        try:
            os.unlink(path)
        except (IsADirectoryError, PermissionError):
            os.rmdir(path)
    elif os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_writable_and_retry)
        else:                                   # pragma: no cover
            shutil.rmtree(path, onerror=_writable_and_retry)
    else:
        try:
            os.unlink(path)
        except PermissionError:
            _writable_and_retry(os.unlink, path)


def _work(env):
    work = env.get("RUNNER_WORK_DIR")
    if not work or is_link(work) or not os.path.isdir(work):
        return None
    return work


def _under(path, root):
    """The first part of `path` below `root`, or None when it is not below."""
    if not path or not root:
        return None
    path = os.path.normcase(os.path.normpath(path))
    root = os.path.normcase(os.path.normpath(root))
    if not path.startswith(root.rstrip("\\/") + os.sep):
        return None
    return path[len(root.rstrip("\\/")) + 1:].split(os.sep)[0]


def remove_earlier_workspaces(env):
    """Every earlier job's workspace, never the current one. Returns how many
    went. Nothing is removed while it cannot tell which one is current."""
    work = _work(env)
    if not work:
        return 0
    current = env.get("GITHUB_WORKSPACE")
    keep = None
    for root in (work, env.get("RUNNER_JOB_WORK_DIR")):
        keep = keep or _under(current, root)
    if not keep:
        print("The job's workspace is not under the runner's work directory; "
              "earlier workspaces were left alone")
        return 0
    removed = 0
    for name in os.listdir(work):
        path = os.path.join(work, name)
        if (name.startswith(KEPT_PREFIXES) or os.path.normcase(name) == keep
                or is_link(path) or not os.path.isdir(path)):
            continue
        try:
            _remove(path)
            removed += 1
        except OSError as error:
            print(f"{name} could not be removed: {error}")
    return removed


def clear_temp(env):
    """What the runner left in _temp, except the hook step's own file
    commands. Returns how many entries went; one in use is skipped."""
    work = _work(env)
    temp = work and os.path.join(work, "_temp")
    if not temp or is_link(temp) or not os.path.isdir(temp):
        return 0
    removed = 0
    for name in os.listdir(temp):
        if name in KEPT_IN_TEMP:
            continue
        try:
            _remove(os.path.join(temp, name))
            removed += 1
        except OSError:
            continue
    return removed


def check_disk(paths, env, free_bytes=_free_bytes, cleanup=lambda: None,
               clearing="earlier workspaces"):
    """0, or REFUSE when even after cleaning there is less than the floor."""
    def threshold(name, default):
        return float(env.get(name, default)) * GB

    clean_below = threshold("RUNNER_DISK_CLEAN_BELOW_GB", 15)
    warn_below = threshold("RUNNER_DISK_WARN_BELOW_GB", 10)
    fail_below = threshold("RUNNER_DISK_FAIL_BELOW_GB", 3)

    def lowest():
        seen = []
        for path in paths:
            try:
                seen.append(free_bytes(path))
            except OSError:
                continue
        return min(seen) if seen else None

    free = lowest()
    if free is None:
        return 0
    print(f"Disk free before the job: {free / GB:.0f} GB (lowest of {', '.join(paths)})")
    if free < clean_below:
        print(f"Under {clean_below / GB:.0f} GB: clearing {clearing}")
        try:
            cleanup()
        except Exception as error:      # noqa: BLE001 - the check still decides
            print(f"cleanup did not finish: {error}")
        free = lowest() or free
        print(f"Disk free after cleanup: {free / GB:.0f} GB")
    if free < fail_below:
        print(f"::error title=Runner disk full::only {free / GB:.1f} GB free on this runner "
              f"(needs {fail_below / GB:.0f} GB). The job was stopped before it could crash "
              f"the runner; re-run it, it will land elsewhere or after a cleanup.")
        return REFUSE
    if free < warn_below:
        print(f"::warning title=Runner disk low::only {free / GB:.0f} GB free on this runner.")
    return 0


def started(env):
    work = env.get("RUNNER_WORK_DIR")
    if not work:
        return 0
    # _temp is left to the runner here: it empties it itself when a job
    # starts, and what is in it now is this job's event payload.
    return check_disk([work], env, cleanup=lambda: remove_earlier_workspaces(env))


def completed(env):
    clear_temp(env)
    return 0


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    stage = argv[0] if argv else ""
    try:
        if stage == "started":
            return started(env)
        if stage == "completed":
            return completed(env)
        print(f"::warning title=Runner hook::unknown hook stage {stage!r}")
    except Exception as error:          # noqa: BLE001 - never fail a job on our fault
        what = "disk check" if stage == "started" else "cleanup"
        print(f"::warning title=Runner hook::{what} failed: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
