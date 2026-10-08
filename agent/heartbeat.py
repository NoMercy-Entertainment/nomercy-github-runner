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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        result = {}
        for rid in runner_ids:
            try:
                result[rid] = dict(runtime.telemetry(rid) or {})
            except Exception:              # one failed unit is not the fleet
                continue
        return result
    except Exception:                       # noqa: BLE001
        return {}


def _jobs(runtime, runner_ids):
    """Which job each running unit says it has, where the runtime can read
    that. Display only, and never worth a beat: a runtime that cannot say,
    or fails trying, names none."""
    if not runner_ids or not hasattr(runtime, "jobs"):
        return {}
    try:
        return dict(runtime.jobs(runner_ids) or {})
    except Exception:                       # noqa: BLE001
        return {}


#: What a unit's telemetry may say on every beat. The machine's cores and
#: memory and the volume a unit's tree is on are what a meter is a share of
#: when the unit has no limit of its own.
LIGHT_KEYS = ("cpu_percent", "cpu_cores", "host_cores", "host_mem_bytes",
              "mem_used_bytes", "mem_limit_bytes", "mem_swap_limit_bytes",
              "storage_volume_used_bytes", "storage_volume_total_bytes")

#: Each deep probe, the key its own figure goes out as, and the prefix its
#: shared volume's figures do.
PROBES = (("disk_usage", "storage"), ("cache_size", "cache"))


def _depth(runtime, runner_id):
    """Storage and cache of one unit, from the closed probe set: what the
    unit uses, and what bounds it - its own disk's size or its cache's cap
    where it has one, the volume it shares where a probe reported that."""
    out = {}
    for probe, name in PROBES:
        try:
            got = runtime.probe(runner_id, probe) or {}
        except Exception:                   # noqa: BLE001
            continue
        if not (got.get("ok") and isinstance(got.get("value"), int)):
            continue
        stamp = _stamp()
        out[f"{name}_bytes"] = got["value"]
        out[f"{name}_at"] = stamp
        if name == "storage" and got.get("total_bytes") is not None:
            out["storage_total_bytes"] = got["total_bytes"]
        if name == "cache" and got.get("cap_bytes") is not None:
            out["cache_cap_bytes"] = got["cap_bytes"]
        if "volume_total_bytes" in got or "volume_used_bytes" in got:
            out[f"{name}_volume_used_bytes"] = got.get("volume_used_bytes")
            out[f"{name}_volume_total_bytes"] = got.get("volume_total_bytes")
            out[f"{name}_volume_at"] = stamp
    return out


