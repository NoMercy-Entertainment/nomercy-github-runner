"""The GitHub job-started hook: refuse code from outside the org, put back
what an earlier job took, then make sure there is room for this one.

First, whose code it is (runner_guard.py, beside this file): a pull request
whose code comes from a repository owned by anyone but the org or a trusted
owner is refused, and the job ended, before anything else is done for it.

A unit's root is writable and outlives its jobs, so whatever one job deletes
from the image stays deleted for every job after it. nomercy-whisper-models
runs jlumbroso/free-disk-space with `android: true`, which is
`rm -rf /usr/local/lib/android`, and every runner it landed on lost the
Android SDK until it was recreated (GitHub #13, 2026-10-04). The image keeps
a copy no workflow knows about, and missing files are copied back from it.
Files a job changed or added are left as they are.

A full disk crashed a runner mid-release with nothing useful in its log
(GitHub #7). Under a threshold this clears what the completion hook clears,
warns lower down, and refuses the job lower still, with a line that says why,
before the build can take the runner down with it.

The same writable root lets a job change which PHP the unit answers with.
nomercy-tv's `ci` asked shivammathur/setup-php for 8.4 (2026-10-28); on a
self-hosted runner that is an apt install from the PPA and
`update-alternatives --set php /usr/bin/php8.4`, and every job after it on
that unit got 8.4 where the fleet runs 8.5. The alternatives are put back to
the fleet's series before each job; the foreign packages may stay, they are
harmless once nothing points at them.

Exit status: 0 to let the job run, REFUSE to fail it on purpose. A fault in
this hook itself is reported as a warning and never fails a job.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

REFUSE = 75
GB = 1000 ** 3

PRISTINE = "/opt/nomercy/android-sdk.pristine"
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_guard.py")
LIVE = "/usr/local/lib/android"
DISK_PATHS = ("/runner/work", "/")

#: The PHP series the fleet runs (owner's rule of 2026-10-27: 8.5 only) and
#: the alternatives Debian's php packages register for it.
PHP_SERIES = "8.5"
PHP_ALTERNATIVES = ("php", "phar", "phar.phar", "php-config", "phpize")


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False).returncode


def restore_php(series, root="/", run=_run):
    """Point every registered PHP alternative at `series` again. Returns the
    names that were switched. A link nobody registered is left alone, and so
    is one whose `series` binary is missing: nothing is invented."""
    root = Path(root)
    switched = []
    for name in PHP_ALTERNATIVES:
        link = root / "etc" / "alternatives" / name
        if not link.is_symlink():
            continue
        wanted = root / "usr" / "bin" / f"{name}{series}"
        if os.path.basename(os.readlink(link)) == wanted.name:
            continue
        if not wanted.is_file():
            continue
        if run(["update-alternatives", "--set", name, str(wanted)]) == 0:
            switched.append(name)
    return switched


def restore_tree(pristine, live):
    """Copy every file and link `pristine` has and `live` lacks. Returns how
    many were copied. Never overwrites, never deletes, never follows links."""
    pristine, live = Path(pristine), Path(live)
    if not pristine.is_dir() or pristine.is_symlink():
        return 0
    restored = 0
    for base, dirs, files in os.walk(pristine, followlinks=False):
        target = live / Path(base).relative_to(pristine)
        if os.path.lexists(target) and (target.is_symlink() or not target.is_dir()):
            dirs[:] = []            # something else is there now; leave it
            continue
        if not target.exists():
            target.mkdir(parents=True)
            shutil.copystat(base, target)
        for name in dirs + files:
            src, dst = Path(base) / name, target / name
            if os.path.lexists(dst):
                continue
            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
                restored += 1
            elif src.is_file():
                shutil.copy2(src, dst)
                restored += 1
    return restored


def _free_bytes(path):
    return shutil.disk_usage(path).free


#: cleanup.py's own default, when the fleet's cache policy names none.
CLEANUP_SCOPES = "workspace,temp,diagnostics,engine-build-cache,engine-images-unused"


def _cleanup():
    """The completion hook's cleanup, less `temp`: when this hook runs, _temp
    already holds the starting job's event.json and file-command files."""
    env = dict(os.environ)
    scopes = env.get("RUNNER_CLEANUP_SCOPES") or CLEANUP_SCOPES
    env["RUNNER_CLEANUP_SCOPES"] = ",".join(
        s for s in scopes.split(",") if s and s != "temp")
    subprocess.run(["/runner/cleanup"], timeout=360, check=False, env=env)


def check_disk(paths, env, free_bytes=_free_bytes, cleanup=_cleanup):
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
        print(f"Under {clean_below / GB:.0f} GB: clearing earlier workspaces and unused build cache")
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


def check_origin(env, guard=None):
    """runner_guard.check, loaded from beside this file: 0 or REFUSE. A guard
    that cannot be loaded lets the job run, with a warning."""
    try:
        spec = importlib.util.spec_from_file_location("runner_guard", guard or GUARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.check(env)
    except Exception as error:          # noqa: BLE001 - never fail a job on our fault
        print(f"::warning title=Runner guard::the origin check could not be loaded "
              f"({type(error).__name__}); origin not checked")
        return 0


def main(env=None):
    env = os.environ if env is None else env
    if check_origin(env) == REFUSE:
        return REFUSE
    try:
        restored = restore_tree(env.get("RUNNER_ANDROID_PRISTINE", PRISTINE),
                                env.get("RUNNER_ANDROID_LIVE", LIVE))
        if restored:
            print(f"::warning title=Android SDK restored::an earlier job on this runner "
                  f"deleted {restored} Android SDK files; they were put back before this job.")
    except Exception as error:          # noqa: BLE001 - never fail a job on our fault
        print(f"::warning title=Runner hook::Android SDK check failed: {error}")
    try:
        series = env.get("RUNNER_PHP_SERIES", PHP_SERIES)
        switched = restore_php(series)
        if switched:
            print(f"::warning title=PHP restored::an earlier job on this runner switched "
                  f"{', '.join(switched)} to another PHP; put back to {series} before this job.")
    except Exception as error:          # noqa: BLE001 - never fail a job on our fault
        print(f"::warning title=Runner hook::PHP check failed: {error}")
    try:
        return check_disk(DISK_PATHS, env)
    except Exception as error:          # noqa: BLE001
        print(f"::warning title=Runner hook::disk check failed: {error}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
