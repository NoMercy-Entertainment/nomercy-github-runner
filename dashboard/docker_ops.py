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

import forgejo_api
import providers

# Path to the repo AS THE DOCKER DAEMON SEES IT, not as this container sees it.
# Bind mounts are resolved by the daemon, so a path valid only inside the
# dashboard container would silently create an empty directory instead.
REPO_HOST_PATH = os.environ.get("REPO_HOST_PATH", "/mnt/d/docker-compose/GithubRunners")
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

def list_runners():
    """[(name, provider)] for every runner container on this engine.

    One `docker ps` for both fleets: `--format` exposes a single label through
    the `.Label` function, so names and providers arrive together rather than
    costing an inspect per container.

    Selection is by name prefix, not by the nomercy.runner label. The runners
    deployed today were created by compose, which never set that label, and a
    listing that required it would show an empty fleet.
    """
    ok, out, _ = _docker(
        "ps", "-a", "--format",
        '{{.Names}}\t{{.Label "' + providers.LABEL_PROVIDER + '"}}')
    if not ok:
        return []
    found = []
    for line in out.splitlines():
        name, _, label = line.partition("\t")
        name, label = name.strip(), label.strip()
        p = providers.from_label(label, name)
        # from_label can answer on the label alone; require the name to match
        # too, so a stray label on an unrelated container cannot enrol it into
        # a fleet whose action routes would then act on it.
        if p and name.startswith(p.prefix):
            found.append((name, p))
    return sorted(found, key=lambda t: (t[1].key != "github", t[1].key,
                                        _index_of(t[0])))


def list_runner_names():
    """Names only. app.py's guards ask nothing more than "does this exist"."""
    return [n for n, _ in list_runners()]


def _index_of(name):
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def next_free_index(provider):
    """Lowest unused index within one fleet. The two number independently."""
    used = {_index_of(n) for n, p in list_runners() if p is provider}
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


# "2026-08-20 13:27:33Z: Running job: build-base / docker-build" - the
# runner stamps its own lines, so the age of an event can be read straight off
# the line that was matched, without a second `docker logs --timestamps` pass.
RE_LOG_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})Z:")

# time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/q ..."
# The daemon logs a task starting and never logs it finishing, which is why
# Forgejo's busy/idle comes from the API and this pattern only names the job.
RE_FORGEJO_TASK = re.compile(
    r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z".*?'
    r'msg="task (\d+) repo is (\S+)')


def _outlived_by_container(name, line):
    """Whether this log line describes something the container has outlived.

    A job that began before the container's current start cannot still be
    running - the process that was running it is gone. Without this, a job
    killed mid-flight leaves its "Running job:" as the last event in the log
    for good, and the runner reads busy for ever.

    Both unknowns answer False, which keeps whatever the log alone said. That
    direction matters: is_idle() gates prune and drain, so a guess that
    invents "idle" would clear the build cache out from under a live job,
    while a guess that keeps "busy" only delays a sweep.
    """
    m = RE_LOG_STAMP.search(line)
    if not m:
        return False
    started = started_at(name)
    if not started:
        return False
    return f"{m.group(1)}T{m.group(2)}Z" < started


def _runner_file(name, provider):
    """The runner's registration file, parsed, or {} if unreadable.

    Read once per collect and passed to everything that needs it: the
    registration name, and - for Forgejo - the uuid that identifies this
    runner to the forge. Reading it twice would double the exec cost of every
    poll for no gain.
    """
    ok, out, _ = _docker("exec", name, "cat", provider.registration_path,
                         timeout=8)
    if not ok or not out:
        return {}
    try:
        # GitHub writes .runner with a UTF-8 BOM. json.loads chokes on it,
        # which silently turned every registration name into "-".
        return json.loads(out.lstrip("﻿"))
    except Exception:  # noqa: BLE001 - rendered, not raised
        return {}


def _forgejo_current_job(name):
    """A label for whatever task was last picked up. Display only."""
    ok, out, _ = _docker("logs", "--tail", "200", name, timeout=10)
    if not ok:
        return ""
    last = None
    for line in out.splitlines():
        m = RE_FORGEJO_TASK.search(line)
        if m:
            last = m
    return f"task {last.group(2)} - {last.group(3)}" if last else ""


