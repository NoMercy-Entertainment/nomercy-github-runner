"""Alarms: a self-hosted runner offline too long, a job no runner can take.

On 2026-09-30 the org's only runner with the `xcode` label - someone else's
Mac, registered in the GitHub org and not managed by this platform - was
offline for more than two hours and nobody noticed (GitHub #11). Nothing
here would have said so: the dashboard shows the runners it manages.

**Every runner a forge lists**, managed or not: the GitHub org's self-hosted
runners and the Forgejo runners the token's user can see.

**Timed from the first poll that saw it.** When a runner was first seen
offline is a row in control.db (`alarm_watch`), so a restart neither
forgets an outage nor starts its clock again. Raised once the condition has
lasted its threshold (ALARM_OFFLINE_MINUTES, ALARM_QUEUE_MINUTES), resolved
when it ends - a runner online again, or gone from the forge - and both are
written to the audit log.

**Blind is said, never guessed.** A forge that cannot be read marks the
monitor itself degraded ("cannot reach GitHub since ...") and neither
raises nor resolves an alarm about a runner it could not see: an unreadable
list is not every runner removed.

**Never on a page load.** A background thread reads the forges
(`run_forever`, `run_queue_forever`); a page only reads what it published.
"""
import json
import threading
import time
from datetime import datetime, timezone

from store import schema

#: (env key, default, minimum) of every setting, read from the deployment
#: each pass so a change needs no restart.
SETTINGS = {
    "offline_minutes": ("ALARM_OFFLINE_MINUTES", 10, 1),
    "queue_minutes": ("ALARM_QUEUE_MINUTES", 10, 1),
    "poll_seconds": ("ALARM_POLL_SECONDS", 60, 15),
    "queue_poll_seconds": ("ALARM_QUEUE_POLL_SECONDS", 300, 60),
    # Below this many GitHub calls left in the hour the queue check stops
    # until the limit resets. The controller and the history enricher share
    # the token, and a registration that cannot mint a token is worse than
    # a queue nobody watched for half an hour.
    "rate_floor": ("ALARM_RATE_LIMIT_FLOOR", 1000, 0),
}

FORGES = {"github": "GitHub", "forgejo": "Forgejo"}

ACTOR = "alarm-monitor"

#: One writer at a time: the minute thread and the queue thread both read a
#: row and then change it, and two raises of one alarm would be two audit
#: rows and two notifications.
_WRITE = threading.Lock()

#: Called with (path, event) for every raise and resolve once it is stored -
#: the webhook's outbox (alarm_notify.enqueue).
LISTENERS = []


