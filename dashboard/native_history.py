"""Replay the durable job ledger into history without reading runner logs."""
from datetime import datetime, timezone

import history
from control.job_events import _within_worker_clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS native_job_runs (
 runner_id TEXT NOT NULL, provider TEXT NOT NULL, forge_run TEXT NOT NULL,
 attempt TEXT NOT NULL, job_id TEXT, job_name TEXT NOT NULL, history_id INTEGER NOT NULL,
 source TEXT NOT NULL, at TEXT NOT NULL,
 FOREIGN KEY(history_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_native_job_identity
 ON native_job_runs(runner_id,provider,forge_run,attempt,job_id);
CREATE TABLE IF NOT EXISTS native_job_names(history_id INTEGER NOT NULL,job_name TEXT NOT NULL,
 UNIQUE(history_id,job_name),FOREIGN KEY(history_id) REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS native_job_ids(history_id INTEGER NOT NULL,job_id TEXT NOT NULL,
 UNIQUE(history_id,job_id),FOREIGN KEY(history_id) REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS native_job_cursors(control_path TEXT PRIMARY KEY,sequence INTEGER NOT NULL);
"""
RESULT = {"success": "Succeeded", "failure": "Failed", "cancelled": "Canceled",
          "timed_out": "TimedOut", "skipped": "Skipped", "neutral": "Neutral",
          "action_required": "ActionRequired", "stale": "Stale"}


def _stamp(at):
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _find_run(connection, base, job, job_id, event):
    if job_id:
        exact = connection.execute("SELECT n.* FROM native_job_runs n JOIN native_job_ids ids ON ids.history_id=n.history_id "
                                   "WHERE n.runner_id=? AND n.provider=? AND n.forge_run=? AND n.attempt=? "
                                   "AND ids.job_id=? ORDER BY n.history_id DESC LIMIT 1", (*base, job_id)).fetchone()
        if exact:
            return exact
    candidates = connection.execute("SELECT n.*,r.started_at,r.ended_at FROM native_job_runs n JOIN runs r ON r.id=n.history_id "
                                    "WHERE n.runner_id=? AND n.provider=? AND n.forge_run=? AND n.attempt=? "
                                    "ORDER BY n.history_id DESC LIMIT 20", base).fetchall()
    eligible = []
    instant = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
    for candidate in candidates:
        aliases = {row[0] for row in connection.execute("SELECT job_id FROM native_job_ids WHERE history_id=?",
                                                      (candidate["history_id"],))}
        if job_id:
            same_kind = {value for value in aliases if value.startswith("hook:") == job_id.startswith("hook:")}
            if same_kind and job_id not in same_kind:
                continue  # two known IDs are distinct, regardless of matching names
        if event["state"] == "started" and candidate["ended_at"] and event["at"] > candidate["at"]:
            continue
        names = {row[0] for row in connection.execute("SELECT job_name FROM native_job_names WHERE history_id=?",
                                                     (candidate["history_id"],))}
        # A same-kind unknown legacy ID may bind by name; otherwise only
        # the one current unbound hook/forge execution on this runner can bind.
        if job in names or (base[2] and (not candidate["ended_at"] or job_id and aliases or
                                       event.get("source") == "forge" or event["state"] == "finished")):
            eligible.append(candidate)
    # Strong IDs are handled above. Cross-kind aliases must fit this
    # execution's original time interval, regardless of webhook arrival order.
    # Multiple plausible candidates stay separate; never guess by newest row.
    groups = [[], [], []]
    for candidate in eligible:
        started = datetime.fromisoformat(candidate["started_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(candidate["ended_at"].replace("Z", "+00:00")) if candidate["ended_at"] else None
        endpoint = started if event["state"] == "started" else ended
        if endpoint == instant:
            groups[0].append(candidate)
        elif started <= instant and (ended is None or instant <= ended):
            groups[1].append(candidate)
        elif event["state"] == "started" and 0 <= (started - instant).total_seconds() <= 30:
            groups[2].append(candidate)
        elif event["state"] == "finished" and ended and 0 <= (instant - ended).total_seconds() <= 30:
            groups[2].append(candidate)
    for group in groups:
        if group:
            return group[0] if len(group) == 1 else None
    return None


class NativeHistory:
    def __init__(self, control_path):
        self.path = str(control_path)
        with history._lock, history._conn() as connection:
            connection.executescript(SCHEMA)

    def sequence(self):
        with history._lock, history._conn() as connection:
            row = connection.execute("SELECT sequence FROM native_job_cursors WHERE control_path=?", (self.path,)).fetchone()
            return row[0] if row else 0

    def record(self, spec, event):
        """Projection and cursor commit together; replay after a crash is harmless."""
        with history._lock, history._conn() as connection:
            row = connection.execute("SELECT sequence FROM native_job_cursors WHERE control_path=?", (self.path,)).fetchone()
            if row and row[0] >= event["sequence"]:
                return
            if event.get("source") == "forge" or _within_worker_clock(event, event["received_at"]):
                # A runner removed between receipt and projection still owns
                # its durable historical identity; its audit must not disappear.
                self._project(connection, spec or {"display_name": event["runner_id"]}, event)
            connection.execute("INSERT OR REPLACE INTO native_job_cursors VALUES(?,?)", (self.path, event["sequence"]))

    def _project(self, connection, spec, event):
        rid, provider = event["runner_id"], event["provider"]
        forge_run, attempt = event.get("run_id") or "", event.get("run_attempt") or "1"
        job, job_id = event.get("job") or "job", event.get("job_id")
        base = (rid, provider, forge_run, attempt)
        row = _find_run(connection, base, job, job_id, event)
        at = _stamp(event["at"])
        source = event.get("source") or "worker"
        if row is None:
            name = spec.get("display_name") or rid
            # Identity includes runner_id even if a renamed/deleted runner
            # once held the same display name and timestamp.
            cursor = connection.execute("INSERT OR IGNORE INTO runs(runner,registration,job_name,started_at,provider,"
                                        "runner_id,forge_task_id,gh_checked,gh_run_id,gh_repo,gh_sha) "
                                        "VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                                        (rid, name, job, at, provider, rid,
                                         int(job_id) if provider == "forgejo" and str(job_id).isdigit() else None,
                                         int(forge_run) if forge_run.isdigit() else None, event.get("repo"), event.get("commit")))
            history_id = cursor.lastrowid or connection.execute(
                "SELECT id FROM runs WHERE runner=? AND job_name=? AND started_at=?", (rid, job, at)).fetchone()[0]
            connection.execute("INSERT INTO native_job_runs VALUES(?,?,?,?,?,?,?,?,?)",
                               (*base, job_id, job, history_id, source, event["at"]))
            connection.execute("INSERT OR IGNORE INTO native_job_names VALUES(?,?)", (history_id, job))
            if job_id:
                connection.execute("INSERT OR IGNORE INTO native_job_ids VALUES(?,?)", (history_id, job_id))
        else:
            history_id = row["history_id"]
            run = connection.execute("SELECT ended_at,result FROM runs WHERE id=?", (history_id,)).fetchone()
            connection.execute("INSERT OR IGNORE INTO native_job_names VALUES(?,?)", (history_id, job))
            if job_id:
                connection.execute("INSERT OR IGNORE INTO native_job_ids VALUES(?,?)", (history_id, job_id))
            if event["at"] < row["at"] and source != "forge":
                return
            if row["source"] == "forge" and source != "forge" and run["ended_at"]:
                return  # a result-less local completion cannot erase signed proof
            if event["state"] == "started" and run["ended_at"]:
                if source == "forge":
                    connection.execute("UPDATE native_job_runs SET job_id=COALESCE(?,job_id),job_name=?,source='forge' "
                                       "WHERE history_id=?", (job_id, job, history_id))
                    connection.execute("UPDATE runs SET job_name=? WHERE id=?", (job, history_id))
                return
            connection.execute("UPDATE native_job_runs SET job_id=COALESCE(?,job_id),job_name=?,source=?,at=? "
                               "WHERE history_id=?", (job_id, job if source == "forge" else row["job_name"], source, event["at"], history_id))
            if source == "forge":
                connection.execute("UPDATE runs SET job_name=? WHERE id=?", (job, history_id))
        if event["state"] == "finished":
            started = connection.execute("SELECT started_at FROM runs WHERE id=?", (history_id,)).fetchone()[0]
            aggregate = connection.execute("SELECT COUNT(*) n,MIN(cpu) cmin,MAX(cpu) cmax,AVG(cpu) cavg,"
                                           "MIN(mem) mmin,MAX(mem) mmax,AVG(mem) mavg FROM samples WHERE run_id=?", (history_id,)).fetchone()
            duration = max(0, int((datetime.fromisoformat(at.replace("Z", "+00:00")) -
                                  datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()))
            connection.execute("UPDATE runs SET ended_at=?,duration_s=?,result=?,gh_conclusion=?,"
                               "cpu_min=?,cpu_max=?,cpu_avg=?,mem_min=?,mem_max=?,mem_avg=?,samples=? WHERE id=?",
                               (at, duration, RESULT.get(event.get("result")), event.get("result"),
                                aggregate["cmin"], aggregate["cmax"], aggregate["cavg"], aggregate["mmin"], aggregate["mmax"],
                                int(aggregate["mavg"] or 0), aggregate["n"], history_id))
