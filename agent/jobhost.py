"""The program a Windows runner's service runs: the runner, inside a Job Object.

A Windows service cannot be given a memory or CPU ceiling by the service
manager. A Job Object can, and every process started inside one - the runner,
each job it runs, everything those start - stays inside it and counts against
the same limits. So the service does not run the runner directly. It runs
this, which creates the job, puts itself in it, and only then starts the
runner, which inherits the membership. Nothing started afterwards can leave.

**The limits come from the unit file**, `<root>\\reg\\unit.json`, which the
runtime writes at create and which is readable only by this runner's own
account and administrators. The runner's environment is in the same file, not
on a command line: a command line on Windows is readable by anything that can
open the process.

**The job is killed with its host.** `KILL_ON_JOB_CLOSE` means that when this
process ends - stopped by the service manager, or crashed - every process left
in the job ends with it. A runner cannot outlive its service and keep running
a job nobody manages.

**It reports what the job uses.** Every ten seconds it writes
`<root>\\logs\\telemetry.json` with the job's CPU share and committed memory,
read from the job itself, which is the only place that sums a whole process
tree. The runtime reads that file; a stale one reads as unknown, never as
zero.

**It checks rather than trusts.** It refuses to run under the Microsoft Store
Python, whose children leave every job, and it confirms the runner's process
is inside the job before letting it run. A runner without its limits is
stopped, not tolerated.

Windows only, by nature: ctypes against kernel32. Nothing here takes a shell
or a command from anywhere but the fixed entry point in the runner's own tree.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

GRACE_SECONDS = 60
REPORT_SECONDS = 10

# Job object information classes and flags, winnt.h.
_BASIC_ACCOUNTING = 1
_EXTENDED_LIMITS = 9
_CPU_RATE_CONTROL = 15
_MEMORY_USAGE = 28

_LIMIT_AFFINITY = 0x00000010
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CPU_RATE_ENABLE = 0x1
_CPU_RATE_HARD_CAP = 0x4


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD),
                ("CpuRate", wintypes.DWORD)]


class _Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD)]


class _MemoryUsage(ctypes.Structure):
    _fields_ = [("JobMemory", ctypes.c_ulonglong),
                ("PeakJobMemoryUsed", ctypes.c_ulonglong)]


def cpu_rate(cpus, cpu_count):
    """Docker's `--cpus` as a Job Object CPU rate: cycles per 10,000 across
    the whole machine. None when there is no limit."""
    if not cpus or cpus <= 0:
        return None
    return max(1, min(10000, round(cpus / cpu_count * 10000)))


def affinity_mask(cpuset):
    """A Linux cpuset string ("0-15,32") as an affinity mask, or None. Only
    the first 64 processors can be named in one mask; a set beyond that is
    refused rather than silently truncated."""
    if not cpuset:
        return None
    mask = 0
    for part in cpuset.split(","):
        low, _, high = part.partition("-")
        low, high = int(low), int(high or low)
        if high >= 64 or low > high:
            raise ValueError(f"cpuset {cpuset!r} cannot be an affinity mask")
        for cpu in range(low, high + 1):
            mask |= 1 << cpu
    return mask


class Job:
    """One Job Object, closed when this process ends."""

    def __init__(self, memory_bytes=None, cpus=None, cpuset=None):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        limits = _ExtendedLimits()
        flags = _LIMIT_KILL_ON_JOB_CLOSE
        if memory_bytes:
            flags |= _LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = int(memory_bytes)
        mask = affinity_mask(cpuset)
        if mask:
            flags |= _LIMIT_AFFINITY
            limits.BasicLimitInformation.Affinity = mask
        limits.BasicLimitInformation.LimitFlags = flags
        self._set(_EXTENDED_LIMITS, limits)

        rate = cpu_rate(cpus, os.cpu_count() or 1)
        if rate:
            self._set(_CPU_RATE_CONTROL,
                      _CpuRate(_CPU_RATE_ENABLE | _CPU_RATE_HARD_CAP, rate))

    def _set(self, cls, info):
        if not self._k32.SetInformationJobObject(
                wintypes.HANDLE(self.handle), cls, ctypes.byref(info),
                ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _query(self, cls, info):
        if not self._k32.QueryInformationJobObject(
                wintypes.HANDLE(self.handle), cls, ctypes.byref(info),
                ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def enter(self):
        """Put this process in the job. Everything it starts afterwards is in
        it too, and cannot leave."""
        if not self._k32.AssignProcessToJobObject(
                wintypes.HANDLE(self.handle),
                wintypes.HANDLE(self._k32.GetCurrentProcess())):
            raise ctypes.WinError(ctypes.get_last_error())

    def limits(self):
        info = self._query(_EXTENDED_LIMITS, _ExtendedLimits())
        rate = self._query(_CPU_RATE_CONTROL, _CpuRate())
        return {"flags": info.BasicLimitInformation.LimitFlags,
                "job_memory_limit": info.JobMemoryLimit,
                "affinity": info.BasicLimitInformation.Affinity,
                "cpu_rate": rate.CpuRate if rate.ControlFlags else None}

    def cpu_seconds(self):
        acct = self._query(_BASIC_ACCOUNTING, _Accounting())
        return (acct.TotalUserTime + acct.TotalKernelTime) / 1e7

    def memory_bytes(self):
        return self._query(_MEMORY_USAGE, _MemoryUsage()).JobMemory


def packaged():
    """Whether this interpreter is an MSIX-packaged app - the Microsoft Store
    Python. Its child processes break away from every Job Object, so a cap
    set here would silently not apply. **MEASURED** on this host, 2026-09-18:
    under the Store Python a 512 MiB allocation escaped a 128 MiB cap; under a
    regular install it failed with OutOfMemoryException."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = wintypes.UINT(0)
    return k32.GetCurrentPackageFullName(ctypes.byref(length), None) != 15700