def _forgejo_job_state(name, runner_file, forge_status):
    """busy/idle/unknown from Forgejo, which knows without being inferred.

    forge_status is {uuid: status} or None. None means the API could not be
    reached, and a runner Forgejo has never heard of is equally unknown - a
    container mid-registration has no answer yet, and answering "idle" for it
    would let prune act on a runner about to pick up work.
    """
    uuid = (runner_file or {}).get("uuid")
    if not uuid or forge_status is None:
        return "unknown", ""
    status = forge_status.get(uuid)
    if not status:
        return "unknown", ""
    if status == "active":
        return "busy", _forgejo_current_job(name)
    # idle and offline both mean "running no job". offline is separately
    # visible from the container being down, so it needs no third state here.
    # Anything else - a status word this code does not recognise, whether
    # from a future Forgejo release or a bug - is refused rather than assumed
    # harmless: a wrong "idle" here is what lets prune delete layers a live
    # job needs, so an unrecognised word must read as "unknown", not "idle".
    return ("idle", "") if status in ("idle", "offline") else ("unknown", "")


def _job_state(name, provider=None, runner_file=None, forge_status=None):
    """Derive busy/idle/unknown for a runner.

    provider defaults to GitHub so the single-argument form keeps working.

    The GitHub branch below takes the LAST job event of either kind.
    Grepping only for 'Running job' reports a finished job as still running
    whenever its completion line has scrolled out of the tail window.

    A failed `docker logs` (including its own 10s timeout) returns "unknown",
    not "idle". The two are not interchangeable: a runner mid-way through a
    heavy build is exactly the one whose log read is most likely to time out,
    so answering "idle" here would tell a destructive caller (prune) it is
    safe to act precisely when it cannot tell. Callers that need a strict
    safe/unsafe split (is_idle) already collapse "unknown" into "not idle";
    callers that show state to an operator (collect) treat it explicitly.
    """
    provider = provider or providers.GITHUB
    if provider is providers.FORGEJO:
        return _forgejo_job_state(name, runner_file, forge_status)

    ok, out, _ = _docker("logs", "--tail", "200", name, timeout=10)
    if not ok:
        return "unknown", ""
    last = ""
    for line in out.splitlines():
        if "Running job:" in line or re.search(r"Job .* completed", line):
            last = line
    if "Running job:" not in last:
        return "idle", ""
    if _outlived_by_container(name, last):
        return "idle", ""
    return "busy", last.split("Running job:", 1)[1].strip()


def _registration(name, provider=None, runner_file=None):
    provider = provider or providers.GITHUB
    rf = runner_file if runner_file is not None else _runner_file(name, provider)
    return rf.get(provider.registration_key) or "-"


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


# How long one fleet-wide Forgejo runner-status answer is reused.
#
# collect() runs every 5s, so uncached this endpoint was called ~17,000 times
# a day - and, worse, on the SHARED collector thread with a 20s HTTP timeout,
# so a slow or unreachable Forgejo stalled telemetry for the GITHUB fleet too.
# That cross-engine coupling is the exact thing this whole design exists to
# avoid, reached from the dashboard's side instead of Docker's.
#
# 10s, matching the drain watcher's own cadence: it bounds how stale a
# busy/idle badge can be to about two collector sweeps, which is under what a
# person watching the grid notices, while halving the call rate. The GitHub
# side's equivalent cache is 60s, but that one backs a page a human opens, not
# a badge that flips when a job starts.
_FORGE_STATUS_TTL = 10

# A FAILED call gets a longer window than a successful one. It must exceed
# forgejo_api's own HTTP timeout (read from there rather than assumed, so the
# two cannot drift apart): the deadline below is taken AFTER the call
# returns, so a call that pays the full timeout already spends that long
# before the window even starts. Backing failure off by only _FORGE_STATUS_TTL
# on top would still let a dead Forgejo cost the collector its 20s timeout
# roughly every 30s (20s blocking + 10s cached); this instead bounds a
# completely dead Forgejo to one 20s stall per 50s cycle (20s blocking + 30s
# cached) - worse case is a fixed cost per sweep, not an unbounded retry storm.
_FORGE_STATUS_FAIL_BACKOFF = forgejo_api.REQUEST_TIMEOUT + _FORGE_STATUS_TTL  # 30s

