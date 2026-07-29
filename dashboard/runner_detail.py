"""Read-only introspection of a single runner.

Separate from docker_ops, which owns fleet lifecycle and fleet telemetry. This
module only looks at one runner and never changes anything, so a bug here can
misreport but cannot break a runner.

Owns mask() and SECRET_KEYS because app.py imports this module; importing them
back from app.py would be a circular import.
"""

import json

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
