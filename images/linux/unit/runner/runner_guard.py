"""Whose code is this job about to run? The first thing a GitHub runner's
job-started hook asks, before any step - checkout included - has run.

The runner group lets every repository in the org use these runners, public
ones included. A pull request from a fork runs the fork's code, workflow
file and all, so a stranger who opens one could run anything on a
self-hosted runner; GitHub's "Approve and run" click was the only barrier.
This refuses such a job unless everyone whose code it is is trusted.

**The rule.** A job is refused when its event payload carries a
`pull_request` whose head is a fork - another repository than the base - that
the org does not own, unless everyone whose code it is is trusted: the
pull request's author, the fork's owner (`head.repo.owner.login`: a member
can open a pull request from an outsider's fork, and the outsider can then
push to it), and on `synchronize` whoever pushed (`sender.login`). A fork
deleted since (`head.repo` null) is refused: its owner cannot be checked.
A fork the org itself owns runs: what is in it was pushed by someone with
write access there. Everything else runs too, with one line that says where
it came from.

A login is trusted when it is in RUNNER_TRUSTED_AUTHORS (comma-separated,
compared as written but without case), or when it is the author's and the
author's `author_association` is OWNER or MEMBER. Not COLLABORATOR: an
outside collaborator is not in the org, and the owner wants nothing from
outside it run here. GitHub reports a member whose org membership is private
as CONTRIBUTOR, which is what the list is for.

**A refusal ends the job.** A failed job-started hook is only a failed
step: the runner goes on to every later step whose `if:` is always(),
failure() or !cancelled(), and a fork writes its own workflow file. So after
the ::error has had time to reach GitHub (RUNNER_GUARD_KILL_DELAY seconds, 5
by default, while the runner waits for this hook), the process running the
job - Runner.Worker, found among this hook's own parents - is killed. Never
Runner.Listener, which takes the next job, and never anything else. When no
Runner.Worker is found, or it cannot be killed, a warning says the job could
not be ended, and the hook still fails.

**Fail-open on our own faults, closed only on a positive answer.** No event
file, one that cannot be read or parsed, a pull request without a readable
head, or a bug in here: a warning, and the job runs. Only a pull request
positively identified as coming from outside is refused.

The runner hands a hook GITHUB_EVENT_NAME and GITHUB_EVENT_PATH from the
job's `github` context, and writes the payload before it starts the hook
(actions/runner: JobHookProvider.RunHook, ScriptHandler). A workflow's own
`env:` does not reach a hook.

Shipped twice, byte for byte: `images/linux/unit/runner/runner_guard.py`
for the Linux unit and `agent/hooks/windows/runner_guard.py` for Windows. The
macOS hook runs `agent/hooks/macos/runner_guard.js`, a line-for-line port;
`agent/tests/origin_cases.py` holds all three to the same answers.

Standard library only: it runs as the runner's account, with nothing of the
agent's.
"""
import json
import os
import re
import signal
import sys
import time

REFUSE = 75

#: What a unit reports about its hook, so the dashboard can tell a runner
#: that carries this check from one made before it. Raise it when the rule
#: changes; macOS's lib.sh carries the same number.
GUARD_VERSION = 1

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})

#: "<em dash> allowed", spelled in ASCII so this file reads the same under any
#: locale.
ALLOWED = "\u2014 allowed"

UNREAD = "::warning title=Runner guard::could not read the event; origin not checked"

_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]")


def shown(value, fallback):
    """`value` as it may appear in the job's log: only what GitHub allows in
    a login or a repository name, so nothing from the payload can end the
    line or start a workflow command of its own."""
    if not isinstance(value, str) or not value:
        return fallback
    return _UNSAFE.sub("?", value)[:100]


def trusted_authors(text):
    return {name.strip().lower() for name in (text or "").split(",") if name.strip()}


