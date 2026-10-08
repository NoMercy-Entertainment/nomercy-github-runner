"""What a CPU or memory limit may be written as.

One definition for the two places a limit is set - a fleet's default in
Settings, and an admin's override on one runner - so a value one of them
accepts is never refused, or read differently, by the other.

A CPU limit is a positive number. On Linux and Windows a whole number pins
each runner to a window of that many cores and a fraction is a quota
(control/cpusets.py); a macOS appliance takes a whole vCPU count. It is
stored as text because a runner's resolved value can be a cpuset, which is
not a number - but a cpuset is never something a person types here.

A memory limit is a positive number of bytes. The macOS appliance takes
whole GiB from 4 to 128, which is what its guest can be given.
"""
import math

GIB = 1024**3
MAX_BYTES = 2**63 - 1


def normalize_cpu(value, platform):
    """The text stored for a CPU limit, or None for "not set"; ValueError
    with the reason otherwise."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("cpu_limit must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("cpu_limit must be positive") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError("cpu_limit must be positive")
    if platform == "macos":
        if not number.is_integer() or not 1 <= number <= 64:
            raise ValueError("macOS appliance CPU limit must be a whole count from 1 to 64")
        return str(int(number))
    return str(number)


def normalize_bytes(value, key):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_BYTES:
        raise ValueError(f"{key} must be positive bytes or null")
    return value


def normalize_memory(value, platform, key="memory_limit"):
    value = normalize_bytes(value, key)
    if value is not None and platform == "macos":
        if value % GIB or not 4 * GIB <= value <= 128 * GIB:
            raise ValueError("macOS appliance memory must be whole GiB from 4 to 128")
    return value
