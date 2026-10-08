"""Which worker a new runner goes on, or why none can take it (T-1502).

Design 12.4's step 3. A worker qualifies when it is of the kind the runner's
platform runs on, healthy, of the runner's architecture, and has room - by
the number of runners it says it can hold, and by the memory it says it has
against the memory its runners are limited to. Of those, the least loaded.

**It refuses rather than overcommits.** When no worker qualifies the answer
is None and a reason naming, for each worker, why it did not - so a fleet
waiting for room says what it is waiting for. The reconciler holds such a
runner in `planned` rather than failing it, and places it on a later pass
once a worker has room.

What a worker can hold comes from what its agent declares in its
capabilities: `max_runners`, `memory_bytes` and `architecture`, at the top or
under `runtime`. A worker that declares no limit is not limited by it; one
that declares no architecture is taken to be x64, which every worker built
so far is. Degraded, unknown and silent workers never qualify: the inventory
only offers healthy ones.

Pure: it reads what it is given and returns a choice. The flow feeds it the
inventory and the placed specs.
"""
from typing import Iterable, Mapping, Optional, Tuple

import providers

#: Which worker kind hosts which platform. macOS runs as an appliance inside a
#: Linux worker (16.4): there is no separate worker kind for it, on purpose.
WORKER_KIND = {
    providers.LINUX: "hyperv-linux",
    providers.WINDOWS: "hyperv-windows",
    providers.MACOS: "hyperv-linux",
}

#: Which runtime drives which platform - what a worker declares in its
#: capabilities. The worker kind alone cannot tell two Linux workers apart,
#: and by T-0802 there are two: one that runs containers and one that drives
#: a macOS appliance. A macOS runner belongs only on the second, and a Linux
#: runner would find no engine there. So placement reads what the worker says
#: it drives, the same way it reads what it says it can hold.
RUNTIME_KIND = {
    providers.LINUX: "linux-container",
    providers.WINDOWS: "windows-process",
    providers.MACOS: "macos-appliance",
}


def declared(worker, key):
    caps = worker.get("capabilities") or {}
    if not isinstance(caps, Mapping):
        return None
    if key in caps:
        return caps[key]
    runtime = caps.get("runtime") or {}
    return runtime.get(key) if isinstance(runtime, Mapping) else None


def enforces_disk_quota(worker):
    """Whether a worker holds each runner to its own disk limit. Windows
    declares `disk_quota`; the Linux runtime declares `disk_limit_enforced`
    for its per-runner filesystems. An explicit `disk_quota` wins either
    way, so an appliance that says False is never taken at its word for
    something else."""
    quota = declared(worker, "disk_quota")
    if quota is not None:
        return quota is True
    return declared(worker, "disk_limit_enforced") is True


def enforces_appliance_limits(worker):
    return all(declared(worker, key) is True for key in
               ("appliance_per_runner", "cpu_enforcement", "memory_enforcement"))


def _cores_asked(spec):
    """How many logical CPUs a runner needs: the transient `cpu_width` the
    service hands placement for a pinned runner that has no window yet, else
    the width of the window it has, else its quota. None when it asks for no
    particular number."""
    width = spec.get("cpu_width")
    if width:
        return float(width)
    value = spec.get("cpu_limit")
    text = str(value or "").strip()
    if not text:
        return None
    if "-" in text or "," in text:
        from .cpusets import parse
        try:
            return float(len(parse(text)))
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _too_big_for(spec, worker, want_memory):
    """Why this worker's hardware cannot hold the runner at all, or None.
    Measured hardware only - a declared budget is checked further down as
    the budget it is - and unknown hardware never refuses (control/
    hardware.py)."""
    from .hardware import cores, gib, of_worker
    have = of_worker(worker)
    asked = _cores_asked(spec)
    if asked and have["cpus"] and asked > have["cpus"]:
        return (f"{worker['host_id']}: has {have['cpus']} logical CPUs, "
                f"this runner needs {cores(asked)}")
    if (want_memory and have["memory_source"] == "measured"
            and want_memory > have["memory_bytes"]):
        return (f"{worker['host_id']}: has {gib(have['memory_bytes'])} of "
                f"physical memory, this runner needs {gib(want_memory)}")
    return None


def _load(host_id, placed):
    mine = [s for s in placed if s.get("host_id") == host_id]
    return len(mine), sum(int(s.get("memory_limit") or 0) for s in mine)