def _text(value):
    return value if isinstance(value, str) and value else None


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def origin(payload):
    """What the payload says about where the code comes from:
    `kind` is "none" (no pull request), "same" (a branch of the base
    repository), "fork", "deleted" (a fork deleted since) or "unknown"."""
    pr = payload.get("pull_request")
    facts = {"kind": "none", "base": None, "head": None, "login": None,
             "association": None, "number": None, "org": None, "owner": None,
             "sender": _text(_get(payload, "sender", "login")),
             "action": _text(payload.get("action"))}
    if not isinstance(pr, dict):
        return facts
    head = pr.get("head")
    association = pr.get("author_association")
    number = pr.get("number")
    facts.update(base=(_text(_get(pr, "base", "repo", "full_name"))
                       or _text(_get(payload, "repository", "full_name"))),
                 login=_text(_get(pr, "user", "login")),
                 association=association.upper() if isinstance(association, str) else None,
                 number=number if isinstance(number, int) and not isinstance(number, bool)
                 and number > 0 else None)
    facts["org"] = (_text(_get(pr, "base", "repo", "owner", "login"))
                    or (facts["base"] or "").split("/")[0] or None)
    if not isinstance(head, dict) or "repo" not in head:
        facts["kind"] = "unknown"
    elif head["repo"] is None:
        facts["kind"] = "deleted"
    else:
        name = _text(_get(head, "repo", "full_name"))
        if not name or not facts["base"]:
            facts["kind"] = "unknown"
        else:
            facts["head"] = name
            facts["owner"] = (_text(_get(head, "repo", "owner", "login"))
                              or name.split("/")[0] or None)
            facts["kind"] = "same" if name.lower() == facts["base"].lower() else "fork"
    return facts


def decide(event, payload, env):
    """(allowed, the line to print)."""
    name = shown(event, "an unnamed event")
    facts = origin(payload)
    if facts["kind"] == "unknown":
        return True, UNREAD
    if facts["kind"] == "none":
        repo = shown(_text(_get(payload, "repository", "full_name"))
                     or env.get("GITHUB_REPOSITORY"), "an unknown repository")
        who = shown(_text(_get(payload, "sender", "login")) or env.get("GITHUB_ACTOR"),
                    "an unknown account")
        return True, f"Origin: {name} from {repo} by {who} {ALLOWED}"
    who = shown(facts["login"], "an unknown account")
    where = {"same": shown(facts["base"], "an unknown repository"),
             "fork": "fork " + shown(facts["head"], "an unknown repository"),
             "deleted": "a deleted fork"}[facts["kind"]]
    if facts["kind"] == "same":
        return True, f"Origin: {name} from {where} by {who} {ALLOWED}"
    org_owned = (facts["kind"] == "fork" and facts["org"] and facts["owner"]
                 and facts["owner"].lower() == facts["org"].lower())
    if org_owned:
        return True, f"Origin: {name} from {where} by {who} {ALLOWED}"
    listed = trusted_authors(env.get("RUNNER_TRUSTED_AUTHORS"))
    author = (facts["login"] or "").lower()

    def trusted(login):
        login = (login or "").lower()
        return bool(login) and (login in listed or (
            login == author and facts["association"] in TRUSTED_ASSOCIATIONS))

    # Who is not trusted, and how to say it: the author, the fork's owner,
    # the last pusher - or nobody can tell, for a deleted fork.
    if facts["kind"] == "deleted":
        untrusted, how = None, ", whose owner cannot be checked"
    elif not trusted(facts["login"]):
        untrusted, how = facts["login"], ""
    elif not trusted(facts["owner"]):
        untrusted = facts["owner"]
        how = f", which belongs to {shown(untrusted, 'an unknown account')}"
    elif facts["action"] == "synchronize" and not trusted(facts["sender"]):
        untrusted = facts["sender"]
        how = f", last pushed to by {shown(untrusted, 'an unknown account')}"
    else:
        return True, f"Origin: {name} from {where} by {who} {ALLOWED}"
    org = shown(facts["org"], "the organisation")
    pr = f"Pull request #{facts['number']}" if facts["number"] else "The pull request"
    association = shown(facts["association"], "UNKNOWN")
    line = ("::error title=Outside code refused::Self-hosted runners only run code "
            f"from {org} members and known maintainers. {pr} by {who} ({association}) "
            f"comes from {where}{how}, so this job was stopped before any of its code ran.")
    if facts["kind"] != "deleted":
        named = shown(untrusted, "an unknown account")
        line += (f" If {named} is a maintainer whose org membership is private, add them "
                 "to RUNNER_TRUSTED_AUTHORS.")
    return False, line


