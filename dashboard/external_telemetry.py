"""Telemetry for the Forgejo runners that are not containers on this engine.

Fetched from the exporter on BEAST-UNIT, which is the only host that can
reach both of them (see docs/superpowers/specs/2026-09-17-external-runner-
telemetry-design.md). This module is only the client: one cached GET, and
None whenever the answer is not trustworthy.

The caching rules here are not a matter of taste. They are the ones
docker_ops._forge_records() arrived at after this exact shape of bug:

  * A failure is cached as None, never as the previous good answer. A stale
    reading on a status page is worse than a blank one, because nothing on
    the page tells the operator it is stale.
  * A failure backs off LONGER than a success, or an unreachable exporter is
    retried on every 5s collector sweep and pays its full timeout each time.
  * The deadline is taken AFTER the call returns. Taken before, a call slower
    than the TTL stores a deadline already in the past and never caches
    anything at all.

That matters more here than it looks: the collector sweep this runs on also
carries the GitHub fleet's telemetry, so a wedged exporter would stall
runners it has nothing to do with.
"""
import json
import time
import urllib.error
import urllib.request

# Same shape as _FORGE_STATUS_TTL/_FORGE_STATUS_FAIL_BACKOFF in docker_ops.
#
# The timeout is 10s, not the 5s this started with. Measured, the exporter
# answered in 3.2-3.9s while it still sampled on the request, so a sweep under
# load crossed 5s, and a failure is cached for longer than a success - one slow
# sweep blanked both cards for fifteen seconds. The exporter samples in the
# background now and answers in about a millisecond, so this margin should
# never be reached; it is here so that an exporter running an older build, or
# one briefly slowed by a restart, degrades to a late answer rather than to a
# blank card. FAIL_BACKOFF still bounds what a genuinely hung exporter costs
# the sweep that also carries the GitHub fleet: one timeout per 20s, not one
# per 5s poll.
TIMEOUT = 10
TTL = 10
FAIL_BACKOFF = TIMEOUT + TTL

_cache = None  # (deadline, value) or None


def reset_cache():
    """Drop the cache. For tests; nothing in the app needs to call this."""
    global _cache
    _cache = None


def _fetch(url):
    """The exporter's payload, or None. Never raises.

    A transport error, an HTTP error, a timeout and a body that is not JSON
    are the same answer to the caller: we could not ask.
    """
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _runners_of(payload):
    """The runners map of a payload, or None if it is not one."""
    if not isinstance(payload, dict):
        return None
    runners = payload.get("runners")
    return runners if isinstance(runners, dict) else None


def telemetry(env):
    """{runner name: metrics or None}, or None if the exporter is unavailable.

    None at the top level means "not configured, or could not ask"; None for a
    single runner means the exporter answered but that probe could not.
    """
    global _cache
    url = (env.get("EXTERNAL_EXPORTER_URL") or "").strip()
    if not url:
        return None
    now = time.monotonic()
    if _cache is not None and now < _cache[0]:
        return _cache[1]
    got = _runners_of(_fetch(url))
    ttl = TTL if got is not None else FAIL_BACKOFF
    _cache = (time.monotonic() + ttl, got)
    return got