_forge_status_cache = None   # (monotonic deadline, records-or-None)


def _status_map(records):
    """{uuid: status} from the full records forgejo_api.runner_statuses() now
    returns. A separate step so the one call this cache exists to bound can
    still serve every caller that only ever wanted busy/idle, unchanged."""
    return {r["uuid"]: (r.get("status") or "")
            for r in (records or []) if isinstance(r, dict) and r.get("uuid")}


def _forge_records(env):
    """The whole Forgejo fleet's runner records, cached for a window that
    starts when the call FINISHES, not when it started.

    A failed call is NOT cached as though it were an answer - the hazard
    host_info() documents. What is stored on failure is None, which is
    already the explicit "we could not ask" sentinel every caller handles by
    reporting "unknown"; the previous good records are discarded rather than
    replayed, so a stale idle - or a stale Elsewhere card - can never outlive
    the call that produced it.

    The failed ATTEMPT is still rate-limited, deliberately, but by
    _FORGE_STATUS_FAIL_BACKOFF rather than _FORGE_STATUS_TTL. Taking the
    deadline from a `now` read BEFORE the call (the previous bug here) made a
    call slower than the TTL store a deadline already in the past by the time
    it was written, so a slow/unreachable Forgejo was retried, and paid its
    full 20s HTTP timeout, on every subsequent collector sweep - exactly the
    cross-engine stall on the GITHUB fleet's telemetry this cache exists to
    prevent. Reading the clock after the call fixes that; using a longer
    backoff for failures on top of it is what keeps a dead Forgejo from still
    costing a 20s stall every ~30s.

    This is the ONE call collect() makes to Forgejo per window. Both the
    busy/idle map (_forge_statuses, below) and the Elsewhere list (collect())
    are derived from what is cached here rather than fetched separately - two
    reductions of one answer, not two calls.
    """
    global _forge_status_cache
    now = time.monotonic()
    if _forge_status_cache is not None and now < _forge_status_cache[0]:
        return _forge_status_cache[1]
    client = providers.FORGEJO.forge_client(env)
    got = client.runner_statuses() if client is not None else None
    ttl = _FORGE_STATUS_TTL if got is not None else _FORGE_STATUS_FAIL_BACKOFF
    _forge_status_cache = (time.monotonic() + ttl, got)
    return got


def _forge_statuses(env):
    """{uuid: status} for the whole Forgejo fleet, or None if the forge could
    not be asked. Same cache window as _forge_records() - this only reduces
    what is already cached there, so calling this costs nothing extra."""
    records = _forge_records(env)
    return None if records is None else _status_map(records)


def collect(env=None):
    env = env or {}
    stats = _stats_map()
    draining = set(load_state().get("draining", []))
    runners = []
    found = list_runners()

    # One API call for the whole Forgejo fleet, not one per runner. Skipped
    # entirely when no Forgejo runner exists, so a GitHub-only deployment
    # never pays for a forge it does not use. forge_records feeds both the
    # busy/idle map below AND the Elsewhere list after the loop - one call,
    # two reductions of its answer.
    forge_status = None
    forge_records = None
    if any(p is providers.FORGEJO for _, p in found):
        forge_records = _forge_records(env)
        forge_status = None if forge_records is None else _status_map(forge_records)

    # uuids of every Forgejo runner that IS a container on this engine, so
    # the Elsewhere list below can tell "the forge knows about this and it is
    # one of ours" from "the forge knows about this and it is not". Only
    # populated from RUNNING containers: a stopped one's registration file is
    # behind `docker exec`, which needs a running container to answer, and
    # nothing else in collect() reads a stopped container's file either (its
    # "registration" field below is a bare "-" for the same reason).
    known_forgejo_uuids = set()

    for name, provider in found:
        ok, status, _ = _docker("ps", "-a", "--filter", f"name=^{name}$",
                                "--format", "{{.Status}}")
        status = status or "unknown"
        running = status.startswith("Up")

        if not running:
            runners.append({
                "name": name, "provider": provider.key, "registration": "-",
                "state": "stopped", "job": "", "uptime": status,
                "cpu_percent": 0, "mem_used": "0B", "mem_limit": "-",
                "build_cache": "0B", "images": "0B",
            })
            continue

        rf = _runner_file(name, provider)
        if provider is providers.FORGEJO:
            uuid = (rf or {}).get("uuid")
            if uuid:
                known_forgejo_uuids.add(uuid)
        job_state, job = _job_state(name, provider, rf, forge_status)
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
            "provider": provider.key,
            "registration": _registration(name, provider, rf),
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
        "elsewhere": _elsewhere(forge_records, known_forgejo_uuids),
    }


