"""A message for every job: when it starts, and how it ended (2026-10-27).

The owner asked for every job on both forges and every platform in Discord:
a short message when it starts - forge, repository, workflow, job, runner,
a link - and one when it ends with its real result, its duration and a
colour. About 500 GitHub jobs a day, so each is compact (alarm_notify
puts ten to a post) and nothing here ever waits on Discord.

The jobs are the run history's native rows (history.py), which the job
events open and close and whose results come from where they really are:
the runner's event, GitHub's job conclusion, the Forgejo task. A row whose
result is not known yet when it closes is said when it is. Each message is
one outbox row, unique on ("job:<forge>:<run id>", started|finished, start),
so a second scan or a restart never says a job twice.

Settings (platform_settings, the Settings page): job messages all, failures
only or off; runner online/offline messages on or off. All on by default.
"""
import time

import alarms
import alarm_notify
from store import schema

MODES = ("all", "failures", "off")

#: A job already over by more than this when it is first seen gets only its
#: finished message: a start said long after the fact is noise.
LATE_START = 600

#: How far back a scan looks. Results come within a day (app._fill_*).
WINDOW = 2 * 86400

#: Results that are not a failure, for "failures only".
FINE = ("Succeeded", "Skipped")

#: How often the thread scans.
SCAN_SECONDS = 10

_details_cache = {}
_clients = {}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _read(path):
    with schema.connect(path) as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT key, value FROM platform_settings WHERE key LIKE 'notify_%'")}


def _write(c, key, value):
    c.execute("INSERT INTO platform_settings(key, value) VALUES(?, ?)"
              " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_settings(path):
    """{"jobs": all|failures|off, "runners": bool, "since": iso or None}."""
    try:
        raw = _read(path)
    except Exception:  # noqa: BLE001 - no table yet reads as the defaults
        raw = {}
    jobs = raw.get("notify_jobs")
    return {"jobs": jobs if jobs in MODES else "all",
            "runners": raw.get("notify_runners", "on") != "off",
            "since": raw.get("notify_jobs_since")}


def set_settings(path, jobs=None, runners=None, since=None, now=None):
    """Change what is sent. Turning job messages on starts them from now:
    jobs that ran while they were off are not posted after the fact."""
    if jobs is not None and jobs not in MODES:
        raise ValueError(f"job messages are one of {list(MODES)}")
    before = get_settings(path)
    with schema.connect(path) as c:
        if jobs is not None:
            _write(c, "notify_jobs", jobs)
            if before["jobs"] == "off" and jobs != "off" and since is None:
                since = alarms.iso(time.time() if now is None else now)
        if runners is not None:
            _write(c, "notify_runners", "on" if runners else "off")
        if since is not None:
            _write(c, "notify_jobs_since", since)
    return get_settings(path)


# ---------------------------------------------------------------------------
# what a job is called, where it is, and its link
# ---------------------------------------------------------------------------

def _client(provider, env):
    import providers
    keys = {"github": ("GH_TOKEN", "GITHUB_ORG"),
            "forgejo": ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN")}[provider]
    identity = tuple((env or {}).get(k) for k in keys)
    held = _clients.get(provider)
    if held and held[0] == identity:
        return held[1]
    p = providers.by_key(provider)
    client = p.forge_client(env or {}) if p else None
    _clients[provider] = (identity, client)
    return client


def lookup(env, run):
    """The workflow, the job's display name and its link, from the forge:
    GitHub's job in that run attempt, or the Forgejo task. {} when the forge
    cannot say; the message then carries what the history row knows."""
    client = _client(run["provider"], env)
    if client is None:
        return {}
    if run["provider"] == "github" and hasattr(client, "job_details"):
        found = client.job_details(run.get("gh_repo"), run.get("gh_run_id"),
                                   run.get("attempt") or "1", run.get("registration"),
                                   run.get("started_at"))
        if found:
            return {"workflow": found.get("workflow"), "job": found.get("name"),
                    "url": found.get("url")}
    if run["provider"] == "forgejo" and hasattr(client, "task_info") and run.get("forge_task_id"):
        found = client.task_info(run.get("gh_repo"), int(run["forge_task_id"]))
        if found:
            return {"workflow": found.get("workflow"), "job": found.get("name"),
                    "url": found.get("url")}
    return {}


def _details(env, run, details):
    if run["id"] in _details_cache:
        return _details_cache[run["id"]]
    try:
        found = details(env, run) or {}
    except Exception as e:  # noqa: BLE001 - a name is never worth a lost message
        print(f"[jobs] details of run {run['id']}: {type(e).__name__}")
        found = {}
    if found:
        if len(_details_cache) > 2000:
            _details_cache.clear()
        _details_cache[run["id"]] = found
    return found


def _fallback_url(env, run):
    if run["provider"] == "github" and run.get("gh_repo") and run.get("gh_run_id"):
        return f"https://github.com/{run['gh_repo']}/actions/runs/{run['gh_run_id']}"
    base = str((env or {}).get("FORGEJO_INSTANCE_URL") or "").rstrip("/")
    if run["provider"] == "forgejo" and base and run.get("gh_repo"):
        return f"{base}/{run['gh_repo']}/actions"
    return None


def _event(env, run, state, found):
    return {"event": state, "key": f"job:{run['provider']}:{run['id']}", "kind": "job",
            "forge": run["provider"], "repo": run.get("gh_repo"),
            "workflow": found.get("workflow") or run.get("gh_workflow"),
            "job": found.get("job") or run.get("job_name"),
            "runner": run.get("registration") or run.get("runner"),
            "url": (run.get("gh_url") if state == "finished" else None)
            or found.get("url") or _fallback_url(env, run),
            "result": run.get("result") if state == "finished" else None,
            "duration_s": run.get("duration_s") if state == "finished" else None,
            "started_at": run.get("started_at"), "ended_at": run.get("ended_at"),
            "raised_at": run.get("started_at")}


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------

def _runs(since):
    import history
    with history._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM runs WHERE provider IN ('github', 'forgejo')"
            " AND runner_id IS NOT NULL AND started_at >= ?"
            " ORDER BY started_at LIMIT 1000", (since[:19],))]
        for row in rows:
            try:
                found = c.execute("SELECT attempt FROM native_job_runs WHERE history_id=?",
                                  (row["id"],)).fetchone()
            except Exception:  # noqa: BLE001 - no native ledger in this db
                found = None
            row["attempt"] = str(found[0]) if found and found[0] else "1"
        return rows