def settings(env):
    out = {}
    for name, (key, default, minimum) in SETTINGS.items():
        raw = str((env or {}).get(key) or "").strip()
        try:
            value = int(raw)
        except ValueError:
            value = default
        out[name] = value if value >= minimum else default
    return out


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    """Seconds since the epoch of an ISO time, or None."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def message(kind, forge, subject, detail, since):
    """What an alarm says, in one line."""
    name = FORGES.get(forge, forge)
    detail = detail or {}
    if kind == "runner_offline":
        labels = ", ".join(detail.get("labels") or [])
        return (f"{name} runner {subject} has been offline since {since}"
                + (f" (labels: {labels})" if labels else ""))
    if kind == "job_queued":
        return (f"{subject} has been queued since {since} and no online runner "
                f"has its labels: {', '.join(detail.get('labels') or [])}")
    if kind == "monitor" and detail.get("part") == "queue":
        return f"alarm monitor cannot read {name}'s queued jobs since {since}"
    return f"alarm monitor cannot reach {name} since {since}"


class AlarmBook:
    """What is being watched, in control.db, and the raise and resolve of
    each. Every method takes `now` so a test can move the clock."""

    _ready = set()
    _ready_lock = threading.Lock()

    def __init__(self, path):
        self.path = path
        with self._ready_lock:
            if path not in self._ready:
                # The tables are CREATE IF NOT EXISTS: additive, and safe
                # beside the controller doing the same.
                schema.init(path)
                self._ready.add(path)

    # ---- reading ---------------------------------------------------------

    def rows(self):
        with schema.connect(self.path) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM alarm_watch ORDER BY since, alarm_key")]
        for row in rows:
            row["detail"] = json.loads(row["detail"]) if row["detail"] else {}
            row["message"] = message(row["kind"], row["forge"], row["subject"],
                                     row["detail"], row["since"])
        return rows

    def known_labels(self, forge):
        with schema.connect(self.path) as c:
            return {r[0] for r in c.execute(
                "SELECT label FROM alarm_labels WHERE forge=?", (forge,))}

    # ---- the one way a condition starts, lasts and ends ------------------

    def _watch(self, c, key, kind, forge, subject, detail, since, threshold, now):
        row = c.execute("SELECT * FROM alarm_watch WHERE alarm_key=?", (key,)).fetchone()
        if row is None:
            c.execute("INSERT INTO alarm_watch (alarm_key, kind, forge, subject,"
                      " detail, since) VALUES (?,?,?,?,?,?)",
                      (key, kind, forge, subject, json.dumps(detail), since))
            row = {"since": since, "raised_at": None}
        else:
            c.execute("UPDATE alarm_watch SET subject=?, detail=? WHERE alarm_key=?",
                      (subject, json.dumps(detail), key))
        started = epoch(row["since"])
        if row["raised_at"] is None and started is not None \
                and now - started >= threshold:
            c.execute("UPDATE alarm_watch SET raised_at=? WHERE alarm_key=?",
                      (iso(now), key))
            self._record(key, kind, forge, subject, detail, row["since"],
                         iso(now), "raised", None)

    def _resolve(self, c, key, now, reason):
        row = c.execute("SELECT * FROM alarm_watch WHERE alarm_key=?", (key,)).fetchone()
        if row is None:
            return
        c.execute("DELETE FROM alarm_watch WHERE alarm_key=?", (key,))
        if row["raised_at"]:
            detail = json.loads(row["detail"]) if row["detail"] else {}
            self._record(key, row["kind"], row["forge"], row["subject"], detail,
                         row["since"], row["raised_at"], "resolved", reason,
                         resolved_at=iso(now))

    def _record(self, key, kind, forge, subject, detail, since, raised_at,
                event, reason, resolved_at=None):
        """A raise or a resolve, held until the watch change is committed;
        `_flush` then writes the audit row and tells the listeners. An audit
        that cannot be written loses the record, never the alarm."""
        text = message(kind, forge, subject, detail, since)
        if reason:
            text = f"{text}; resolved: {reason}"
        self._pending.append({"event": event, "key": key, "kind": kind,
                              "forge": forge, "subject": subject, "detail": detail,
                              "since": since, "raised_at": raised_at,
                              "resolved_at": resolved_at, "reason": reason,
                              "message": text})

    def _flush(self):
        events, self._pending = self._pending, []
        for event in events:
            try:
                from control import audit
                audit.record(self.path, "alarm", event["event"], actor=ACTOR,
                             parameters={"alarm": event["key"], "kind": event["kind"],
                                         "forge": event["forge"]},
                             outcome=event["message"])
            except Exception as e:  # noqa: BLE001
                print(f"[alarms] audit not written: {type(e).__name__}")
            for listener in list(LISTENERS):
                try:
                    listener(self.path, event)
                except Exception as e:  # noqa: BLE001
                    print(f"[alarms] listener failed: {type(e).__name__}")
        return events

    def _begin(self):
        self._pending = []
        return schema.connect(self.path)

    # ---- the monitor itself ----------------------------------------------

    def _blind(self, c, forge, now, threshold, why, part="runners"):
        key = f"monitor:{forge}" + (":queue" if part == "queue" else "")
        self._watch(c, key, "monitor", forge, FORGES.get(forge, forge),
                    {"why": why, "part": part}, iso(now), threshold, now)

    def _sighted(self, c, forge, now, part="runners"):
        key = f"monitor:{forge}" + (":queue" if part == "queue" else "")
        self._resolve(c, key, now, f"{FORGES.get(forge, forge)} answered again")

    # ---- runners ---------------------------------------------------------

    def observe_runners(self, forge, runners, now, cfg, suppressed=(), why=None):
        """One reading of a forge's runners: each {"id", "name", "online",
        "labels"}, or None when the forge could not be read."""
        threshold = cfg["offline_minutes"] * 60
        with _WRITE, self._begin() as c:
            if runners is None:
                self._blind(c, forge, now, threshold, why or "no answer")
            else:
                self._sighted(c, forge, now)
                for label in {str(l).lower() for r in runners
                              for l in r.get("labels") or [] if l}:
                    c.execute("INSERT OR IGNORE INTO alarm_labels VALUES (?,?,?)",
                              (forge, label, iso(now)))
                seen = set()
                for runner in runners:
                    key = f"runner:{forge}:{runner['id']}"
                    seen.add(key)
                    if str(runner["id"]) in {str(s) for s in suppressed}:
                        self._resolve(c, key, now, "stopped on purpose on the platform")
                    elif runner.get("online"):
                        self._resolve(c, key, now, "back online")
                    else:
                        self._watch(c, key, "runner_offline", forge,
                                    runner.get("name") or str(runner["id"]),
                                    {"labels": list(runner.get("labels") or []),
                                     "id": runner["id"]},
                                    iso(now), threshold, now)
                for (key,) in c.execute(
                        "SELECT alarm_key FROM alarm_watch WHERE kind='runner_offline'"
                        " AND forge=?", (forge,)).fetchall():
                    if key not in seen:
                        self._resolve(c, key, now,
                                      f"removed from {FORGES.get(forge, forge)}")
        return self._flush()

    # ---- queued jobs -----------------------------------------------------

    def observe_queue_reader(self, forge, why, now, cfg):
        """Whether the queue could be read: `why` it could not, or None."""
        with _WRITE, self._begin() as c:
            if why:
                self._blind(c, forge, now, cfg["offline_minutes"] * 60, why, part="queue")
            else:
                self._sighted(c, forge, now, part="queue")
        return self._flush()

    def _queued(self, c, forge, job, now, threshold):
        key = f"queue:{forge}:{job['id']}"
        subject = (f"{job.get('repo')} · {job.get('workflow') or 'workflow'}"
                   f" / {job.get('name')}")
        detail = {"repo": job.get("repo"), "workflow": job.get("workflow"),
                  "job": job.get("name"), "labels": list(job.get("labels") or []),
                  "url": job.get("url"), "run_id": job.get("run_id")}
        since = iso(epoch(job.get("since")) or now)
        self._watch(c, key, "job_queued", forge, subject, detail, since, threshold, now)
        return key

    def observe_queue(self, forge, jobs, swept, online, now, cfg):
        """One sweep of the queue: `jobs` every queued job found, each
        {"id", "repo", "workflow", "name", "labels", "since", "url"}; `swept`
        the repositories read completely; `online` the label sets of the
        runners online now. A job alarm in a repository not swept stays."""
        threshold = cfg["queue_minutes"] * 60
        known = self.known_labels(forge)
        with _WRITE, self._begin() as c:
            waiting, taken = set(), set()
            for job in jobs:
                if hosted(job.get("labels"), known):
                    continue
                key = f"queue:{forge}:{job['id']}"
                if can_run(job.get("labels"), online):
                    taken.add(key)
                    continue
                waiting.add(self._queued(c, forge, job, now, threshold))
            for key, detail in c.execute(
                    "SELECT alarm_key, detail FROM alarm_watch WHERE kind='job_queued'"
                    " AND forge=?", (forge,)).fetchall():
                if key in waiting:
                    continue
                if key in taken:
                    self._resolve(c, key, now, "a runner with its labels is online")
                elif json.loads(detail or "{}").get("repo") in swept:
                    self._resolve(c, key, now, "no longer queued")
        return self._flush()

    def recheck_queue(self, forge, online, now, cfg):
        """Between sweeps, against the runners online now: a job a runner
        can take is resolved, one whose wait crossed the threshold raised."""
        threshold = cfg["queue_minutes"] * 60
        with _WRITE, self._begin() as c:
            for row in c.execute("SELECT * FROM alarm_watch WHERE kind='job_queued'"
                                 " AND forge=?", (forge,)).fetchall():
                detail = json.loads(row["detail"] or "{}")
                if can_run(detail.get("labels"), online):
                    self._resolve(c, row["alarm_key"], now,
                                  "a runner with its labels came online")
                else:
                    self._watch(c, row["alarm_key"], "job_queued", forge,
                                row["subject"], detail, row["since"], threshold, now)
        return self._flush()


def _lower(labels):
    return {str(l).strip().lower() for l in labels or [] if str(l).strip()}


def can_run(labels, online):
    """Whether one online runner carries every label the job asks for -
    GitHub's own rule, without regard to case."""
    wanted = _lower(labels)
    return any(wanted <= _lower(have) for have in online or [])


