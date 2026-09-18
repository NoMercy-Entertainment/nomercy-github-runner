"""The agent's heartbeat: every ten seconds, what it is and what it runs.

Design 13.4. Each beat carries the agent's version and protocol, what its
runtime can do, which verbs this worker serves, what it observes of each of its
execution units, and a few counters. Three missed beats and the controller
marks the worker degraded - and a degraded worker is sent nothing destructive.

**A beat never claims more than the agent knows.** If the runtime cannot list
its units, the beat says so and lists none, rather than an empty list: an empty
list would read as "I run nothing", which the controller could take as every
unit having gone. A unit whose state the runtime cannot tell is reported as
`unknown`, which the controller keeps as unknown.

**It sends nothing until it has proved who it is talking to.** The receiver's
certificate must chain to the control plane's authority and name the
controller. The beat carries no secret, but it does tell whoever receives it
which runners live here, and that is not for anyone else.

A failed beat is not retried: the next one is ten seconds away (design 17.2).
"""
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import protocol
from .link import ControllerLink

INTERVAL = 10
TIMEOUT = 5


#: Every how many beats the storage and cache of each unit are measured as
#: well - about every five minutes. They mean walking a directory tree or
#: asking a nested engine, which is too slow to do every ten seconds.
DEEP_EVERY = 30


def _telemetry(runtime, runner_ids):
    """CPU and memory of the running units, all at once where the runtime can
    do that. A unit it could not read is left out - unknown - rather than
    reported as idle at zero."""
    if not runner_ids:
        return {}
    try:
        if hasattr(runtime, "telemetry_all"):
            return dict(runtime.telemetry_all(runner_ids) or {})
        return {rid: dict(runtime.telemetry(rid) or {}) for rid in runner_ids}
    except Exception:                       # noqa: BLE001
        return {}


def _depth(runtime, runner_id):
    """Storage and cache of one unit, from the closed probe set."""
    out = {}
    for probe, key in (("disk_usage", "storage_bytes"),
                       ("cache_size", "cache_bytes")):
        try:
            got = runtime.probe(runner_id, probe) or {}
        except Exception:                   # noqa: BLE001
            continue
        if got.get("ok") and isinstance(got.get("value"), int):
            out[key] = got["value"]
    return out


def build(agent, server=None, deep=False):
    """One heartbeat's payload. `deep` adds each unit's storage and cache."""
    beat = {
        "host_id": agent.host_id,
        "agent_version": agent.version,
        "protocol_major": protocol.PROTOCOL_MAJOR,
        "served": sorted(agent.permitted),
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        beat["capabilities"] = dict(agent.runtime.capabilities() or {})
    except Exception:                       # noqa: BLE001
        beat["capabilities_error"] = True
    try:
        beat["instances"] = [
            {"runner_id": u["runner_id"],
             "state": u.get("state") if u.get("state") in
             ("running", "stopped", "absent") else "unknown"}
            for u in agent.runtime.instances()]
    except Exception:                       # noqa: BLE001
        # Not an empty list: that would say "I run nothing".
        beat["instances_error"] = True
    else:
        # What each unit uses (T-1803): CPU and memory every beat for the
        # running ones, storage and cache on a deep beat for every one.
        running = [u["runner_id"] for u in beat["instances"]
                   if u["state"] == "running"]
        used = _telemetry(agent.runtime, running)
        for unit in beat["instances"]:
            t = {k: v for k, v in (used.get(unit["runner_id"]) or {}).items()
                 if k in ("cpu_percent", "mem_used_bytes", "mem_limit_bytes",
                          "root_disk_used_bytes", "root_disk_total_bytes")}
            if deep:
                t.update(_depth(agent.runtime, unit["runner_id"]))
            if t:
                unit["telemetry"] = t
    beat["counters"] = {
        "refused_connections": len(server.refusals) if server else 0,
    }
    return beat


class HeartbeatSender:
    def __init__(self, agent, url, ssl_context, server=None,
                 interval=INTERVAL, timeout=TIMEOUT):
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError("heartbeats go over https only")
        self.agent = agent
        self.link = ControllerLink(f"https://{parts.netloc}", ssl_context,
                                   timeout=timeout)
        self.path = parts.path or protocol.HEARTBEAT_PATH
        self.server = server
        self.interval = interval
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self._stop = threading.Event()
        self._thread = None

    def send_once(self):
        """Send one beat. True when the controller took it. The first, and
        every DEEP_EVERY-th after it, also measures storage and cache."""
        deep = (self.sent + self.failed) % DEEP_EVERY == 0
        ok = self.link.post(self.path, build(self.agent, self.server,
                                             deep=deep))
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    def _loop(self):
        while not self._stop.is_set():
            self.send_once()
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop,
                                        name="agent-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)