def in_job(process_handle, job):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    result = wintypes.BOOL(False)
    if not k32.IsProcessInJob(wintypes.HANDLE(int(process_handle)),
                              wintypes.HANDLE(job.handle),
                              ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(result.value)


def read_unit(root):
    with open(os.path.join(root, "reg", "unit.json"), encoding="utf-8") as fh:
        return json.load(fh)


def report(root, job, last, now):
    """Write the job's usage since `last` (cpu_seconds, time)."""
    cpu = job.cpu_seconds()
    used = job.memory_bytes()
    share = None
    if last is not None and now > last[1]:
        share = round(100.0 * (cpu - last[0]) / (now - last[1]), 2)
    path = os.path.join(root, "logs", "telemetry.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"at": now, "cpu_percent": share, "mem_used_bytes": used},
                  fh)
    os.replace(path + ".tmp", path)
    return cpu, now


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jobhost")
    parser.add_argument("--root", required=True)
    parser.add_argument("--report-seconds", type=float,
                        default=REPORT_SECONDS)
    args = parser.parse_args(argv)
    root = args.root

    if packaged():
        print("jobhost: refusing to run under a packaged (Store) Python: "
              "its children leave every Job Object, so no limit would hold",
              file=sys.stderr)
        return 3

    unit = read_unit(root)
    job = Job(memory_bytes=unit.get("memory_bytes"), cpus=unit.get("cpus"),
              cpuset=unit.get("cpuset"))
    job.enter()

    env = dict(os.environ)
    env.update(unit.get("env") or {})
    entry = os.path.join(root, "reg", "run.cmd")
    child = subprocess.Popen([entry], cwd=os.path.join(root, "work"), env=env)
    # Checked, not assumed: a runner outside its job is a runner without
    # limits, and that is worse than no runner.
    if not in_job(child._handle, job):
        child.kill()
        print("jobhost: the runner is not inside its Job Object; stopped it",
              file=sys.stderr)
        return 4

    last = None
    try:
        while True:
            try:
                code = child.wait(timeout=args.report_seconds)
                break
            except subprocess.TimeoutExpired:
                try:
                    last = report(root, job, last, time.time())
                except OSError:
                    pass            # telemetry must never stop the runner
    except KeyboardInterrupt:
        # The service manager's stop. The runner shares this console and has
        # the same Ctrl+C; give it the grace a deregistration needs.
        try:
            code = child.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
