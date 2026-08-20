"""Runner lifecycle and telemetry against the github-runners Docker engine.

Everything here shells out to the docker CLI over the mounted socket. That
socket belongs to the runners' own engine, so nothing in this module can reach
any other Docker daemon.

Deliberately NOT using `docker compose`: the dashboard is itself a container on
this engine, and a compose run would recreate it mid-request and kill the
process serving that request. Talking to the API directly keeps the dashboard
out of its own blast radius and leaves the compose file untouched.
"""

import json
import os
import re
import subprocess
import time

IMAGE = os.environ.get(
    "RUNNER_IMAGE",
    "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest",
)
# Path to the repo AS THE DOCKER DAEMON SEES IT, not as this container sees it.
# Bind mounts are resolved by the daemon, so a path valid only inside the
# dashboard container would silently create an empty directory instead.
REPO_HOST_PATH = os.environ.get("REPO_HOST_PATH", "/mnt/d/docker-compose/GithubRunners")
PREFIX = "github-runner-"
LABEL = "nomercy.runner=true"

STATE_PATH = os.path.join(os.environ.get("DASH_DATA", "/data"), "state.json")


# --------------------------------------------------------------------------
# shell helpers
# --------------------------------------------------------------------------

def _run(args, timeout=30):
    """Run a docker command. Returns (ok, stdout, stderr).

    Never raises: a wedged runner must not take the whole dashboard down with
    it, so every failure comes back as data for the caller to render.
    """
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 - surfaced to the UI, not swallowed
        return False, "", str(e)


def _docker(*args, timeout=30):
    return _run(["docker", *args], timeout=timeout)


