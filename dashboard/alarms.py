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

#: Called with every raise and resolve once it is stored - the webhook.
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
        """The audit row of a raise or a resolve. Its own connection, after
        the watch change: an audit that cannot be written loses the record,
        never the alarm."""
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
                    listener(event)
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
        with self._begin() as c:
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
