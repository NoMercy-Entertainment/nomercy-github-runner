"""Entry point for the exporter as it runs on BEAST-UNIT.

Keeps everything Windows-specific out of runner_exporter.py, so that module
stays OS-neutral and its parsers can be tested anywhere, including in the
dashboard's Linux container image.

Configuration is environment only, so NSSM carries it and nothing secret ever
lands in a command line visible to every process on the box.
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner_exporter as ex  # noqa: E402

# Win32_PerfFormattedData_PerfProc_Process, not Get-Process: the service runs
# as SYSTEM, and an unelevated Get-Process cannot read its StartTime or
# TotalProcessorTime at all (both throw). The perf class answers all three
# without elevation, which is what lets this run as an ordinary service.
_PS = r"""
$ErrorActionPreference = 'Stop'
$p = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -Filter "Name='forgejo-runner'"
$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$env:EXPORTER_DRIVE'"
$s = Get-CimInstance Win32_Service -Filter "Name='forgejo-runner'"
$c = Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{
  cpu_percent   = [double]$p.PercentProcessorTime
  mem_used      = [int64]$p.WorkingSetPrivate
  uptime_seconds= [int64]$p.ElapsedTime
  disk_total    = [int64]$d.Size
  disk_free     = [int64]$d.FreeSpace
  state         = [string]$s.State
  cpu_cores     = [int]$c.NumberOfLogicalProcessors
} | ConvertTo-Json -Compress
"""


def probe_windows_service(log_path, drive="C:", timeout=20):
    """The forgejo-runner Windows service: CPU, memory, uptime, disk, job.

    Returns None when PowerShell cannot answer. Zeros would render as an
    idle, healthy runner - the one state an unreachable machine must never
    be mistaken for.
    """
    env = dict(os.environ, EXPORTER_DRIVE=drive)
    powershell = resolve_powershell()
    if not powershell:
        ex.note("windows", "no powershell binary found")
        return None
    try:
        p = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _PS],
            capture_output=True, text=True, timeout=timeout, shell=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        ex.note("windows", "powershell could not run: %s" % exc)
        return None
    if p.returncode != 0 or not p.stdout.strip():
        ex.note("windows", "powershell exit %s: %s"
                % (p.returncode, (p.stderr or "").strip()[:300]))
        return None
    try:
        d = json.loads(p.stdout)
    except ValueError:
        ex.note("windows", "powershell output was not JSON: %s"
                % p.stdout.strip()[:200])
        return None

    job = ""
    try:
        # Tail only: these logs run to tens of thousands of lines and the
        # exporter is on the dashboard's refresh path.
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            job = ex.job_from_log(fh.read().decode("utf-8", "replace"))
    except OSError:
        job = ""

    total, free = d.get("disk_total") or 0, d.get("disk_free") or 0
    disk = None
    if total > 0:
        used = total - free
        disk = {"total_bytes": total, "used_bytes": used,
                "percent": int(used * 100 / total)}
    return {
        "reachable": True,
        "cpu_percent": round(float(d.get("cpu_percent") or 0.0), 2),
        "mem_used_bytes": int(d.get("mem_used") or 0),
        "mem_limit_bytes": None,      # a service has no cgroup ceiling
        "cpu_cores": int(d.get("cpu_cores") or 0) or None,
        "uptime_seconds": int(d.get("uptime_seconds") or 0),
        "state": d.get("state") or "",
        "disk": disk,
        "job": job,
    }


# Windows ships OpenSSH in System32 but does not put it on PATH, so
# shutil.which("ssh") finds nothing and the macOS probe silently returned
# None for every sweep - the exporter looked healthy while half of what it
# exists for was missing. Resolve it explicitly and let the service override.
_WINDOWS_SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"

# Absolute, for the same reason as ssh: running as LocalSystem the service
# got "[WinError 2] The system cannot find the file specified" for a bare
# "powershell.exe". A service PATH is not the interactive one.
_WINDOWS_PS = r"C:\Windows\System32\WindowsPowerShell1.0\powershell.exe"


def resolve_powershell():
    """The powershell binary to use, or None if there is genuinely none."""
    configured = os.environ.get("EXPORTER_POWERSHELL")
    if configured:
        return configured if os.path.exists(configured) else None
    if os.path.exists(_WINDOWS_PS):
        return _WINDOWS_PS
    return shutil.which("powershell.exe")


def resolve_ssh():
    """The ssh binary to use, or None if there is genuinely none."""
    configured = os.environ.get("EXPORTER_SSH")
    if configured:
        return configured if os.path.exists(configured) else None
    found = shutil.which("ssh")
    if found:
        return found
    return _WINDOWS_SSH if os.path.exists(_WINDOWS_SSH) else None


def main():
    windows_name = os.environ.get("EXPORTER_WINDOWS_RUNNER",
                                  "beaststack-windows-runner")
    macos_name = os.environ.get("EXPORTER_MACOS_RUNNER",
                                "beaststack-macos-sequoia")
    log_path = os.environ.get("EXPORTER_WINDOWS_LOG",
                              r"C:\forgejo-runner\runner.err.log")
    drive = os.environ.get("EXPORTER_DRIVE", "C:")

    probes = {windows_name: lambda: probe_windows_service(log_path, drive)}

    host = os.environ.get("EXPORTER_MACOS_HOST")
    if host:
        user = os.environ.get("EXPORTER_MACOS_USER", "runner")
        key = os.environ.get("EXPORTER_MACOS_KEY", "")
        container = os.environ.get("EXPORTER_MACOS_CONTAINER", "macos-sequoia")
        ssh = resolve_ssh()
        probes[macos_name] = lambda: ex.probe_macos_vm(
            host, user, key, container, ssh=ssh)

    ex.serve(os.environ.get("EXPORTER_BIND", "0.0.0.0"),
             int(os.environ.get("EXPORTER_PORT", "9101")), probes)


if __name__ == "__main__":
    main()
