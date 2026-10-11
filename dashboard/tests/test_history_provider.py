"""Two forges in one history table, and an existing database that predates it.

CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
schema change alone would leave the live history without the new columns and
every insert would fail. init() has to migrate.
"""
import sqlite3

import history


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()


def _cols(tmp_path):
    c = sqlite3.connect(str(tmp_path / "h.db"))
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    finally:
        c.close()


def test_a_new_database_has_both_columns(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert {"provider", "forge_task_id"} <= _cols(tmp_path)


def test_an_existing_database_is_migrated(tmp_path, monkeypatch):
    """The live history table predates both columns."""
    db = str(tmp_path / "h.db")
    monkeypatch.setattr(history, "DB_PATH", db)
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " runner TEXT NOT NULL, registration TEXT, job_name TEXT NOT NULL,"
        " started_at TEXT NOT NULL, ended_at TEXT, duration_s INTEGER,"
        " result TEXT, UNIQUE(runner, job_name, started_at));")
    c.execute("INSERT INTO runs (runner, job_name, started_at)"
              " VALUES ('github-runner-1','build','2026-08-01T00:00:00Z')")
    c.commit()
    c.close()

    history.init()

    assert {"provider", "forge_task_id"} <= _cols(tmp_path)
    rows = history.list_runs()
    assert rows[0]["provider"] == "github", \
        "existing history must be labelled, not left NULL"


def test_open_run_keeps_its_four_argument_form(tmp_path, monkeypatch):
    """test_orphaned_runs.py calls it this way and must not be edited."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("github-runner-1", "nomercy-x", "build",
                     "2026-08-25T10:00:00Z")
    assert history.list_runs()[0]["provider"] == "github"


def test_a_forgejo_run_records_its_task(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "nomercy-forgejo-1", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    row = history.list_runs()[0]
    assert (row["provider"], row["forge_task_id"]) == ("forgejo", 830)


def test_apply_close_closes_by_id_and_computes_duration(tmp_path, monkeypatch):
    """Forgejo has no completion log line, so the end arrives by run id."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    run_id = history.list_runs()[0]["id"]
    history.apply_close(run_id, "2026-08-25T14:44:02Z", "success")
    row = history.list_runs()[0]
    assert row["ended_at"] == "2026-08-25T14:44:02Z"
    assert row["result"] == "success"
    assert row["duration_s"] == 307


def test_pending_enrichment_carries_what_the_forgejo_lookup_needs(
        tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    pending = history.pending_enrichment()
    assert pending[0]["provider"] == "forgejo"
    assert pending[0]["forge_task_id"] == 830


def test_an_open_forgejo_run_is_still_pending(tmp_path, monkeypatch):
    """The run above was never closed, and must be listed anyway: the API
    sweep is the only thing that can close it. An ended_at filter here would
    mean no Forgejo run is ever enriched or ever ends."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:38:55Z",
                     provider="forgejo", forge_task_id=830)
    assert len(history.pending_enrichment()) == 1


def test_an_open_github_run_is_not_yet_pending(tmp_path, monkeypatch):
    """Unchanged for GitHub: find_job() matches inside the start-to-end
    window, so a run with no end cannot be looked up yet."""
    _fresh(tmp_path, monkeypatch)
    history.open_run("github-runner-1", "nomercy-x", "build",
                     "2026-08-25T10:00:00Z")
    assert history.pending_enrichment() == []
