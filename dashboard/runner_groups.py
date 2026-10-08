"""Which GitHub runner group each fleet's runners join, and what it allows.

A self-hosted runner that a public repository can reach runs whatever a
fork's pull request asks of it. Whether one can is not a property of the
runner but of its runner group on GitHub - its visibility and
`allows_public_repositories` - and nothing on the dashboard said which group
a fleet registers into or what that group allows (GitHub #5). This module
reads it so the fleet page can say it, always, for every GitHub fleet.

**Read only.** Nothing here writes to GitHub. Changing a group stays a
decision made on GitHub.

**A cache, refreshed in the background.** `run_forever` reads every group
every `REFRESH_SECONDS` on a daemon thread of its own; a page read only ever
looks at what the last refresh found. A page that called GitHub would be as
slow as GitHub and fail when GitHub does - and it would be called once per
browser tab every few seconds.

**Unknown is an answer.** Before the first read, without a token, when the
read failed, or for a group GitHub does not list, the answer is
`{"known": False, "why": ...}` - shown grey - never a guess that the group
is safe.
"""
import threading
import time
from datetime import datetime, timezone

import providers

REFRESH_SECONDS = 300

_lock = threading.Lock()
_cache = {}


def reset():
    """Forget what was read. For tests, and for nothing else."""
    with _lock:
        _cache.clear()
        _cache.update(groups=None, error=None, at=None, org=None)


reset()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh(env):
    """Read every runner group once and keep the answer. Never raises."""
    env = env or {}
    provider = providers.by_key("github")
    client = provider.forge_client(env) if provider else None
    if client is None:
        with _lock:
            _cache.update(groups=None, at=_now(), org=None,
                          error="no GH_TOKEN and GITHUB_ORG to read them with")
        return
    try:
        groups = client.runner_groups()
        error = None if groups is not None else "GitHub's runner groups could not be read"
    except Exception as e:      # noqa: BLE001 - a refresh never stops the loop
        groups, error = None, f"GitHub's runner groups could not be read: {type(e).__name__}"
    with _lock:
        _cache.update(groups=groups, error=error, at=_now(), org=client.org)


def run_forever(get_env, interval=REFRESH_SECONDS, sleep=time.sleep):
    """The background thread: refresh, wait, repeat. `get_env` is asked
    each time, so a token set in Settings is used without a restart."""
    while True:
        try:
            refresh(get_env())
        except Exception as e:      # noqa: BLE001
            with _lock:
                _cache.update(error=f"runner groups not refreshed: {type(e).__name__}")
        sleep(interval)


def snapshot():
    with _lock:
        return {"groups": [dict(g) for g in _cache["groups"]]
                if _cache["groups"] is not None else None,
                "error": _cache["error"], "at": _cache["at"], "org": _cache["org"]}


def _wanted(fleet, env):
    """(group name or None for the org's default, where that came from) -
    the same order a registration uses: the fleet's own group, then the
    deployment's RUNNER_GROUP, then GitHub's default group."""
    if (fleet.get("runner_group") or "").strip():
        return fleet["runner_group"].strip(), "fleet setting"
    if ((env or {}).get("RUNNER_GROUP") or "").strip():
        return env["RUNNER_GROUP"].strip(), "deployment RUNNER_GROUP"
    return None, "GitHub default group"


def policy_for(fleet, env):
    """The runner group policy a GitHub fleet's runners get, or None for a
    forge that has no runner groups."""
    if (fleet or {}).get("provider") != "github":
        return None
    name, source = _wanted(fleet, env)
    state = snapshot()
    if state["groups"] is None:
        why = state["error"] or "GitHub's runner groups have not been read yet"
        return {"known": False, "group": name, "source": source, "why": why}
    if name is None:
        match = next((g for g in state["groups"] if g.get("default")), None)
    else:
        match = next((g for g in state["groups"]
                      if str(g.get("name") or "").lower() == name.lower()), None)
    if match is None:
        return {"known": False, "group": name, "source": source,
                "why": (f"GitHub lists no runner group named {name}" if name
                        else "GitHub lists no default runner group")}
    return {"known": True, "group": match.get("name"), "source": source,
            "visibility": match.get("visibility"),
            "allows_public_repositories": bool(match.get("allows_public_repositories")),
            "default": bool(match.get("default"))}


def panel(fleets, env):
    """What Settings shows: every group as last read, the GitHub fleets
    that register into each, and when it was read."""
    state = snapshot()
    used = {}
    for fleet in fleets:
        policy = policy_for(fleet, env)
        if policy and policy.get("known"):
            used.setdefault(policy["group"], []).append(fleet["fleet_id"])
    groups = [dict(g, fleets=used.get(g.get("name"), []))
              for g in (state["groups"] or [])]
    return {"groups": groups, "known": state["groups"] is not None,
            "error": state["error"], "at": state["at"],
            "refresh_seconds": REFRESH_SECONDS}
