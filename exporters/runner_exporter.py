"""Telemetry for the Forgejo runners that are not containers on this engine.

The dashboard can see every runner it started itself, because they are
containers on its own daemon. Two runners are not: the `forgejo-runner`
Windows service on BEAST-UNIT, and `beaststack-macos-sequoia`, which is a
QEMU container inside a Hyper-V VM. The forge knows their name, labels,
version and idle/active state and nothing else, so their cards on the
status page are blank where every other runner shows CPU, memory and disk.

This runs on BEAST-UNIT and serves both over one HTTP endpoint, because
BEAST-UNIT is the only host that can reach both: the dashboard's WSL
network has a route to the macOS VM but no traffic crosses it, while
BEAST-UNIT reaches it over SSH with a key that already exists. Putting an
exporter on the VM instead would need a netsh portproxy, which this
deployment already depends on for the dashboard itself and which has
already gone silently unbound once.

Standard library only, so it runs on the host's Python unchanged.

Read-only by construction: one GET route, no writes, no shell passthrough,
and every external command is a fixed argument list with a timeout.
"""
import json
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# time="2026-09-16T13:57:54+02:00" level=info msg="task 1130 repo is FiLL/x ..."
#
# The offset alternative is not decoration. The Windows service stamps a local
# offset while the containerised runners stamp "Z", and docker_ops's own
# pattern only accepts "Z" - reusing it here would report "no job" forever on
# Windows. Accepting both keeps one parser honest for both fleets.
RE_TASK = re.compile(
    r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:Z|[+-]\d{2}:\d{2})".*?'
    r'msg="task (\d+) repo is (\S+)')

_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
          "TIB": 1024 ** 4, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}


def job_from_log(text):
    """The job the runner is on, from the tail of its log.

    Returns "" for "no task line here", which is a real answer: the runner is
    running nothing. A caller that could not read the log at all returns None
    instead, and the two must not be confused - the daemon logs a task
    starting and never logs it finishing, which is why the forge, not this,
    decides busy versus idle.
    """
    last = ""
    for m in RE_TASK.finditer(text or ""):
        last = "task %s repo is %s" % (m.group(2), m.group(3))
    return last


def _to_bytes(s):
    m = re.match(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*$", s or "")
    if not m:
        return None
    unit = _UNITS.get(m.group(2).upper())
    if unit is None:
        return None
    return int(float(m.group(1)) * unit)


def parse_docker_stats(text):
    """One `docker stats --no-stream` row: name, CPU%, "used / limit".

    None on anything unrecognised. A zeroed reading would render as an idle,
    healthy runner, which is the one thing an unreachable machine must never
    be mistaken for.
    """
    parts = (text or "").strip().split("\t")
    if len(parts) < 3:
        return None
    try:
        cpu = float(parts[1].replace("%", "").strip())
    except ValueError:
        return None
    used, _, limit = parts[2].partition("/")
    used_b, limit_b = _to_bytes(used), _to_bytes(limit)
    if used_b is None or limit_b is None:
        return None
    return {"cpu_percent": cpu,
            "mem_used_bytes": used_b,
            "mem_limit_bytes": limit_b}


def parse_df(text):
    """The data line of `df -P -k`, in 1K blocks. None if it is not one."""
    parts = (text or "").strip().split()
    if len(parts) < 6:
        return None
    try:
        total = int(parts[1]) * 1024
        used = int(parts[2]) * 1024
        pct = int(parts[4].rstrip("%"))
    except ValueError:
        return None
    return {"total_bytes": total, "used_bytes": used, "percent": pct}


def _run(args, timeout):
    """A fixed argument list, never a shell string, with a hard timeout.

    Returns stdout or None. A hung VM must cost one timeout and not a wedged
    exporter, because the dashboard's collector sweep waits on this.
    """
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def probe_macos_vm(host, user, key, container, ssh=None, timeout=20):
    """The macOS runner, read over SSH in a single connection per sweep.

    Deliberately measures the VM and the QEMU container rather than macOS
    itself: this runner's actual failure has been the VM's root disk filling
    while the container stayed Up, and that is visible from here.
    """
    ssh = ssh or shutil.which("ssh")
    if not ssh:
        return None
    script = ("docker stats --no-stream --format "
              "'{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' %s; "
              "echo '===DF==='; df -P -k /; "
              "echo '===CPU==='; nproc" % container)
    out = _run([ssh, "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                "%s@%s" % (user, host), script], timeout)
    if out is None:
        return None
    stats_text, _, rest = out.partition("===DF===")
    df_text, _, cpu_text = rest.partition("===CPU===")
    stats = parse_docker_stats(stats_text.strip().splitlines()[0]
                               if stats_text.strip() else "")
    disk = parse_df(df_text.strip().splitlines()[-1] if df_text.strip() else "")
    if stats is None and disk is None:
        return None
    # nproc, so the card can read "1.2 / 8 cores" like every other runner.
    # docker stats reports CPU as a percentage of ONE core, so 113% is healthy
    # on this 8-core VM and alarming without the denominator.
    try:
        cores = int(cpu_text.strip().splitlines()[0])
    except (ValueError, IndexError):
        cores = None
    data = {"reachable": True, "disk": disk, "job": "", "cpu_cores": cores}
    data.update(stats or {})
    return data


def build_payload(probes):
    """Run every probe, isolating each one's failure from the others.

    A probe that raises or returns None lands as None, never as zeros and
    never as the previous sweep's numbers. The dashboard treats None as
    "could not ask", the same sentinel its forge lookups already use.
    """
    runners = {}
    for name, probe in probes.items():
        try:
            runners[name] = probe()
        except Exception:  # noqa: BLE001 - one bad probe must not hide the rest
            runners[name] = None
    return {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runners": runners}


class _Handler(BaseHTTPRequestHandler):
    probes = {}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.split("?")[0] != "/metrics":
            self.send_error(404)
            return
        body = json.dumps(build_payload(self.probes)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        """Silence per-request logging; the service log is for failures."""


def serve(address, port, probes):
    _Handler.probes = probes
    ThreadingHTTPServer((address, port), _Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover - exercised as a service
    sys.exit("run through exporters/windows/serve.py")