# ---- ending the job ---------------------------------------------------------

#: The runner's process that runs one job, and nothing else. Never
#: Runner.Listener: that is the runner itself, which takes the next job.
WORKER_NAMES = ("runner.worker", "runner.worker.exe")
#: What may stand between this guard and the hook the runner started: the
#: Python that runs it (a venv's launcher and the Python it starts, on
#: Windows) and Linux's `timeout`.
HELPER = re.compile(r"^(python[0-9.]*|pythonw?|py|timeout)(\.exe)?$", re.I)
#: The hook the runner starts: job-started.sh under a shell, or on Windows
#: job-started.js under the runner's node.
HOOK_FILES = ("job-started.sh", "job-started.js")
MAX_DEPTH = 64


def _base(name):
    return re.split(r"[\\/]", str(name or ""))[-1]


def find_worker(chain):
    """The pid of the Runner.Worker that started this hook, or None.

    `chain` is (pid, [names], [args]) from this process's parent upwards. It
    must read: at most three helpers (HELPER), then the hook itself - a
    process whose arguments name job-started.sh or .js, or, where arguments
    cannot be read (Windows), a node - and directly above it Runner.Worker,
    by that exact file name. Anything else is not a job-started hook the
    runner started - a test run of the hook inside a job, say - and nothing
    is killed. Never Runner.Listener."""
    def names(entry):
        return [_base(n).lower() for n in entry[1] if n]

    i = 0
    while i < len(chain) and i < 3 and any(HELPER.match(n) for n in names(chain[i])):
        i += 1
    if i >= len(chain):
        return None
    pid, _, args = (tuple(chain[i]) + ([],))[:3]
    is_hook = (any(_base(a) in HOOK_FILES for a in args) if args
               else any(n in ("node", "node.exe") for n in names(chain[i])))
    if not is_hook or i + 1 >= len(chain):
        return None
    worker = chain[i + 1]
    return worker[0] if any(n in WORKER_NAMES for n in names(worker)) else None


def linux_ancestors(pid=None, proc="/proc"):
    """[(pid, [comm, argv0], argv)] of every parent of `pid`, nearest first,
    from /proc. comm is the executable's name; argv0 catches a worker started
    under another one (`exec -a`)."""
    pid = os.getpid() if pid is None else pid
    chain, seen = [], {pid}
    while len(chain) < MAX_DEPTH:
        try:
            with open(f"{proc}/{pid}/status", encoding="utf-8", errors="replace") as handle:
                status = handle.read()
        except OSError:
            break
        found = re.search(r"^PPid:\s*(\d+)", status, re.M)
        parent = int(found.group(1)) if found else 0
        if parent <= 1 or parent in seen:
            break
        names, args = [], []
        try:
            with open(f"{proc}/{parent}/comm", encoding="utf-8", errors="replace") as handle:
                names.append(handle.read().strip())
        except OSError:
            pass
        try:
            with open(f"{proc}/{parent}/cmdline", "rb") as handle:
                args = [a.decode("utf-8", "replace")
                        for a in handle.read().split(b"\0") if a]
            names.extend(args[:1])
        except OSError:
            pass
        chain.append((parent, names, args))
        seen.add(parent)
        pid = parent
    return chain