# --------------------------------------------------------------------------
# drain state (persisted so a dashboard restart does not forget an in-flight
# drain and leave a runner marked forever)
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {"draining": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def set_draining(name, on=True):
    st = load_state()
    d = set(st.get("draining", []))
    d.add(name) if on else d.discard(name)
    st["draining"] = sorted(d)
    save_state(st)


# --------------------------------------------------------------------------
# telemetry
# --------------------------------------------------------------------------

def list_runner_names():
    ok, out, _ = _docker("ps", "-a", "--filter", f"name=^{PREFIX}",
                         "--format", "{{.Names}}")
    if not ok:
        return []
    names = [n for n in out.splitlines() if n.startswith(PREFIX)]
    return sorted(names, key=_index_of)


def _index_of(name):
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def next_free_index():
    used = {_index_of(n) for n in list_runner_names()}
    i = 1
    while i in used:
        i += 1
    return i


def _stats_map():
    """One `docker stats` call for every container.

    Per-container calls cost ~1s each; with six runners that is six seconds
    per refresh, which makes the whole page feel broken.
    """
    ok, out, _ = _docker("stats", "--no-stream",
                         "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                         timeout=25)
    m = {}
    if ok:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                m[parts[0]] = (parts[1], parts[2])
    return m


def _job_state(name):
    """Derive busy/idle/unknown from the runner's log.

    Takes the LAST job event of either kind. Grepping only for 'Running job'
    reports a finished job as still running whenever its completion line has
    scrolled out of the tail window.

    A failed `docker logs` (including its own 10s timeout) returns "unknown",
    not "idle". The two are not interchangeable: a runner mid-way through a
    heavy build is exactly the one whose log read is most likely to time out,
    so answering "idle" here would tell a destructive caller (prune) it is
    safe to act precisely when it cannot tell. Callers that need a strict
    safe/unsafe split (is_idle) already collapse "unknown" into "not idle";
    callers that show state to an operator (collect) treat it explicitly.
    """
    ok, out, _ = _docker("logs", "--tail", "200", name, timeout=10)
    if not ok:
        return "unknown", ""
    last = ""
    for line in out.splitlines():
        if "Running job:" in line or re.search(r"Job .* completed", line):
            last = line
    if "Running job:" in last:
        return "busy", last.split("Running job:", 1)[1].strip()
    return "idle", ""


def _registration(name):
    ok, out, _ = _docker("exec", name, "cat",
                         "/root/actions-runner/.runner", timeout=8)
    if not ok:
        return "-"
    try:
        # The runner writes .runner with a UTF-8 BOM. json.loads chokes on it,
        # which silently turned every registration name into "-".
        return json.loads(out.lstrip("﻿")).get("agentName", "-")
    except Exception:
        return "-"


def _df_rows(name):
    """Parsed `docker system df` rows keyed by Type, or None if unmeasurable.

    _inner_df flattens a failure to ("0B","0B") for the grid, which is fine
    there. prune() must not: reporting a fabricated empty after-state would
    overstate what was reclaimed.
    """
    ok, out, _ = _docker("exec", name, "docker", "system", "df",
                         "--format", "json", timeout=20)
    if not ok or not out:
        return None
    rows = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:      # noqa: BLE001 - rendered, not raised
            continue
        if isinstance(row, dict) and row.get("Type"):
            rows[row["Type"]] = row
    return rows or None


def _inner_df(name):
    """Build cache and image totals from the runner's own daemon.

    `docker system df --format json` emits ONE OBJECT PER LINE, one per
    resource type, keyed by a "Type" field - not a single object with
    "BuildCache" and "Images" keys. Reading only the first line and looking up
    those keys is why this reported 0B for every runner regardless of what they
    were actually holding.
    """
    rows = _df_rows(name)
    if rows is None:
        return "0B", "0B"
    return (rows.get("Build Cache", {}).get("Size", "0B"),
            rows.get("Images", {}).get("Size", "0B"))


_host_info_cache = None


def host_info():
    """Core count and total RAM of the engine hosting the runners.

    Cached for the process lifetime: this shells out, and a host does not gain
    cores while the dashboard runs.

    Needed because `docker stats` reports CPU as a percentage of ONE core, so
    "70%" on a 56-core box means 1.3 cores, not "nearly full". Without the
    denominator the number is actively misleading.
    """
    global _host_info_cache
    if _host_info_cache is not None:
        return _host_info_cache
    # 15s, not the default 30s: `docker info` talks to the daemon, not a
    # container, so it should answer fast - but a slow/warming-up daemon can
    # still hit this boundary, which is exactly the case the caching below
    # must survive without wedging the feature off for good.
    ok, out, _ = _docker("info", "--format", "{{.NCPU}} {{.MemTotal}}",
                         timeout=15)
    info = {"ncpu": 0, "mem_total_bytes": 0}
    if ok and out:
        parts = out.split()
        if len(parts) >= 2:
            try:
                info = {"ncpu": int(parts[0]),
                        "mem_total_bytes": int(parts[1])}
            except ValueError:
                pass
    # Only cache on success. A transient failure (daemon warm-up, a timeout
    # under load, a blip) must not be memoised as though it were the real
    # answer - runner_detail.cached() already had to fix exactly this bug
    # shape (a failure stored and replayed for its whole TTL); caching the
    # zero sentinel here unconditionally would reproduce it, but for the rest
    # of the process's life instead of a TTL, since nothing ever calls
    # host_info() with _host_info_cache reset. Leaving the sentinel at None
    # lets the next call retry.
    if ok:
        _host_info_cache = info
    return info


def collect():
    stats = _stats_map()
    draining = set(load_state().get("draining", []))
    runners = []

    for name in list_runner_names():
        ok, status, _ = _docker("ps", "-a", "--filter", f"name=^{name}$",
                                "--format", "{{.Status}}")
        status = status or "unknown"
        running = status.startswith("Up")

        if not running:
            runners.append({
                "name": name, "registration": "-", "state": "stopped",
                "job": "", "uptime": status, "cpu_percent": 0,
                "mem_used": "0B", "mem_limit": "-",
                "build_cache": "0B", "images": "0B",
            })
            continue

        job_state, job = _job_state(name)
        if name in draining:
            state = "draining"
        elif job_state == "unknown":
            # A failed log read correlates with a runner grinding through a
            # heavy build (see _job_state), so "busy" is the safer guess for
            # the grid than a bare "unknown" the CSS has no styling for -
            # and it is more accurate than the old behaviour of silently
            # showing "idle" here.
            state = "busy"
            job = job or "state unknown - could not read logs"
        else:
            state = job_state
        cpu_s, mem_s = stats.get(name, ("0%", "0B / 0B"))
        try:
            cpu = float(cpu_s.replace("%", ""))
        except ValueError:
            cpu = 0.0
        mem_used, _, mem_limit = mem_s.partition(" / ")
        cache, images = _inner_df(name)

        runners.append({
            "name": name,
            "registration": _registration(name),
            "state": state,
            "job": job,
            "uptime": status.replace("Up ", "").split(" (")[0],
            "cpu_percent": round(cpu, 2),
            "mem_used": mem_used or "0B",
            "mem_limit": mem_limit or "-",
            "build_cache": cache,
            "images": images,
        })

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disk": _disk(),
        "host": host_info(),
        "runners": runners,
    }


def _disk():
    """Disk backing this engine.

    Measures /data - the dashboard's own volume - because it lives on the same
    filesystem as the engine's data root. /var/lib/docker does not exist inside
    this container and would silently report zero.
    """
    try:
        s = os.statvfs(os.environ.get("DASH_DATA", "/data"))
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - free
        pct = int(used * 100 / total) if total else 0
        return {"used_bytes": used, "total_bytes": total, "percent": pct}
    except Exception:
        return {"used_bytes": 0, "total_bytes": 0, "percent": 0}


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def start(name):
    return _docker("start", name)


def stop(name, timeout=60):
    """SIGTERM with a grace period.

    60s, not the engine default: start.sh deregisters the runner from GitHub on
    SIGTERM and needs a few seconds. Docker Engine 29.x creates containers with
    StopTimeout=1, which kills deregistration mid-flight and orphans the
    registration.
    """
    return _docker("stop", "-t", str(timeout), name, timeout=timeout + 20)


def restart(name, timeout=60):
    return _docker("restart", "-t", str(timeout), name, timeout=timeout + 30)


def remove(name):
    # 180s, not 120s: these containers hold a nested (Docker-in-Docker)
    # daemon, and tearing that down on removal has been observed to take
    # ~110s - too close to a 120s limit for comfort. A timeout here is read
    # by the caller as "removal failed" even when it eventually succeeds, so
    # the margin matters more than it looks like it should.
    ok, out, err = _docker("rm", "-f", name, timeout=180)
    set_draining(name, False)
    return ok, out, err


def create(index, env):
    """Create a runner container matching the compose definition."""
    name = f"{PREFIX}{index}"
    args = [
        "run", "-d",
        "--name", name,
        "--label", LABEL,
        "--privileged",
        "--restart", "unless-stopped",
        "--stop-timeout", "60",
        "--tmpfs", "/tmp",
        "-v", f"{REPO_HOST_PATH}/scripts/start.sh:/root/start.sh:ro",
        "-e", f"GH_TOKEN={env.get('GH_TOKEN', '')}",
        "-e", f"GITHUB_ORG={env.get('GITHUB_ORG', 'NoMercy-Entertainment')}",
        "-e", f"RUNNER_LABELS={env.get('RUNNER_LABELS', 'self-hosted,Linux,X64')}",
        "-e", f"RUNNER_GROUP={env.get('RUNNER_GROUP', '')}",
    ]
    cpu = (env.get("RUNNER_CPU_LIMIT") or "0").strip()
    mem = (env.get("RUNNER_MEM_LIMIT") or "0").strip()
    if cpu not in ("", "0"):
        args += ["--cpus", cpu]
    if mem not in ("", "0"):
        args += ["--memory", mem]
    args.append(IMAGE)

    ok, out, err = _docker(*args, timeout=180)
    return ok, name, (err or out)


def started_at(name):
    """When this container last started, to whole seconds, or "" if unknown.

    Truncated rather than passed through: the daemon reports nanoseconds
    ("2026-08-20T14:23:37.714612450Z") while history stores whole seconds
    ("2026-08-20T14:23:37Z"), and the two are compared as strings. Left
    untruncated, a fractional stamp sorts *before* the same second without
    one, because "." is below "Z" - close enough to right to survive review
    and wrong exactly once a second.
    """
    ok, out, _ = _docker("inspect", "-f", "{{.State.StartedAt}}", name,
                         timeout=10)
    if not ok:
        return ""
    stamp = (out or "").strip()
    head, sep, _ = stamp.partition(".")
    return (head + "Z") if sep else stamp


def is_idle(name):
    """True only for a definite "idle". "busy" and "unknown" both answer
    False - this gates both the drain watcher (worst case: an early stop,
    which just restarts) and cache prune (worst case: deleting layers a
    running job needs), and the two callers cannot be told apart from here.
    """
    state, _ = _job_state(name)
    return state == "idle"


def logs_since(name, seconds=45):
    """Recent log output, for history event extraction.

    `--since` rather than `--tail N`: a verbose build can push thousands of
    lines between polls, and a fixed tail would scroll the "Running job" line
    out of view and lose the run entirely. Bounding by time cannot miss
    anything as long as the window exceeds the poll interval.
    """
    ok, out, _ = _docker("logs", "--since", f"{seconds}s", name, timeout=20)
    return out if ok else ""


_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
          "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _df_sizes(rows):
    """{"build_cache": ..., "images": ...} from _df_rows(), or None if
    unmeasurable. Kept distinct from _inner_df's "0B" fallback: prune() must
    be able to tell "empty" from "could not measure"."""
    if rows is None:
        return None
    return {"build_cache": rows.get("Build Cache", {}).get("Size", "0B"),
            "images": rows.get("Images", {}).get("Size", "0B")}


def prune(name, timeout=300):
    """Reclaim build cache and unused images inside one runner.

    Measures `docker system df` either side so the caller can report a real
    number rather than "done". Both prunes are attempted even if the first
    fails - they are independent, and reclaiming one of the two is better than
    neither - UNLESS the buildx prune itself times out, in which case a
    second prune is not issued (see below).

    `measured` tells the caller whether "before"/"after"/"freed_bytes" can be
    trusted. A failed `docker system df` is not read as "0B" here: that would
    either fabricate an empty after-state (inflating freed_bytes to the whole
    before-figure) or otherwise misreport what was reclaimed.

    Generous timeout: discarding tens of gigabytes of build cache is not fast,
    and a premature kill leaves the daemon mid-sweep.
    """
    # Narrow the check-then-act window the route opened. This cannot close it -
    # a job can still start between this check and the prune - but BuildKit
    # will not delete layers an in-flight build holds, so the residual risk is
    # a slower build, not a broken one.
    if not is_idle(name):
        return {"name": name, "ok": False,
                "error": f"{name} became busy before the prune started",
                "before": None, "after": None,
                "freed_bytes": None, "measured": False}

    before = _df_sizes(_df_rows(name))

    ok1, _, err1 = _docker("exec", name, "docker", "buildx", "prune", "-af",
                           timeout=timeout)
    timed_out = not ok1 and "timed out" in (err1 or "")
    if timed_out:
        # The exec client was killed; the daemon is very likely still sweeping.
        # Issuing a second prune now would race it, and measuring df would
        # report a number that means nothing.
        return {
            "name": name, "ok": False,
            "error": f"buildx prune {err1}; the runner's daemon may still be "
                     f"pruning. Re-check its usage in a few minutes.",
            "before": before,
            "after": None, "freed_bytes": None, "measured": False,
        }

    ok2, _, err2 = _docker("exec", name, "docker", "image", "prune", "-af",
                           timeout=timeout)

    after = _df_sizes(_df_rows(name))

    if before is None or after is None:
        return {
            "name": name,
            "ok": ok1 and ok2,
            "error": (err1 or err2) or None,
            "before": before,
            "after": after,
            "freed_bytes": None,
            "measured": False,
        }

    freed = ((parse_size(before["build_cache"]) + parse_size(before["images"]))
             - (parse_size(after["build_cache"]) + parse_size(after["images"])))

    return {
        "name": name,
        "ok": ok1 and ok2,
        "error": (err1 or err2) or None,
        "before": before,
        "after": after,
        # A build running elsewhere on the same daemon can grow the cache while
        # we prune, which would otherwise report a negative "freed".
        "freed_bytes": max(0, freed),
        "measured": True,
    }


def parse_size(s):
    """'1.41GiB' -> bytes. Returns 0 on anything unparseable."""
    if not s:
        return 0
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?i?B)\s*$", str(s), re.I)
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2).upper(), 1))
    except ValueError:
        return 0
