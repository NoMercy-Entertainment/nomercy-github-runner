"""What the machine this agent runs on has: logical CPUs and physical memory.

Declared in every runtime's capabilities as `hardware`, and read by the
controller as the maximum any CPU or memory limit on this worker may be
(dashboard/control/hardware.py, GitHub #5). Measured here, on the machine,
rather than configured: a number someone typed is a budget, and a budget is
what `capacity.memory_bytes` already is.

Linux reads MemTotal from /proc/meminfo - inside the Hyper-V VM that is the
VM's memory, which is the machine the units share. Windows has no
/proc/meminfo and asks GlobalMemoryStatusEx for ullTotalPhys.

A reading that fails is None. The agent declares everything else either way,
and the controller treats unknown hardware as unverified, never as small.
"""
import ctypes
import os


class MEMORYSTATUSEX(ctypes.Structure):
    """The structure GlobalMemoryStatusEx fills. Its first field must hold
    the structure's own size or Windows refuses the call."""
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def logical_cpus():
    count = os.cpu_count()
    return count if isinstance(count, int) and count > 0 else None


def linux_memory(path="/proc/meminfo"):
    """MemTotal in bytes, or None."""
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                name, _, value = line.partition(":")
                if name.strip() == "MemTotal":
                    kib = int(value.split()[0])
                    return kib * 1024 if kib > 0 else None
    except (OSError, ValueError, IndexError):
        return None
    return None


def _kernel32():
    windll = getattr(ctypes, "windll", None)
    return getattr(windll, "kernel32", None) if windll is not None else None


def windows_memory(kernel32=None):
    """Total physical memory in bytes from GlobalMemoryStatusEx, or None."""
    kernel32 = kernel32 if kernel32 is not None else _kernel32()
    if kernel32 is None:
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
    except (OSError, AttributeError, ctypes.ArgumentError):
        return None
    total = int(status.ullTotalPhys)
    return total if total > 0 else None


def facts(platform, meminfo="/proc/meminfo", kernel32=None):
    """{"logical_cpus", "memory_bytes"} for this machine, each None when it
    could not be measured. `platform` is the runtime's, not a guess from
    sys.platform: a Windows runtime asks Windows, the others read Linux."""
    memory = (windows_memory(kernel32) if str(platform).startswith("win")
              else linux_memory(meminfo))
    return {"logical_cpus": logical_cpus(), "memory_bytes": memory}