def windows_ancestors(pid=None, table=None, created=None):
    """[(pid, [exe], [])] of every parent of `pid`, nearest first - Windows
    does not say a process's arguments here. `table` is
    pid -> (parent pid, exe name), `created` a pid's creation time. Windows
    keeps a dead parent's pid in its children and may hand it to a new
    process: a "parent" created after its child is not one, and ends the
    walk."""
    pid = os.getpid() if pid is None else pid
    if table is None:
        table, created = _windows_processes()
    chain, seen = [], {pid}
    while len(chain) < MAX_DEPTH and pid in table:
        parent, _ = table[pid]
        if parent <= 0 or parent in seen or parent not in table:
            break
        mine, theirs = created(pid), created(parent)
        if mine is None or theirs is None or theirs > mine:
            break
        chain.append((parent, [table[parent][1]], []))
        seen.add(parent)
        pid = parent
    return chain


def _windows_processes():
    """(pid -> (parent pid, exe name), pid -> creation time) from a
    Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("no process snapshot")
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    def created(pid):
        handle = kernel32.OpenProcess(0x1000, False, pid)       # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    return table, created


def _kill(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x0001, False, pid)         # PROCESS_TERMINATE
        if not handle:
            raise OSError(f"cannot open process {pid}: error {ctypes.get_last_error()}")
        try:
            if not kernel32.TerminateProcess(handle, 1):
                raise OSError(f"cannot end process {pid}: error {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGKILL)


def _ancestors():
    return windows_ancestors() if os.name == "nt" else linux_ancestors()


def _delay(env):
    try:
        value = float(env.get("RUNNER_GUARD_KILL_DELAY", 5))
    except (TypeError, ValueError):
        value = 5.0
    if value != value:                  # NaN
        value = 5.0
    return min(max(value, 0.0), 30.0)


def end_the_job(env, ancestors=None, kill=None, sleep=None):
    """Kill the Runner.Worker this hook runs under, after the delay that lets
    the ::error reach GitHub. True when it was killed."""
    try:
        worker = find_worker((ancestors or _ancestors)())
        if worker is None:
            raise LookupError("this hook was not started by a Runner.Worker")
        (sleep or time.sleep)(_delay(env))
        (kill or _kill)(worker)
        return True
    except Exception as error:          # noqa: BLE001 - the refusal stands either way
        print("::warning title=Runner guard::the job could not be ended "
              f"({shown(type(error).__name__, 'error')}); steps the workflow marks "
              "always() may still run", flush=True)
        return False


#: Where each run leaves its answer, in the runner's log directory: the agent
#: reads it to tell a runner whose hook has run from one whose never did, or
#: one whose last job's event could not be read (agent/origin_guard.py).
LAST_RESULT = "origin-guard.json"


def record(env, result):
    """Leave `result` - allowed, refused, unread or failed - for the agent.
    Never raises: a record that cannot be written changes no answer."""
    try:
        logs = env.get("RUNNER_LOG_DIR")
        if not logs or not os.path.isdir(logs):
            return
        path = os.path.join(logs, LAST_RESULT)
        text = json.dumps({"version": GUARD_VERSION, "result": result,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        with open(path + ".new", "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        os.replace(path + ".new", path)
    except Exception:                   # noqa: BLE001
        pass


def check(env=None, end=None):
    """0 to let the job run, REFUSE to stop it. Never raises. A refusal also
    ends the job (`end_the_job`)."""
    env = os.environ if env is None else env
    try:
        path = env.get("GITHUB_EVENT_PATH")
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            print(UNREAD)
            record(env, "unread")
            return 0
        allowed, line = decide(env.get("GITHUB_EVENT_NAME"), payload, env)
        print(line, flush=True)
        record(env, "refused" if not allowed else "unread" if line == UNREAD else "allowed")
        if allowed:
            return 0
    except Exception as error:          # noqa: BLE001 - never block every job on our fault
        try:
            print(f"::warning title=Runner guard::the origin check failed "
                  f"({shown(type(error).__name__, 'error')}); origin not checked")
        except Exception:               # noqa: BLE001
            pass
        record(env, "failed")
        return 0
    try:
        (end or end_the_job)(env)
    except Exception as error:          # noqa: BLE001 - the refusal stands
        print("::warning title=Runner guard::the job could not be ended "
              f"({shown(type(error).__name__, 'error')}); steps the workflow marks "
              "always() may still run", flush=True)
    return REFUSE


if __name__ == "__main__":
    sys.exit(check())