def _elsewhere(forge_records, known_uuids):
    """Forgejo runners the forge knows about that are not one of the
    containers on this engine - read-only cards on the status page, never a
    target for start/stop/prune/remove (those all reach a runner through
    ops.list_runner_names(), which this never feeds).

    Matched on uuid, like every other runner-identity comparison in this
    module - Forgejo documents runner names as not unique, so a name match
    could silently conflate two different runners.

    forge_records is None exactly when the forge could not be asked this
    window (see _forge_records). That must produce an empty list here, not a
    stale one from an earlier successful call and not a fabricated entry -
    the same "unknown answers unknown" discipline _job_state() and is_idle()
    already apply to this sentinel.
    """
    if forge_records is None:
        return []
    return [{
        "uuid": r.get("uuid"),
        "name": r.get("name") or "-",
        "status": r.get("status") or "unknown",
        "labels": ", ".join(r.get("labels") or []),
        "version": r.get("version") or "",
    } for r in forge_records if r.get("uuid") not in known_uuids]


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


def remove(name, provider=None, env=None):
    """Remove a runner, deregistering it from its forge first where needed.

    start.sh deregisters a GitHub runner on SIGTERM, so that side needs
    nothing here. forgejo-runner does not deregister on its own, and a removed
    container would leave a runner sitting at "offline" in Forgejo for ever.

    A failed deregistration does not block the removal. The operator asked for
    the container to be gone, and a forge that is not answering must not be
    able to veto that; the stale registration is visible and deletable in
    Forgejo's own UI.
    """
    provider = provider or providers.GITHUB
    if provider is providers.FORGEJO:
        try:
            client = provider.forge_client(env or {})
            if client is not None:
                rf = _runner_file(name, provider)
                # The id in .runner is written at registration. Falling back
                # to the live map covers a file that is missing or stale.
                runner_id = rf.get("id")
                if not runner_id and rf.get("uuid"):
                    runner_id = (client.runner_ids() or {}).get(rf["uuid"])
                if runner_id:
                    client.delete_runner(runner_id)
        except Exception as e:  # noqa: BLE001 - never blocks the removal
            print(f"[forgejo:deregister:{name}] {e}")

    # -v removes ANONYMOUS volumes only, never named ones (docker rm's own
    # documented behaviour: "Remove anonymous volumes associated with the
    # container"). That distinction is what makes this safe: forgejo-runner-1
    # in docker-compose.runners.yml mounts the NAMED volume
    # forgejo-runner-1-data, which -v cannot touch, while a runner this
    # dashboard created mounts nothing at /data - so the VOLUME /data in
    # forgejo-runner/Dockerfile made Docker create an anonymous volume per
    # container, and every remove/recreate left one behind, dangling, on the
    # engine whose disk containment is the entire point of this deployment.
    #
    # 180s: these containers hold a nested daemon, and tearing that down on
    # removal has been observed to take ~110s. A timeout here is read by the
    # caller as "removal failed" even when it eventually succeeds, so the
    # margin matters more than it looks like it should.
    ok, out, err = _docker("rm", "-f", "-v", name, timeout=180)
    set_draining(name, False)
    return ok, out, err


