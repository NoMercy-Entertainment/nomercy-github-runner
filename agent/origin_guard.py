"""Which version of the job-started hook's origin check a runner's own hook
carries (images/linux/unit/runner/runner_guard.py, docs/operations/
runner-job-hooks.md "Outside code").

Hooks are written into a runner's tree at its create, so the agent's own copy
says nothing about a runner made before it: each runtime reads the runner's
copy and reports the number with the unit in every heartbeat. 0 is hooks from
before the check.
"""


def guard_version(text, prefix):
    """The whole number after `prefix` at the start of a line of `text`
    (`GUARD_VERSION = 1` in runner_guard.py, `ORIGIN_GUARD_VERSION=1` in
    the macOS lib.sh), or 0 when no line says one."""
    for line in str(text or "").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value.isdigit():
                return int(value)
    return 0