def hosted(labels, known):
    """Whether a job is a GitHub-hosted runner's: it asks for a label no
    self-hosted runner has ever carried (ubuntu-latest, a larger runner's
    name). A job that says self-hosted is never hosted - its runner may
    simply never have existed, which is exactly what should be said."""
    wanted = _lower(labels)
    if not wanted:
        return True
    if "self-hosted" in wanted:
        return False
    return not wanted <= _lower(known)


# ---------------------------------------------------------------------------
# the monitor: reads the forges, keeps the book, publishes what pages show
# ---------------------------------------------------------------------------

#: Which settings name a forge client, so a client - and the ETags it keeps -
#: lives as long as its credentials do.
_IDENTITY = {"github": ("GH_TOKEN", "GITHUB_ORG"),
             "forgejo": ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN")}

#: How long a forge must have been unreadable before the banner says so -
#: about three failed reads. One timeout must not turn every page red for a
#: minute; the webhook waits the full ALARM_OFFLINE_MINUTES.
BLIND_GRACE_SECONDS = 180

#: How long the org's repository list is used before it is read again.
REPOS_SECONDS = 3600

#: A platform runner in one of these desired states is offline on purpose.
#: `drained` too: provision.drain stops the runtime once the job is done,
#: so a drained runner is offline at the forge for as long as it stays so.
QUIET_STATES = ("stopped", "drained", "absent")

#: And whatever the desired state says, a runner the platform is in the
#: middle of taking down is offline on purpose. `stopped` and `failed` are
#: not here: a runner meant to run that is stopped or broken is the alarm.
QUIET_ACTUAL = ("draining", "drained", "stopping", "deregistering", "removing",
                "absent")


def _forge_client(forge, env):
    import providers
    provider = providers.by_key(forge)
    return provider.forge_client(env or {}) if provider else None


def _record(forge, r):
    """A forge's runner as the book reads it. Offline is only what the
    forge calls offline: a state it adds later must not page anyone."""
    if forge == "forgejo":
        labels = [l.get("name", "") if isinstance(l, dict) else str(l)
                  for l in r.get("labels") or []]
        return {"id": r.get("uuid"), "name": r.get("name") or r.get("uuid"),
                "online": str(r.get("status", "")).lower() != "offline",
                "labels": [l for l in labels if l]}
    return {"id": r.get("id"), "name": r.get("name") or str(r.get("id")),
            "online": str(r.get("status", "")).lower() != "offline",
            "labels": list(r.get("labels") or [])}


def _quiet(forge, specs):
    field = "registration_uuid" if forge == "forgejo" else "registration_id"
    return {str(s.get(field)) for s in specs or []
            if s.get("provider") == forge and s.get(field)
            and not s.get("deleted_at")
            and (s.get("desired_state") in QUIET_STATES
                 or s.get("actual_state") in QUIET_ACTUAL)}


class Monitor:
    def __init__(self, clients=None):
        self.clients = clients or _forge_client
        self._held = {}
        self.online = {}
        self.repos, self.repos_at = None, None
        self.paused_until = None
        self.status = {forge: {"configured": None} for forge in FORGES}
        self.status["queue"] = {"checked_at": None, "paused_until": None, "note": None}
        self.started_at = None
        self.checked_at = None
        self.note = None
        self._lock = threading.Lock()
        self._rows = []
        self._cfg = settings({})

    def _client(self, forge, env):
        identity = tuple((env or {}).get(k) for k in _IDENTITY[forge])
        held = self._held.get(forge)
        if held and held[0] == identity:
            return held[1]
        client = self.clients(forge, env)
        self._held[forge] = (identity, client)
        return client

    # ---- every minute ----------------------------------------------------

    def tick(self, path, env, specs, now):
        cfg = settings(env)
        book = AlarmBook(path)
        for forge in FORGES:
            client = self._client(forge, env)
            if client is None:
                self.online[forge] = None
                self.status[forge] = {"configured": False}
                continue
            try:
                records = client.all_runners()
                why = None if records is not None else "the runner list could not be read"
            except Exception as e:  # noqa: BLE001
                records, why = None, f"the runner list could not be read: {type(e).__name__}"
            runners = None if records is None else [_record(forge, r) for r in records]
            book.observe_runners(forge, runners, now, cfg, _quiet(forge, specs), why=why)
            if runners is None:
                self.online[forge] = None
                before = self.status.get(forge) or {}
                self.status[forge] = dict(before, configured=True, error=why,
                                          failing_since=before.get("failing_since")
                                          or iso(now))
                continue
            self.online[forge] = [_lower(r["labels"]) for r in runners if r["online"]]
            self.status[forge] = {"configured": True, "checked_at": iso(now),
                                  "runners": len(runners),
                                  "offline": sum(1 for r in runners if not r["online"])}
            book.recheck_queue(forge, self.online[forge], now, cfg)
        self.checked_at = now
        self.note = None
        self.publish(book, cfg)

    # ---- every few minutes -----------------------------------------------

    def _low(self, client, cfg, now):
        left = getattr(client, "rate_remaining", None)
        if left is None or left >= cfg["rate_floor"]:
            return False
        reset = getattr(client, "rate_reset", None)
        self.paused_until = reset if reset and reset > now else now + 900
        self.status["queue"].update(
            paused_until=iso(self.paused_until),
            note=f"paused: {left} GitHub calls left this hour, below the floor of "
                 f"{cfg['rate_floor']}")
        return True

    def sweep_queue(self, path, env, now):
        """Every queued job of every repository of the org, once. Forgejo's
        queue is not read: see docs/operations/runner-platform.md, Alarms."""
        cfg = settings(env)
        queue = self.status["queue"]
        client = self._client("github", env)
        if client is None:
            queue.update(note="GitHub is not configured")
            return
        if self.online.get("github") is None:
            queue.update(note="waiting for a reading of GitHub's runners")
            return
        if self.paused_until and now < self.paused_until:
            return
        self.paused_until = None
        queue.update(paused_until=None, note=None)
        if self._low(client, cfg, now):
            return
        book = AlarmBook(path)
        if self.repos is None or now - self.repos_at >= REPOS_SECONDS:
            repos = client.org_repos()
            if repos is None:
                why = "the org's repositories could not be read"
                book.observe_queue_reader("github", why, now, cfg)
                queue.update(note=why)
                self.publish(book, cfg)
                return
            self.repos, self.repos_at = repos, now
        # A run younger than this cannot hold a job that crosses the
        # threshold before the next sweep, so its jobs are not asked for.
        cutoff = now - max(0, cfg["queue_minutes"] * 60 - cfg["queue_poll_seconds"])
        jobs, swept, failed = [], set(), 0
        for repo in self.repos:
            if self._low(client, cfg, now):
                break
            runs = client.waiting_runs(repo)
            if runs is None:
                failed += 1
                continue
            complete = True
            for run in runs:
                created = epoch(run.get("created_at"))
                if created is not None and created > cutoff:
                    continue
                found = client.run_jobs(repo, run.get("id"))
                if found is None:
                    complete = False
                    continue
                jobs += [{"id": j.get("id"), "repo": repo,
                          "workflow": j.get("workflow_name") or run.get("name"),
                          "name": j.get("name"), "labels": j.get("labels") or [],
                          "since": j.get("created_at") or j.get("started_at")
                          or run.get("created_at"),
                          "url": j.get("html_url") or run.get("html_url"),
                          "run_id": run.get("id")}
                         for j in found if j.get("status") == "queued"]
            if complete:
                swept.add(repo)
        blind = failed and not swept and self.repos
        book.observe_queue_reader(
            "github", "no repository's runs could be read" if blind else None, now, cfg)
        book.observe_queue("github", jobs, swept, self.online.get("github") or [], now, cfg)
        queue.update(checked_at=iso(now), repositories=len(self.repos),
                     read=len(swept), queued=len(jobs))
        self.publish(book, cfg)

    # ---- what pages read -------------------------------------------------

    def publish(self, book, cfg):
        rows = book.rows()
        with self._lock:
            self._rows, self._cfg = rows, cfg

    def view(self, now=None):
        """Everything a page shows, from memory: never a forge, never a
        wait on the database."""
        now = time.time() if now is None else now
        with self._lock:
            rows, cfg = [dict(r) for r in self._rows], dict(self._cfg)
        for row in rows:
            row["for"] = duration(now - (epoch(row["since"]) or now))
        active = [r for r in rows if r["raised_at"] and r["kind"] != "monitor"]

        def shown(r):
            started = epoch(r["since"])
            return r["raised_at"] or (started is not None
                                      and now - started >= BLIND_GRACE_SECONDS)
        degraded = [r for r in rows if r["kind"] == "monitor" and shown(r)]
        young = [r for r in rows if r["kind"] == "monitor" and not shown(r)]
        if self.started_at is not None:
            last = self.checked_at or self.started_at
            if now - last > 3 * cfg["poll_seconds"] + 60:
                since = iso(last)
                why = self.note or "it has stopped checking"
                degraded.append({"alarm_key": "monitor:self", "kind": "monitor",
                                 "forge": None, "subject": "alarm monitor",
                                 "since": since, "raised_at": since,
                                 "for": duration(now - last), "detail": {"why": why},
                                 "message": f"alarm monitor has not checked since "
                                            f"{since}: {why}"})
        return {"alarms": active, "degraded": degraded,
                "pending": [r for r in rows
                            if not r["raised_at"] and r["kind"] != "monitor"] + young,
                "monitor": {"started_at": iso(self.started_at) if self.started_at else None,
                            "checked_at": iso(self.checked_at) if self.checked_at else None,
                            "note": self.note,
                            "github": dict(self.status.get("github") or {}),
                            "forgejo": dict(self.status.get("forgejo") or {}),
                            "queue": dict(self.status.get("queue") or {}),
                            "forgejo_queue": "not covered"},
                "thresholds": cfg}


def duration(seconds):
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} d {hours % 24} h"


