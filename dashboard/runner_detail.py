"""Read-only introspection of a single runner.

Separate from docker_ops, which owns fleet lifecycle and fleet telemetry. This
module only looks at one runner and never changes anything, so a bug here can
misreport but cannot break a runner.

Owns mask() and SECRET_KEYS because app.py imports this module; importing them
back from app.py would be a circular import.
"""

import json
import time

import docker_ops

SECRET_KEYS = {"GH_TOKEN"}


def mask(value):
    """Show enough to recognise a token, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "•" * 8 + value[-4:]


def _ok(data):
    return {"ok": True, "data": data}


def _err(message):
    return {"ok": False, "error": message or "unknown error"}


def _mask_env(pairs):
    """['K=V', ...] -> {'K': 'V'}, with SECRET_KEYS masked.

    Masking happens here, before the value is ever put in a dict that gets
    serialized. Masking later - in the template or in CSS - would leave the
    real token in the JSON payload.
    """
    env = {}
    for item in pairs or []:
        k, _, v = item.partition("=")
        env[k] = mask(v) if k in SECRET_KEYS else v
    return env


def inspect(name):
    """Container configuration and state, from `docker inspect`."""
    ok, out, err = docker_ops._docker("inspect", name, timeout=10)
    if not ok:
        return _err(err or out)
    try:
        c = json.loads(out)[0]
    except Exception as e:  # noqa: BLE001 - rendered, not raised
        return _err(f"could not parse inspect output: {e}")

    host = c.get("HostConfig") or {}
    state = c.get("State") or {}
    config = c.get("Config") or {}

    nano = host.get("NanoCpus") or 0
    mem = host.get("Memory") or 0

    return _ok({
        "image": config.get("Image", ""),
        "digest": c.get("Image", ""),
        "created": c.get("Created", ""),
        "restart_count": c.get("RestartCount", 0),
        "status": state.get("Status", ""),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "pid": state.get("Pid"),
        # Reported from HostConfig, not from .env: the point is to show what
        # the daemon actually enforced, which is the only way to notice that a
        # limit was set but never applied.
        "cpu_limit": (nano / 1e9) if nano else None,
        "mem_limit_bytes": mem or None,
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
        "privileged": bool(host.get("Privileged")),
        "network_mode": host.get("NetworkMode", ""),
        "ip": (c.get("NetworkSettings") or {}).get("IPAddress", ""),
        "mounts": [{
            "source": m.get("Source", ""),
            "destination": m.get("Destination", ""),
            "mode": m.get("Mode", ""),
            "rw": bool(m.get("RW")),
        } for m in c.get("Mounts") or []],
        "env": _mask_env(config.get("Env")),
    })


def _exec_json_lines(name, args, timeout):
    """Run a docker command inside the runner that emits one JSON object per line."""
    ok, out, err = docker_ops._docker(
        "exec", name, "docker", *args, timeout=timeout)
    if not ok:
        return {"error": err or out or "command failed"}
    items, seen = [], 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        seen += 1
        try:
            row = json.loads(line)
        except Exception:      # noqa: BLE001 - rendered, not raised
            continue
        if not isinstance(row, dict):
            continue           # valid JSON, wrong shape - skip, do not crash
        items.append(row)

    # ...but if there was output and none of it parsed, the format is broken.
    # Returning [] here would be indistinguishable from an empty daemon.
    if seen and not items:
        return {"error": f"could not parse any of {seen} line(s)"}
    return items


def engine(name):
    """What this runner's own Docker daemon is holding.

    Four independent calls. Each is slow enough that this must never be put on
    a poll loop - _inner_df() allows the df call alone 20 seconds.
    """
    ok, out, err = docker_ops._docker(
        "exec", name, "docker", "system", "df", "--format", "json", timeout=20)
    if not ok:
        df = {"error": err or "command failed"}
    else:
        # One object per line, keyed by "Type" - see _inner_df for why this
        # matters. Normalised here so the template carries neither the
        # "Local Volumes" spelling nor the capitalisation.
        wanted = {"Images": "images", "Containers": "containers",
                  "Local Volumes": "volumes", "Build Cache": "build_cache"}
        df = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:      # noqa: BLE001 - rendered, not raised
                continue
            if not isinstance(row, dict):
                continue           # valid JSON, wrong shape - skip, do not crash
            key = wanted.get(row.get("Type", ""))
            if key:
                df[key] = {"size": row.get("Size", "0B"),
                           "reclaimable": row.get("Reclaimable", "0B"),
                           "count": row.get("TotalCount", "0")}
        if not df:
            df = {"error": "could not parse df output"}

    return _ok({
        "df": df,
        "images": _exec_json_lines(
            name, ["images", "--format", "{{json .}}"], 20),
        "containers": _exec_json_lines(
            name, ["ps", "-a", "--format", "{{json .}}"], 15),
        "volumes": _exec_json_lines(
            name, ["volume", "ls", "--format", "{{json .}}"], 15),
    })


DEFAULT_LOG_WINDOW = "5m"


def logs(name, since=""):
    """Log lines newer than `since`, with the cursor to use for the next call.

    Bounded by timestamp rather than `--tail N`: a verbose build can emit
    thousands of lines between two polls, and a fixed tail would silently drop
    everything above it.

    `docker logs --since` is INCLUSIVE, so the line at the cursor comes back
    every time. Lines are filtered with a strict `>` and the caller sees each
    line exactly once. Unstamped lines (from multi-line output) inherit the
    timestamp of the stamped line above them, so they are also filtered by the
    same rule and delivered exactly once.
    """
    window = since or DEFAULT_LOG_WINDOW
    ok, out, err = docker_ops._docker(
        "logs", "--timestamps", "--since", window, name, timeout=15)
    if not ok:
        return _err(err or out)

    lines, cursor, last = [], since, since
    for raw in out.splitlines():
        stamp, sep, text = raw.partition(" ")
        if not sep or "T" not in stamp:
            # No stamp of its own: it belongs to the entry above, so it
            # inherits that entry's time and is filtered by the same rule
            # instead of bypassing the filter and being redelivered forever.
            if since and last <= since:
                continue
            lines.append({"t": "", "text": raw})
            continue
        last = stamp
        if since and stamp <= since:
            continue                       # the inclusive-boundary overlap
        lines.append({"t": stamp, "text": text})
        if stamp > cursor:
            cursor = stamp
    return _ok({"lines": lines, "cursor": cursor})


_cache = {}


def _is_failure(value):
    """Whether a collector result represents a failure rather than an answer."""
    if value is None:
        return True
    return isinstance(value, dict) and value.get("ok") is False


def cached(key, ttl, fn):
    """Memoise a collector result for `ttl` seconds.

    Failures are deliberately not cached. These collectors never raise - they
    return an error value - so caching on return alone would replay a transient
    docker hiccup or GitHub outage as though it were a real answer for the whole
    TTL, and an empty result is indistinguishable from a genuine one.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    if not _is_failure(value):
        _cache[key] = (now, value)
    return value