def choose(spec: Mapping, workers: Iterable[Mapping],
           placed: Iterable[Mapping]) -> Tuple[Optional[str], Optional[str]]:
    """(host_id, None), or (None, why no worker can take this runner).

    `workers` are the healthy workers of the runner's kind; `placed` every
    other runner that holds a place on a worker."""
    placed = [s for s in placed
              if s.get("runner_id") != spec.get("runner_id")
              and s.get("actual_state") != "absent"]
    want_arch = spec.get("architecture") or providers.X64
    want_memory = int(spec.get("memory_limit") or 0)
    kind = WORKER_KIND.get(spec.get("platform"))
    fits, why_not = [], []
    want_runtime = RUNTIME_KIND.get(spec.get("platform"))
    for w in workers:
        host = w["host_id"]
        if declared(w, "capacity_valid") is False:
            why_not.append(f"{host}: declared capacity is not backed by measured RAM and swap")
            continue
        if spec.get("disk_limit") and not enforces_disk_quota(w):
            why_not.append(f"{host}: per-runner disk quota is not supported")
            continue
        if spec.get("platform") == providers.MACOS and (spec.get("cpu_limit") or spec.get("memory_limit")):
            if not enforces_appliance_limits(w):
                why_not.append(f"{host}: per-runner macOS CPU/RAM enforcement is not supported")
                continue
        drives = declared(w, "kind")
        # A worker that has declared nothing is taken at its worker kind,
        # which says what it is for every platform but macOS: an appliance
        # host and an ordinary Linux worker are both `hyperv-linux`, so an
        # appliance runner is placed only where an appliance was declared.
        if drives != want_runtime and (drives or want_runtime ==
                                       RUNTIME_KIND[providers.MACOS]):
            why_not.append(f"{host}: drives {drives or 'nothing it declared'},"
                           f" this runner needs {want_runtime}")
            continue
        arch = declared(w, "architecture") or providers.X64
        if arch != want_arch:
            why_not.append(f"{host}: {arch}, the runner needs {want_arch}")
            continue
        too_big = _too_big_for(spec, w, want_memory)
        if too_big:
            why_not.append(too_big)
            continue
        if declared(w, "builds_from") == "template":
            template = str(spec.get("runtime_template") or "").split(" ")[0]
            if not template or template not in (declared(w, "templates") or []):
                why_not.append(f"{host}: replacement template {template or '(unset)'} is not installed")
                continue
        count, memory = _load(host, placed)
        slots = declared(w, "max_runners")
        if slots is not None and count >= int(slots):
            why_not.append(f"{host}: full ({count} of {slots} runners)")
            continue
        total = declared(w, "memory_bytes")
        commit = declared(w, "memory_commit_bytes")
        if commit is not None:
            # Explicit oversubscription, not a physical reservation guarantee.
            # Aggregate backing alone cannot guarantee every cgroup can reach
            # its combined ceiling: each also has its own swap maximum.
            physical = int(total or 0)
            swap = int(declared(w, "swap_bytes") or 0)
            if declared(w, "memory_admission") != "bounded-overcommit" \
                    or drives != "linux-container" or physical <= 0 or swap <= 0 \
                    or int(commit) <= 0 or int(commit) > physical + swap:
                why_not.append(f"{host}: invalid backed memory commitment budget")
                continue
            if want_memory > physical:
                why_not.append(f"{host}: runner RAM ceiling exceeds physical worker budget")
                continue
            occupants = [s for s in placed if s.get("host_id") == host]
            want_swap = int(spec.get("memory_swap_limit") or want_memory)
            if want_swap < want_memory:
                why_not.append(f"{host}: combined memory limit is below RAM limit")
                continue
            # Do not overbook units that cannot swap at all even in this mode.
            unswappable = sum(int(s.get("memory_limit") or 0) for s in occupants
                if int(s.get("memory_swap_limit") or 0) <= int(s.get("memory_limit") or 0))
            if want_swap == want_memory:
                unswappable += want_memory
            if unswappable > physical:
                why_not.append(f"{host}: insufficient RAM for units without a swap allowance")
                continue
            total = int(commit)
            memory = sum(max(int(s.get("memory_limit") or 0),
                             int(s.get("memory_swap_limit") or 0)) for s in occupants)
            demand = want_swap
        else:
            overhead = int(declared(w, "per_runner_memory_overhead_bytes") or 0)
            if overhead < 0:
                why_not.append(f"{host}: invalid per-runner memory overhead")
                continue
            memory += count * overhead
            demand = want_memory + overhead
        if total is not None and (not want_memory or any(
                not s.get("memory_limit") for s in placed if s.get("host_id") == host)):
            why_not.append(f"{host}: memory requirements are unknown; refusing to overcommit")
            continue
        if total is not None and want_memory and \
                memory + demand > int(total):
            why_not.append(f"{host}: not enough memory ({memory + demand}"
                           f" bytes asked of {total})")
            continue
        fits.append((count, host))
    if fits:
        return min(fits)[1], None
    if not why_not:
        return None, f"no healthy {kind} worker to place this runner on"
    return None, "no worker has room: " + "; ".join(why_not)