MONITOR = Monitor()

#: Called after every minute pass with (path, env, now) - the webhook's
#: delivery of what is due.
TICK_LISTENERS = []


def snapshot(now=None):
    return MONITOR.view(now)


def run_forever(get_plane, monitor=None, sleep=time.sleep, clock=time.time):
    """The minute thread: runners, rechecks, notifications. `get_plane`
    answers (path, env, specs), or None while the control plane has not run;
    asked every pass, so a token set in Settings is used without a restart."""
    monitor = monitor or MONITOR
    monitor.started_at = clock()
    while True:
        cfg = settings({})
        try:
            plane = get_plane()
            if plane is None:
                monitor.note = "the control plane has not run yet"
            else:
                path, env, specs = plane
                cfg = settings(env)
                monitor.tick(path, env, specs, clock())
                for listener in list(TICK_LISTENERS):
                    listener(path, env, clock())
        except Exception as e:  # noqa: BLE001 - a pass never stops the loop
            monitor.note = f"the last check failed: {type(e).__name__}"
            print(f"[alarms] {type(e).__name__}: {e}")
        sleep(cfg["poll_seconds"])


def run_queue_forever(get_plane, monitor=None, sleep=time.sleep, clock=time.time):
    """The queue thread, on its own so a sweep of every repository never
    delays a runner check."""
    monitor = monitor or MONITOR
    while True:
        cfg = settings({})
        try:
            plane = get_plane()
            if plane is not None:
                path, env, _ = plane
                cfg = settings(env)
                monitor.sweep_queue(path, env, clock())
        except Exception as e:  # noqa: BLE001
            monitor.status["queue"]["note"] = f"the last sweep failed: {type(e).__name__}"
            print(f"[alarms] queue: {type(e).__name__}: {e}")
        sleep(cfg["queue_poll_seconds"])
