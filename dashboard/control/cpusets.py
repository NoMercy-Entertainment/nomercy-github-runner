"""Pinned CPU windows for Linux runners.

A CFS quota does not change what `nproc` reports inside a container; a cpuset
does, and a build that runs `-j$(nproc)` sizes itself by it (2026-09-17). So
on Linux a fleet's whole-number CPU limit means: give each runner its own
window of that many cores. Windows are staggered so they overlap as evenly
as the host allows - thirteen 16-core windows over 56 cores put every core in
three or four of them, and none sits idle.
"""


def is_cpuset(value):
    """A range or a list - the same rule the agent runtime applies. A plain
    number is a quota."""
    text = str(value or "").strip()
    return bool(text) and ("-" in text or "," in text)


def parse(text):
    """The cores a cpuset string names."""
    cores = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty element in cpuset {text!r}")
        low, sep, high = part.partition("-")
        if not low.isdigit() or (sep and not high.isdigit()):
            raise ValueError(f"not a cpuset: {text!r}")
        a, b = int(low), int(high) if sep else int(low)
        if b < a:
            raise ValueError(f"descending range in cpuset {text!r}")
        cores.update(range(a, b + 1))
    return cores


def window(start, width, cores):
    """`width` cores from `start`, wrapping round the end of the host."""
    width = min(width, cores)
    end = start + width - 1
    if width == cores:
        return f"0-{cores - 1}"
    if end < cores:
        return f"{start}-{end}"
    return f"{start}-{cores - 1},0-{end - cores}"


def allocate(width, cores, taken):
    """The window of `width` cores that overlaps the `taken` sets least:
    lowest peak first, then lowest total, then lowest start.

    Refused, never narrowed, when `width` does not fit `cores`: a runner
    quietly pinned to fewer cores than its fleet asked for is worse than
    one that fails to start, and the caller - which knows which host
    `cores` came from - can say why in a way this function cannot
    (2026-09-23).
    """
    width, cores = int(width), int(cores)
    if width > cores:
        raise ValueError(
            f"a window of {width} cores does not fit a host of {cores}")
    width = max(1, width)
    cover = [0] * cores
    for used in taken:
        for core in used:
            if 0 <= core < cores:
                cover[core] += 1
    best = None
    for start in range(cores):
        span = [cover[(start + i) % cores] for i in range(width)]
        key = (max(span), sum(span), start)
        if best is None or key < best:
            best = key
    return window(best[2], width, cores)


def whole_cores(value):
    """The core count a fleet limit asks to pin, or None when it is a quota
    (a fraction) or not set."""
    if value is None or is_cpuset(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number >= 1 and number.is_integer() else None
