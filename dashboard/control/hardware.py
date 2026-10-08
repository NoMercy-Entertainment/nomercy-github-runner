"""What a worker's hardware is, and whether a CPU or memory limit fits on it.

The maximum a limit may be is the machine the worker runs on, as its agent
reports it: logical CPUs and physical memory. Read in one place, because four
callers ask - Settings when a fleet default is saved, a runner's own override,
placement, and the pages that show "Max" beside each field - and four readings
of the same capabilities would disagree the first time a key moved.

**Which key wins.** The agent's `hardware` report (`logical_cpus`,
`memory_bytes`) first, measured on the machine itself. Before that report
existed a worker already said `host_cores` (what a pinned window is cut from)
and, on Linux, `memory_total_bytes` (MemTotal) - both measured too. Last, a
worker that measures nothing may still declare a `memory_bytes` budget in its
configuration: placement already refuses anything above it, so it bounds a
limit just as well, but it is a number somebody typed, and it is labelled
"declared budget" wherever it is shown.

**Unknown is not small.** A worker that reports nothing - the macOS appliance
host, a Windows guest's RAM until its agent learns to say it - is unknown, and
a limit checked against unknown hardware is accepted and flagged
(`UNVERIFIED`), never refused: refusing would block every fleet whose agent is
older than this check.
"""
from collections.abc import Mapping

from . import placement

GIB = 1024**3

#: `check()`'s answer when nothing refuses the limit but nothing proves it
#: fits either.
UNVERIFIED = "unverified"


def _count(value):
    return value if type(value) is int and value > 0 else None


def gib(value):
    """Bytes as the page writes them: 78.6 GiB, 32 GiB."""
    text = f"{value / GIB:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + " GiB"


def cores(value):
    number = float(value)
    shown = f"{int(number)}" if number.is_integer() else f"{number:g}"
    return f"{shown} core" + ("" if number == 1 else "s")


def of_worker(worker):
    """{host_id, cpus, cpus_source, memory_bytes, memory_source}, each None
    when the worker has not said."""
    report = placement.declared(worker, "hardware")
    report = report if isinstance(report, Mapping) else {}
    cpus = cpus_source = memory = memory_source = None
    for value in (report.get("logical_cpus"), placement.declared(worker, "host_cores")):
        if _count(value):
            cpus, cpus_source = value, "measured"
            break
    for value, source in ((report.get("memory_bytes"), "measured"),
                          (placement.declared(worker, "memory_total_bytes"), "measured"),
                          (placement.declared(worker, "memory_bytes"), "declared budget")):
        if _count(value):
            memory, memory_source = value, source
            break
    return {"host_id": worker.get("host_id"), "cpus": cpus,
            "cpus_source": cpus_source, "memory_bytes": memory,
            "memory_source": memory_source}


def _serves(worker, fleet):
    """The matching `FleetStore._resource_workers` uses, without its health
    and capacity filters: a worker that is down for a moment, or whose
    declared budget is wrong, still has the hardware it had."""
    platform = fleet["platform"]
    return (worker.get("kind") == placement.WORKER_KIND.get(platform)
            and placement.declared(worker, "kind") == placement.RUNTIME_KIND.get(platform)
            and (placement.declared(worker, "architecture") or "x64") == fleet["architecture"])


def for_fleet(inventory, fleet):
    """Every host that could run this fleet's runners, healthy or not, with
    its hardware and a `healthy` flag."""
    healthy = {w["host_id"] for w in inventory.healthy()}
    return [dict(of_worker(w), healthy=w["host_id"] in healthy)
            for w in inventory.list() if _serves(w, fleet)]


def of_host(inventory, specs, platform, host_id, healthy_only=True):
    """One host's hardware: its own report, else - for the core count - what
    a runner placed on exactly that host last measured.

    Scoped to the host asked about and never pooled: a small guest and the
    physical host behind it number their processors independently, and
    mixing the two pinned a runner on an eight-processor guest to cores
    32-47 (2026-09-23). `healthy_only` keeps the window code's rule that a
    silent worker's declaration is not used; a limit check passes False,
    since hardware does not change when a heartbeat is late."""
    kind = placement.WORKER_KIND.get(platform)
    result = {"host_id": host_id, "cpus": None, "cpus_source": None,
              "memory_bytes": None, "memory_source": None}
    workers = inventory.healthy(kind=kind) if healthy_only else inventory.list(kind=kind)
    for worker in workers:
        if worker["host_id"] == host_id:
            result = of_worker(worker)
    if result["cpus"] is None:
        seen = [(s.get("telemetry") or {}).get("host_cores") for s in specs.list()
                if s.get("platform") == platform and s.get("host_id") == host_id]
        seen = [value for value in seen if _count(value)]
        if seen:
            result.update(cpus=min(seen), cpus_source="runner telemetry")
    return result


def _describe(host):
    parts = []
    parts.append(cores(host["cpus"]) if host.get("cpus") else "unknown cores")
    if host.get("memory_bytes"):
        memory = gib(host["memory_bytes"])
        if host.get("memory_source") == "declared budget":
            memory += " (declared budget)"
        parts.append(memory)
    else:
        parts.append("unknown memory")
    return f"{host['host_id']} has {' and '.join(parts)}"


def check(cpu, memory, hosts):
    """None when some host can hold `cpu` cores and `memory` bytes together,
    `UNVERIFIED` when none is known to be too small but none is known to fit
    either, else the sentence that refuses it, naming each host and what it
    has. `cpu` and `memory` may each be None, meaning "not asked"."""
    if cpu is None and memory is None:
        return None
    hosts = list(hosts)
    unknown = False
    for host in hosts:
        too_small = unclear = False
        for asked, have in ((cpu, host.get("cpus")), (memory, host.get("memory_bytes"))):
            if asked is None:
                continue
            if have is None:
                unclear = True
            elif float(asked) > have:
                too_small = True
        if not too_small and not unclear:
            return None
        unknown = unknown or not too_small
    if unknown or not hosts:
        return UNVERIFIED
    asked = " and ".join(text for text in (
        cores(cpu) if cpu is not None else None,
        gib(memory) if memory is not None else None) if text)
    where = (f"host {hosts[0]['host_id']}" if len(hosts) == 1
             else "any host of this fleet")
    return (f"{asked} does not fit {where}: "
            + "; ".join(_describe(h) for h in hosts))


def limits_max(hosts):
    """The largest core count and memory any of `hosts` has, and which host
    has it - what the page shows as "Max"."""
    top = {"cpus": None, "memory_bytes": None, "cpus_host": None,
           "memory_host": None, "memory_source": None}
    for host in hosts:
        if host.get("cpus") and (top["cpus"] is None or host["cpus"] > top["cpus"]):
            top.update(cpus=host["cpus"], cpus_host=host["host_id"])
        if host.get("memory_bytes") and (top["memory_bytes"] is None
                                         or host["memory_bytes"] > top["memory_bytes"]):
            top.update(memory_bytes=host["memory_bytes"], memory_host=host["host_id"],
                       memory_source=host.get("memory_source"))
    return top