def create(index, env, provider=None):
    """Create a runner container. Returns (ok, name, message)."""
    provider = provider or providers.GITHUB
    name = provider.name_for(index)

    container_env, err = provider.container_env(env, name)
    if err:
        # No container is started on a half-built environment: a Forgejo
        # runner without a registration token boots, fails, and restarts for
        # ever under `restart: unless-stopped`.
        return False, name, err

    args = [
        "run", "-d",
        "--name", name,
        "--label", LABEL,
        "--label", f"{providers.LABEL_PROVIDER}={provider.key}",
        "--privileged",
        "--restart", "unless-stopped",
        "--stop-timeout", "60",
        "--tmpfs", "/tmp",
    ]
    if provider is providers.GITHUB:
        args += ["-v", f"{REPO_HOST_PATH}/scripts/start.sh:/root/start.sh:ro"]
    for k, v in container_env.items():
        args += ["-e", f"{k}={v}"]

    cpu = (env.get("RUNNER_CPU_LIMIT") or "0").strip()
    mem = (env.get("RUNNER_MEM_LIMIT") or "0").strip()
    if cpu not in ("", "0"):
        args += ["--cpus", cpu]
    if mem not in ("", "0"):
        args += ["--memory", mem]
    args.append(provider.image)

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


def is_idle(name, provider=None, forge_status=None, env=None):
    """True only for a definite "idle". "busy" and "unknown" both answer
    False - this gates both the drain watcher (worst case: an early stop,
    which just restarts) and cache prune (worst case: deleting layers a
    running job needs), and the two callers cannot be told apart from here.

    forge_status is fetched here when the caller has none. prune() and the
    drain watcher both call is_idle() holding no status of their own -
    collect() is the one place that already has one for the whole fleet, and
    it calls _job_state() directly instead of coming through here. Without
    this fetch, prune() and the drain watcher would read every Forgejo runner
    as unknown and refuse to act on it for ever.
    """
    provider = provider or providers.GITHUB
    rf = None
    if provider is providers.FORGEJO:
        rf = _runner_file(name, provider)
        if forge_status is None:
            client = provider.forge_client(env or {})
            if client is not None:
                # client.runner_statuses() answers with the full records
                # (see forgejo_api.Forgejo.runner_statuses) - reduced here to
                # the {uuid: status} map _job_state() expects, exactly like
                # the cached path in _forge_statuses() does.
                records = client.runner_statuses()
                forge_status = None if records is None else _status_map(records)
    state, _ = _job_state(name, provider, rf, forge_status)
    return state == "idle"


def _bare(provider):
    """Whether a provider-aware call should be made in its GitHub form.

    THE convention, in one place. Five call sites had each grown their own
    copy of `if provider is GITHUB: <bare call> else: <full call>`, each with
    its own paragraph explaining it, while a sixth - the drain watcher -
    called the full form for both fleets, so the codebase stated the same
    rule twice in opposite directions. Now nothing chooses; everything goes
    through the three wrappers below.

    Why the distinction exists at all, given it changes no behaviour:
    is_idle(), remove() and create() are deliberately easy seams to stub, and
    the stubs in this suite are written to the GitHub signature -
    tests/test_docker_ops.py stubs `is_idle` four times as `lambda n: True`,
    tests/test_routes.py stubs `remove` as `fake_remove(name)` and `create`
    as `fake_create(idx, env)`. Those files are pre-existing and not editable
    here. Passing the extra arguments on the GitHub path would break every
    one of them for nothing: all three functions already default provider to
    GITHUB and only consult env on the Forgejo path, so those arguments are
    exactly what Forgejo needs and exactly what GitHub ignores.

    `provider is None` takes the GitHub branch, because all three default it
    that way themselves.
    """
    return provider is None or provider is providers.GITHUB


def idle_check(name, provider=None, env=None):
    """is_idle() for a caller that holds no forge status of its own.

    Every is_idle() caller outside collect() comes through here - prune(),
    both prune routes, and the drain watcher. See _bare() for the convention.
    """
    if _bare(provider):
        return is_idle(name)
    return is_idle(name, provider, env=env)


def remove_runner(name, provider=None, env=None):
    """remove() under the convention _bare() documents."""
    if _bare(provider):
        return remove(name)
    return remove(name, provider, env)


def create_runner(index, env, provider=None):
    """create() under the convention _bare() documents."""
    if _bare(provider):
        return create(index, env)
    return create(index, env, provider)


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


def prune(name, timeout=300, provider=None, env=None):
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
    if not idle_check(name, provider, env):
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