def scan(path, env, now, details=lookup):
    """Queue every job message that is due and not queued before."""
    cfg = get_settings(path)
    if cfg["since"] is None:
        # The first scan ever: from now, not the whole history.
        cfg = set_settings(path, since=alarms.iso(now))
    if cfg["jobs"] == "off":
        return 0
    since = max(cfg["since"], alarms.iso(now - WINDOW))
    with schema.connect(path) as c:
        done = {(r[0], r[1]) for r in c.execute(
            "SELECT alarm_key, event FROM alarm_outbox WHERE alarm_key LIKE 'job:%'"
            " AND raised_at >= ?", (since[:19],))}
    queued = 0
    for run in _runs(since):
        key = f"job:{run['provider']}:{run['id']}"
        if cfg["jobs"] == "all" and (key, "started") not in done:
            ended = alarms.epoch(run.get("ended_at"))
            if ended is None or now - ended <= LATE_START:
                alarm_notify.enqueue(path, _event(env, run, "started",
                                                  _details(env, run, details)), now=now)
                queued += 1
        if run.get("result") and (key, "finished") not in done:
            if cfg["jobs"] == "failures" and run["result"] in FINE:
                continue
            alarm_notify.enqueue(path, _event(env, run, "finished",
                                              _details(env, run, details)), now=now)
            queued += 1
    return queued


def run_forever(get_plane, sleep=time.sleep, clock=time.time):
    """The job message thread: a scan every SCAN_SECONDS, apart from the
    delivery thread and from every page."""
    while True:
        try:
            plane = get_plane()
            if plane is not None:
                path, env, _ = plane
                scan(path, env, clock())
        except Exception as e:  # noqa: BLE001 - a pass never stops the loop
            print(f"[jobs] scan: {type(e).__name__}: {e}")
        sleep(SCAN_SECONDS)
