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
import time
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

#: How old a measurement may be and still go out with a beat. Beyond this a
#: beat carries none: the controller acts on what a beat says about a unit,
#: and a state from a minute ago is not evidence about it now.
STALE_AFTER = 3 * INTERVAL


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


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minimal(agent, server=None):
    """A beat with everything that costs nothing to know.

    Sent while the agent is still measuring: it says the worker is here,
    which is what the controller's three-beat window is for, and says
    nothing about the units, which is what it does not know at this moment.
    A beat that mentions no unit leaves every unit alone (13.4), so this
    costs the controller nothing but its own patience.
    """
    return {
        "host_id": agent.host_id,
        "agent_version": agent.version,
        "protocol_major": protocol.PROTOCOL_MAJOR,
        "served": sorted(agent.permitted),
        "sent_at": _stamp(),
        "measuring": True,
        "counters": {
            "refused_connections": len(server.refusals) if server else 0,
        },
    }


def build(agent, server=None, deep=False):
    """One heartbeat's payload. `deep` adds each unit's storage and cache."""
    beat = {
        "host_id": agent.host_id,
        "agent_version": agent.version,
        "protocol_major": protocol.PROTOCOL_MAJOR,
        "served": sorted(agent.permitted),
        "sent_at": _stamp(),
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
        self.measured = 0
        #: (monotonic, payload) of the last measurement that finished.
        self._measured = None
        self._stop = threading.Event()
        self._thread = None
        self._measuring = None

    def measure_once(self):
        """Build one measured payload and keep it. The first measurement,
        and every DEEP_EVERY-th after it, also measures storage and cache.

        Called from its own thread, because this is the slow half: `docker
        stats` over every running unit, and a deep pass asks each unit's
        nested engine. A beat that waited for it went silent for minutes on
        a busy worker, and silence is what the controller reads as an
        absence (2026-09-20).
        """
        deep = self.measured % DEEP_EVERY == 0
        try:
            built = build(self.agent, self.server, deep=deep)
        except Exception:                   # noqa: BLE001 - the next one may
            return None                     # do better; the beat goes anyway
        self.measured += 1
        self._measured = (time.monotonic(), built)
        return built

    def payload(self):
        """What the next beat carries: the last measurement while it is
        fresh, else a beat that says the agent is still measuring."""
        got = self._measured
        if got and time.monotonic() - got[0] <= STALE_AFTER:
            return dict(got[1], sent_at=_stamp())
        return minimal(self.agent, self.server)

    def send_once(self):
        """Send one beat. True when the controller took it."""
        ok = self.link.post(self.path, self.payload())
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    def _loop(self):
        while not self._stop.is_set():
            self.send_once()
            self._stop.wait(self.interval)

    def _measure_loop(self):
        while not self._stop.is_set():
            self.measure_once()
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop,
                                        name="agent-heartbeat", daemon=True)
        self._thread.start()
        self._measuring = threading.Thread(target=self._measure_loop,
                                           name="agent-heartbeat-measure",
                                           daemon=True)
        self._measuring.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)
        if self._measuring:
            # Not waited out: a measurement can be blocked on an engine that
            # is not answering, and the agent must still be able to stop.
            self._measuring.join(timeout=0.1)
