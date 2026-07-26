"""Job run history.

One SQLite file in the data volume. Two tables:

  runs    - one row per job: which runner, what job, when it started and
            ended, how it finished, resource summary, and GitHub context
  samples - CPU/memory every ~5s while a job is running, so each run can be
            graphed rather than just totalled

Job events come from the runner's own log lines, which carry their own
timestamps ("2026-07-26 13:47:31Z: Running job: jvm-android"). Parsing those is
exact; inferring start/end from when a poll happened to notice would be off by
up to the poll interval and would lose events entirely across a restart.

Reprocessing the same log line is harmless: (runner, job_name, started_at) is
UNIQUE and inserts are INSERT OR IGNORE.
"""

import os
import re
import sqlite3
import threading

DB_PATH = os.path.join(os.environ.get("DASH_DATA", "/data"), "history.db")

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  runner        TEXT    NOT NULL,
  registration  TEXT,
  job_name      TEXT    NOT NULL,
  started_at    TEXT    NOT NULL,
  ended_at      TEXT,
  duration_s    INTEGER,
  result        TEXT,
  cpu_min REAL, cpu_max REAL, cpu_avg REAL,
  mem_min INTEGER, mem_max INTEGER, mem_avg INTEGER,
  samples       INTEGER DEFAULT 0,
  gh_checked    INTEGER DEFAULT 0,
  gh_run_id     INTEGER,
  gh_repo       TEXT,
  gh_workflow   TEXT,
  gh_branch     TEXT,
  gh_sha        TEXT,
  gh_actor      TEXT,
  gh_url        TEXT,
  gh_conclusion TEXT,
  UNIQUE(runner, job_name, started_at)
);
CREATE INDEX IF NOT EXISTS idx_runs_started  ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_runner   ON runs(runner, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_job      ON runs(job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_open     ON runs(ended_at) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS samples (
  run_id INTEGER NOT NULL,
  t      TEXT    NOT NULL,
  cpu    REAL,
  mem    INTEGER,
  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    # WAL: the collector writes every 5s while the UI reads. Without it a
    # read can block a write long enough to stall the poll loop.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


# --------------------------------------------------------------------------
# log parsing
# --------------------------------------------------------------------------

# 2026-07-26 13:47:31Z: Running job: jvm-android
RE_START = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z:\s*Running job:\s*(.+?)\s*$")
# 2026-07-26 13:52:02Z: Job jvm-android completed with result: Succeeded
RE_END = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z:\s*Job\s+(.+?)\s+completed with result:\s*(\S+)")


def parse_events(text):
    """Extract (kind, iso_time, job, result) from a chunk of runner log."""
    events = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        m = RE_START.search(line)
        if m:
            events.append(("start", m.group(1).replace(" ", "T") + "Z",
                           m.group(2), None))
            continue
        m = RE_END.search(line)
        if m:
            events.append(("end", m.group(1).replace(" ", "T") + "Z",
                           m.group(2), m.group(3)))
    return events


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def open_run(runner, registration, job_name, started_at):
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO runs (runner, registration, job_name, started_at)"
            " VALUES (?,?,?,?)",
            (runner, registration, job_name, started_at))


def close_run(runner, job_name, ended_at, result):
    """Close the most recent still-open run matching this runner and job."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id, started_at FROM runs"
            " WHERE runner=? AND job_name=? AND ended_at IS NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (runner, job_name)).fetchone()
        if not row:
            # No OPEN run for this completion. Two very different reasons:
            #
            #  1. We already recorded and closed this exact run, and are seeing
            #     the same log line again - every restart re-reads the logs.
            #     Must be a no-op, or each restart injects a duplicate
            #     zero-second row. (Observed: one restart turned 78 real runs
            #     into 156 rows, half of them bogus.)
            #  2. The start genuinely never reached us - it scrolled out of the
            #     log window, or the dashboard was down when the job began.
            #     Worth recording, with start == end so it is visibly partial.
            already = c.execute(
                "SELECT 1 FROM runs WHERE runner=? AND job_name=? AND ended_at=?",
                (runner, job_name, ended_at)).fetchone()
            if already:
                return
            c.execute(
                "INSERT OR IGNORE INTO runs"
                " (runner, job_name, started_at, ended_at, duration_s, result)"
                " VALUES (?,?,?,?,0,?)",
                (runner, job_name, ended_at, ended_at, result))
            return

        dur = _seconds_between(row["started_at"], ended_at)
        agg = c.execute(
            "SELECT COUNT(*) n, MIN(cpu) cmin, MAX(cpu) cmax, AVG(cpu) cavg,"
            "       MIN(mem) mmin, MAX(mem) mmax, AVG(mem) mavg"
            " FROM samples WHERE run_id=?", (row["id"],)).fetchone()

        c.execute(
            "UPDATE runs SET ended_at=?, duration_s=?, result=?,"
            " cpu_min=?, cpu_max=?, cpu_avg=?,"
            " mem_min=?, mem_max=?, mem_avg=?, samples=?"
            " WHERE id=?",
            (ended_at, dur, result,
             agg["cmin"], agg["cmax"], agg["cavg"],
             agg["mmin"], agg["mmax"], int(agg["mavg"] or 0), agg["n"],
             row["id"]))


def add_sample(runner, cpu, mem, when):
    """Attach a resource sample to whatever run is open on this runner."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id FROM runs WHERE runner=? AND ended_at IS NULL"
            " ORDER BY started_at DESC LIMIT 1", (runner,)).fetchone()
        if not row:
            return
        c.execute("INSERT INTO samples (run_id,t,cpu,mem) VALUES (?,?,?,?)",
                  (row["id"], when, cpu, mem))


def _seconds_between(a, b):
    import datetime as dt
    try:
        fa = dt.datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ")
        fb = dt.datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ")
        return max(0, int((fb - fa).total_seconds()))
    except Exception:
        return None


# --------------------------------------------------------------------------
# GitHub enrichment
# --------------------------------------------------------------------------

def pending_enrichment(limit=20):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, runner, registration, job_name, started_at, ended_at"
            " FROM runs WHERE gh_checked=0 AND ended_at IS NOT NULL"
            " ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]


def apply_enrichment(run_id, data):
    with _lock, _conn() as c:
        c.execute(
            "UPDATE runs SET gh_checked=1, gh_run_id=?, gh_repo=?, gh_workflow=?,"
            " gh_branch=?, gh_sha=?, gh_actor=?, gh_url=?, gh_conclusion=?"
            " WHERE id=?",
            (data.get("run_id"), data.get("repo"), data.get("workflow"),
             data.get("branch"), data.get("sha"), data.get("actor"),
             data.get("url"), data.get("conclusion"), run_id))


def mark_unmatched(run_id):
    """Give up on a run so it is not retried forever."""
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET gh_checked=1 WHERE id=?", (run_id,))


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_runs(runner=None, job=None, result=None, limit=100, offset=0):
    q = "SELECT * FROM runs WHERE 1=1"
    args = []
    if runner:
        q += " AND runner=?"; args.append(runner)
    if job:
        q += " AND job_name=?"; args.append(job)
    if result:
        q += " AND result=?"; args.append(result)
    q += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_run(run_id):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        run = dict(r)
        run["samples_data"] = [dict(s) for s in c.execute(
            "SELECT t, cpu, mem FROM samples WHERE run_id=? ORDER BY t",
            (run_id,)).fetchall()]
        return run


def summary():
    """Totals plus per-job and per-runner aggregates for the overview."""
    with _lock, _conn() as c:
        tot = c.execute(
            "SELECT COUNT(*) runs,"
            " SUM(result='Succeeded') ok,"
            " SUM(result='Failed') failed,"
            " SUM(result='Canceled') canceled,"
            " SUM(COALESCE(duration_s,0)) total_s"
            " FROM runs WHERE ended_at IS NOT NULL").fetchone()
        by_job = c.execute(
            "SELECT job_name, COUNT(*) n, SUM(result='Succeeded') ok,"
            " SUM(result='Failed') failed,"
            " CAST(AVG(duration_s) AS INTEGER) avg_s,"
            " MAX(duration_s) max_s, MAX(cpu_max) cpu_peak,"
            " MAX(mem_max) mem_peak, MAX(started_at) last_run"
            " FROM runs WHERE ended_at IS NOT NULL"
            " GROUP BY job_name ORDER BY n DESC").fetchall()
        by_runner = c.execute(
            "SELECT runner, COUNT(*) n, SUM(result='Succeeded') ok,"
            " SUM(result='Failed') failed,"
            " CAST(AVG(duration_s) AS INTEGER) avg_s,"
            " SUM(COALESCE(duration_s,0)) total_s, MAX(started_at) last_run"
            " FROM runs WHERE ended_at IS NOT NULL"
            " GROUP BY runner ORDER BY runner").fetchall()
        return {
            "totals": dict(tot) if tot else {},
            "by_job": [dict(r) for r in by_job],
            "by_runner": [dict(r) for r in by_runner],
        }


def distinct(col):
    assert col in ("runner", "job_name", "result")
    with _lock, _conn() as c:
        return [r[0] for r in c.execute(
            f"SELECT DISTINCT {col} FROM runs WHERE {col} IS NOT NULL"
            f" ORDER BY {col}").fetchall()]
