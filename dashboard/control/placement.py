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
    if key in caps:
        return caps[key]
    return (caps.get("runtime") or {}).get(key)


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
        count, memory = _load(host, placed)
        slots = declared(w, "max_runners")
        if slots is not None and count >= int(slots):
            why_not.append(f"{host}: full ({count} of {slots} runners)")
            continue
        total = declared(w, "memory_bytes")
        if total is not None and want_memory and \
                memory + want_memory > int(total):
            why_not.append(f"{host}: not enough memory ({memory + want_memory}"
                           f" bytes asked of {total})")
            continue
        fits.append((count, host))
    if fits:
        return min(fits)[1], None
    if not why_not:
        return None, f"no healthy {kind} worker to place this runner on"
    return None, "no worker has room: " + "; ".join(why_not)
