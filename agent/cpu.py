"""How many cores a unit may really use.

"70%" means nothing without what it is 70% of. Two different knobs cap a
unit's CPU and they are not interchangeable:

  a quota (`--cpus`, a Job Object rate) caps how much CPU time the unit
  gets, and changes nothing about what it sees: `nproc` inside still reports
  every core of the worker, and a build running `-j$(nproc)` still starts a
  job for each of them, with the memory to match.

  a cpuset pins the affinity mask, so `nproc` reports the width of the set
  and `-j$(nproc)` scales down with it. A nested container - how a buildx
  build actually runs - inherits it, because a child cgroup's effective
  cpuset can never exceed its parent's.

Whichever is lower is what the unit can really use. These are the rules the
dashboard this platform replaces arrived at; they are here so every runtime
answers the same way, and so the number on a card is one that is true.
"""
import math


def cpuset_count(spec):
    """How many CPUs a cpuset spec allows; 0 when unset or unparsable.

    An engine hands the spec back exactly as it was given ("0-15",
    "43-55,0-2"), so this takes ranges and bare indices, and a wrapped set is
    just two ranges. 0 means "no answer", never "no CPUs": an unparsable spec
    must not be reported as a ceiling of nothing.
    """
    intervals = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        low, dash, high = part.partition("-")
        try:
            first = int(low)
            last = int(high) if dash else first
        except ValueError:
            return 0
        if first < 0 or last < first:
            return 0
        intervals.append((first, last))
    total, end = 0, -1
    for first, last in sorted(intervals):
        total += max(0, last - max(first, end + 1) + 1)
        end = max(end, last)
    return total


def ceiling(cpuset, nano_cpus):
    """Cores this unit may use, or None for "as many as the worker has"."""
    caps = []
    width = cpuset_count(cpuset)
    if width:
        caps.append(float(width))
    try:
        quota = float(nano_cpus or 0) / 1e9
    except (TypeError, ValueError):
        quota = 0
    if math.isfinite(quota) and quota > 0:
        caps.append(quota)
    if not caps:
        return None
    lowest = min(caps)
    return int(lowest) if lowest == int(lowest) else round(lowest, 2)
