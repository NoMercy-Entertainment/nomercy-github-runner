"""Whether a GitHub runner's job-started hook refuses outside code, as the
runner's own files say (images/linux/unit/runner/runner_guard.py,
docs/operations/runner-job-hooks.md "Outside code").

Hooks are written into a runner at its create, so the agent's own copy, or an
image rebuilt since, says nothing about a runner made before them; and a job
the guard allowed runs as the account that owns those files. So on each deep
pass (about every five minutes) every runtime reads each GitHub runner's own
copy and reports, per unit:

- `version`: which version of the check its hook carries - 0 when the hooks
  are missing, are from before the check, or differ from the agent's own
  copy (changed since, or written by an older agent: either way not trusted,
  whatever version they claim; the runner's next create puts them right);
- `note`: why, in a few words, when something is wrong;
- `last`: the hook's own record of its last run (origin-guard.json in the
  runner's log directory: allowed, refused, unread or failed, and when), or
  None when the hook has not run since that file was last cleared.

A runtime that cannot read a runner now says nothing (None), and the
controller keeps the last report with the time it was measured.
"""
import json

#: The record each run of the guard leaves in the runner's log directory.
LAST_RESULT = "origin-guard.json"
RESULTS = ("allowed", "refused", "unread", "failed")


def guard_version(text, prefix):
    """The whole number after `prefix` at the start of a line of `text`
    (`GUARD_VERSION = 1` in runner_guard.py, `const GUARD_VERSION = 1;` in
    the macOS runner_guard.js), or 0 when no line says one."""
    for line in str(text or "").splitlines():
        if line.startswith(prefix):
            rest = line[len(prefix):].strip()
            digits = rest[:len(rest) - len(rest.lstrip("0123456789"))]
            if digits and rest[len(digits):] in ("", ";"):
                return int(digits)
    return 0


def last_result(text):
    """{"result", "at"} from a record the hook wrote, or None when it is not
    one."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("result") not in RESULTS \
            or not isinstance(data.get("at"), str) or len(data["at"]) > 40:
        return None
    version = data.get("version")
    return {"result": data["result"], "at": data["at"],
            "version": version if isinstance(version, int) and not isinstance(version, bool)
            else 0}


def _same(a, b):
    return str(a).replace("\r\n", "\n") == str(b).replace("\r\n", "\n")


def report(version, changed=(), missing=False, last=None):
    # A record left by another version of the check - the log directory
    # outlives a recreate - proves nothing about this one.
    if last is not None:
        last = ({"result": last["result"], "at": last["at"]}
                if last.get("version") == version else None)
    if missing:
        return {"version": 0, "note": "the hooks are missing", "last": last}
    if changed:
        return {"version": 0, "last": last,
                "note": "hook files differ from this agent's copy: "
                        + ", ".join(sorted(changed))[:160]}
    if not version:
        return {"version": 0, "note": "hooks from before the check", "last": last}
    return {"version": version, "note": None, "last": last}


def measure_tree(fs, hooks, expected, join, guard_file, prefix, last_path):
    """The report for hooks a create copied from `expected` (name -> text)
    into the directory `hooks`, read through `fs`: every file must still be
    exactly the agent's copy. Raises when the runner cannot be read."""
    last = None
    if last_path and fs.exists(last_path):
        last = last_result(fs.read_text(last_path))
    if not fs.exists(hooks):
        return report(0, missing=True, last=last)
    changed = [name for name, text in expected.items()
               if not fs.exists(join(hooks, name))
               or not _same(fs.read_text(join(hooks, name)), text)]
    return report(guard_version(expected.get(guard_file, ""), prefix), changed, last=last)
