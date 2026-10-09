"""Whose code is this job about to run? The first thing a GitHub runner's
job-started hook asks, before any step - checkout included - has run.

The runner group lets every repository in the org use these runners, public
ones included. A pull request from a fork runs the fork's code, workflow
file and all, so a stranger who opens one could run anything on a
self-hosted runner; GitHub's "Approve and run" click was the only barrier.
This refuses such a job unless its author is trusted.

**The rule.** A job is refused when its event payload carries a
`pull_request` whose head is a fork - another repository than the base, or
one deleted since (`head.repo` null) - and whose author is not trusted.
Trusted is `author_association` OWNER or MEMBER, or a login in
RUNNER_TRUSTED_AUTHORS (comma-separated, case-insensitive). Not COLLABORATOR:
an outside collaborator is not in the org, and the owner wants nothing from
outside it run here. GitHub reports a member whose org membership is private
as CONTRIBUTOR, which is what the list is for. Everything else runs, with one line that says where it came
from.

**Fail-open on our own faults, closed only on a positive answer.** No event
file, one that cannot be read or parsed, a pull request without a readable
head, or a bug in here: a warning, and the job runs. Only a fork pull
request from an author nobody vouched for is refused.

The runner hands a hook GITHUB_EVENT_NAME and GITHUB_EVENT_PATH from the
job's `github` context, and writes the payload before it starts the hook
(actions/runner: JobHookProvider.RunHook, ScriptHandler). A workflow's own
`env:` does not reach a hook.

Shipped twice, byte for byte: `images/linux/unit/runner/runner_guard.py`
for the Linux unit and `agent/hooks/windows/runner_guard.py` for Windows. The
macOS hook (`agent/hooks/macos/lib.sh`) applies the same rule with the same
wording; `agent/tests/origin_cases.py` holds all three to it.

Standard library only: it runs as the runner's account, with nothing of the
agent's.
"""
import json
import os
import re
import sys

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
             "association": None, "number": None}
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
    trusted = (facts["association"] in TRUSTED_ASSOCIATIONS
               or (facts["login"] or "").lower() in
               trusted_authors(env.get("RUNNER_TRUSTED_AUTHORS")))
    if trusted:
        return True, f"Origin: {name} from {where} by {who} {ALLOWED}"
    org = shown((facts["base"] or "").split("/")[0], "the organisation")
    pr = f"Pull request #{facts['number']}" if facts["number"] else "The pull request"
    association = shown(facts["association"], "UNKNOWN")
    return False, ("::error title=Outside code refused::Self-hosted runners only run code "
                   f"from {org} members and known maintainers. {pr} by {who} ({association}) "
                   f"comes from {where}, so this job was stopped before any of its code ran. "
                   f"If {who} is a maintainer whose org membership is private, add them to "
                   "RUNNER_TRUSTED_AUTHORS.")


def check(env=None):
    """0 to let the job run, REFUSE to stop it. Never raises."""
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
            return 0
        allowed, line = decide(env.get("GITHUB_EVENT_NAME"), payload, env)
        print(line)
        return 0 if allowed else REFUSE
    except Exception as error:          # noqa: BLE001 - never block every job on our fault
        try:
            print(f"::warning title=Runner guard::the origin check failed "
                  f"({shown(type(error).__name__, 'error')}); origin not checked")
        except Exception:               # noqa: BLE001
            pass
        return 0


if __name__ == "__main__":
    sys.exit(check())