def _with_depth(telemetry, depth):
    """A light measurement with the last deep one added. A figure both
    carry - the volume an appliance runner shares, read every beat and on
    the deep one too - keeps the fresher, light reading when it has one."""
    for key, value in depth.items():
        if telemetry.get(key) is None:
            telemetry[key] = value
    return telemetry


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
    beat["measured_at"] = beat["sent_at"]
    # These are independent reads. On a remote macOS appliance each read
    # crosses an emulated guest SSH connection; serial reads took longer
    # than the dashboard's three-heartbeat freshness window.
    with ThreadPoolExecutor(max_workers=2) as pool:
        capabilities = pool.submit(agent.runtime.capabilities)
        observed_instances = pool.submit(agent.runtime.instances)
        try:
            beat["capabilities"] = dict(capabilities.result() or {})
        except Exception:                   # noqa: BLE001
            beat["capabilities_error"] = True
        try:
            instances = []
            for observed in observed_instances.result():
                unit = {"runner_id": observed["runner_id"],
                        "state": observed.get("state") if observed.get("state") in
                        ("running", "stopped", "absent") else "unknown"}
                proof = observed.get("resource_enforcement")
                if isinstance(proof, dict):
                    unit["resource_enforcement"] = dict(proof)
                instances.append(unit)
            beat["instances"] = instances
        except Exception:                   # noqa: BLE001
            # Not an empty list: that would say "I run nothing".
            beat["instances_error"] = True
    if "instances" in beat:
        # What each unit uses (T-1803): CPU and memory every beat for the
        # running ones, storage and cache on a deep beat for every one.
        running = [u["runner_id"] for u in beat["instances"]
                   if u["state"] == "running"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            used_future = pool.submit(_telemetry, agent.runtime, running)
            jobs_future = pool.submit(_jobs, agent.runtime, running)
            used = used_future.result()
            jobs = jobs_future.result()
        for unit in beat["instances"]:
            t = {k: v for k, v in (used.get(unit["runner_id"]) or {}).items()
                 if k in LIGHT_KEYS}
            if t.get("storage_volume_total_bytes") is not None:
                t["storage_volume_at"] = beat["sent_at"]
            if jobs.get(unit["runner_id"]):
                t["job"] = jobs[unit["runner_id"]]
            if deep:
                _with_depth(t, _depth(agent.runtime, unit["runner_id"]))
            if t:
                t["at"] = beat["sent_at"]
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
        self._sample_ready = threading.Event()
        self._thread = None
        self._measuring = None
        self._deep_thread = None
        self._depths = {}

    def measure_once(self):
        """Measure live units independently of sending and storage probes.

        Reused storage retains its own measurement time. The live sample's
        age starts before collection, so a slow collection is never freshened
        merely by finishing or by another heartbeat being sent.
        """
        try:
            started = time.monotonic()
            built = build(self.agent, self.server)
        except Exception:                   # noqa: BLE001 - the next one may
            return None                     # do better; the beat goes anyway
        for unit in built.get("instances", []):
            depth = self._depths.get(unit["runner_id"])
            if depth:
                _with_depth(unit.setdefault("telemetry", {}), depth)
        self.measured += 1
        self._measured = (started, built)
        self._sample_ready.set()
        return built

    def measure_depth_once(self):
        """Deep probes cannot delay CPU, job, or instance observations."""
        payload = self._measured
        if not payload:
            return
        ids = [unit["runner_id"] for unit in payload[1].get("instances", [])]
        def measure(rid):
            if self._stop.is_set():
                return rid, {}
            return rid, _depth(self.agent.runtime, rid)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(measure, rid) for rid in ids]
            for future in as_completed(futures):
                rid, depth = future.result()
                self._depths[rid] = depth

    def _deep_loop(self):
        while not self._stop.is_set():
            if self._measured:
                started = time.monotonic()
                self.measure_depth_once()
                self._stop.wait(max(self.interval, DEEP_EVERY * self.interval
                                    - (time.monotonic() - started)))
            else:
                self._stop.wait(self.interval)

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
            try:
                self.send_once()
            except Exception:               # noqa: BLE001 - one bad beat
                # This thread is the worker's only way of saying it is
                # here. If it ends, the worker is degraded until somebody
                # restarts the agent, and a degraded worker is sent nothing
                # destructive: the fleet cannot be managed at all. A
                # controller recreated under it cost nine minutes of
                # silence that way (2026-09-20).
                self.failed += 1
            self._sample_ready.wait(self.interval)
            self._sample_ready.clear()

    def _measure_loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.measure_once()
            except Exception:               # noqa: BLE001 - as above: the
                pass                        # next measurement may do better
            self._stop.wait(max(0, self.interval - (time.monotonic() - started)))

    def start(self):
        self._thread = threading.Thread(target=self._loop,
                                        name="agent-heartbeat", daemon=True)
        self._thread.start()
        self._measuring = threading.Thread(target=self._measure_loop,
                                           name="agent-heartbeat-measure",
                                           daemon=True)
        self._measuring.start()
        self._deep_thread = threading.Thread(target=self._deep_loop,
                                             name="agent-heartbeat-storage",
                                             daemon=True)
        self._deep_thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._sample_ready.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)
        if self._measuring:
            # Not waited out: a measurement can be blocked on an engine that
            # is not answering, and the agent must still be able to stop.
            self._measuring.join(timeout=0.1)
