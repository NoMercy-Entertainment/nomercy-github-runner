"""Giving every historical run a stable referent, without losing any of it.

History keys runs on the container name, which was never an identity. The
platform now keys on `runner_id`, so every old row needs one - and the risk in
that sentence is the whole reason this file is careful: a migration over live
history that loses a row loses it silently, and nobody notices until someone
goes looking for a run that used to be there.

So the assertions are mostly about what must NOT change. The row count is
identical before and after. Nothing is deleted. Every column that was there is
still there with the value it had. Running it twice does nothing the second
time. And the specs it creates are born deleted, or the controller would read
them as runners it ought to bring into existence.
"""
import pytest

import history
from store import schema
from store.specs import SpecStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A history database and a control plane, both throwaway."""
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "history.db"))
    history.init()
    control = str(tmp_path / "control.db")
    schema.init(control)
    return SpecStore(control)


def a_run(runner, job, at, provider="github"):
    history.open_run(runner, "reg-1", job, at, provider=provider)


def rows():
    with history._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM runs ORDER BY id")]


class TestTheColumnArrives:
    def test_runs_has_a_nullable_runner_id(self, db):
        with history._conn() as c:
            columns = {r[1]: r for r in c.execute("PRAGMA table_info(runs)")}
        assert "runner_id" in columns
        assert columns["runner_id"][3] == 0, "must be nullable"

    def test_it_is_indexed(self, db):
        with history._conn() as c:
            names = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_runs_runner_id" in names

    def test_a_fresh_database_migrates_cleanly(self, db):
        """Nothing to backfill is a successful backfill, not an error."""
        assert history.backfill_runner_ids(db) == {"specs_created": 0,
                                                   "runs_resolved": 0}

    def test_init_is_still_safe_to_repeat(self, db):
        a_run("github-runner-1", "build", "2026-01-01T00:00:00Z")
        history.init()
        assert len(rows()) == 1


class TestNothingIsLost:
    def test_the_row_count_is_identical(self, db):
        """ACC-15, as a number."""
        for i in range(5):
            a_run(f"github-runner-{i}", "build", f"2026-01-0{i + 1}T00:00:00Z")
        before = len(rows())

        history.backfill_runner_ids(db)

        assert len(rows()) == before

    def test_every_other_column_is_untouched(self, db):
        a_run("github-runner-1", "build-base", "2026-01-01T00:00:00Z")
        history.close_run("github-runner-1", "build-base",
                          "2026-01-01T00:30:00Z", "Succeeded")
        before = rows()[0]

        history.backfill_runner_ids(db)
        after = rows()[0]

        for key, value in before.items():
            if key == "runner_id":
                continue
            assert after[key] == value, key

    def test_the_old_name_is_still_readable(self, db):
        """`runner` is not dropped. It is how an operator recognises the row."""
        a_run("github-runner-7", "build", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        assert rows()[0]["runner"] == "github-runner-7"


class TestEveryRunResolves:
    def test_each_row_gets_an_id(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        a_run("github-runner-2", "b", "2026-01-01T00:00:00Z")

        history.backfill_runner_ids(db)

        assert all(r["runner_id"] for r in rows())
        assert history.unresolved_runs() == 0

    def test_the_id_resolves_to_a_spec_carrying_the_old_name(self, db):
        a_run("github-runner-9", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)

        spec = db.get(rows()[0]["runner_id"])
        assert spec is not None
        assert spec["display_name"] == "github-runner-9"
        assert spec["exec_unit_ref"] == "github-runner-9"

    def test_runs_of_one_runner_share_one_spec(self, db):
        for i in range(3):
            a_run("github-runner-1", f"job-{i}", f"2026-01-0{i + 1}T00:00:00Z")
        history.backfill_runner_ids(db)
        assert len({r["runner_id"] for r in rows()}) == 1

    def test_different_runners_get_different_specs(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        a_run("github-runner-2", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        assert len({r["runner_id"] for r in rows()}) == 2

    def test_one_name_on_two_forges_is_two_runners(self, db):
        """A spec carries a provider and a name alone cannot say which. Keyed
        on the pair, so two forges using one name are not merged into one."""
        a_run("runner-1", "a", "2026-01-01T00:00:00Z", provider="github")
        a_run("runner-1", "b", "2026-01-01T00:00:00Z", provider="forgejo")

        history.backfill_runner_ids(db)

        ids = {r["provider"]: r["runner_id"] for r in rows()}
        assert ids["github"] != ids["forgejo"]
        assert db.get(ids["forgejo"])["provider"] == "forgejo"


class TestTheSpecsAreBornDeleted:
    def test_a_backfilled_spec_is_soft_deleted(self, db):
        """They describe runners that are gone. A live-looking spec would be
        reconciled into existence, which would mean a history migration
        created runners."""
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        spec = db.get(rows()[0]["runner_id"])
        assert spec["deleted_at"]
        assert spec["desired_state"] == "absent"
        assert spec["actual_state"] == "absent"

    def test_it_is_out_of_the_live_listing(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        assert db.list() == []

    def test_but_it_is_still_readable(self, db):
        """A referent that cannot be read is not a referent."""
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        assert db.get(rows()[0]["runner_id"]) is not None


class TestIdempotence:
    def test_a_second_run_changes_nothing(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        a_run("github-runner-2", "b", "2026-01-01T00:00:00Z")
        first = history.backfill_runner_ids(db)
        before = rows()

        second = history.backfill_runner_ids(db)

        assert first["specs_created"] == 2
        assert second == {"specs_created": 0, "runs_resolved": 0}
        assert rows() == before

    def test_it_creates_no_duplicate_specs(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        history.backfill_runner_ids(db)
        assert len(db.list(include_deleted=True)) == 1

    def test_a_run_arriving_after_the_backfill_reuses_the_spec(self, db):
        """The ordinary case during a migration: history keeps being written
        while this runs, and the late row must not mint a second identity."""
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        history.backfill_runner_ids(db)
        first_id = rows()[0]["runner_id"]

        a_run("github-runner-1", "b", "2026-01-02T00:00:00Z")
        result = history.backfill_runner_ids(db)

        assert result["specs_created"] == 0
        assert {r["runner_id"] for r in rows()} == {first_id}

    def test_a_live_spec_sharing_a_name_is_not_adopted(self, db):
        """Display names are not unique. A backfill that reused a live runner's
        spec would attach old history to a runner that never ran it."""
        live = db.create(provider="github", platform="linux",
                         display_name="github-runner-1",
                         exec_unit_ref="github-runner-1")
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")

        history.backfill_runner_ids(db)

        assert rows()[0]["runner_id"] != live


class TestReporting:
    def test_it_says_what_it_did(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        a_run("github-runner-1", "b", "2026-01-02T00:00:00Z")
        a_run("github-runner-2", "c", "2026-01-01T00:00:00Z")

        result = history.backfill_runner_ids(db)

        assert result == {"specs_created": 2, "runs_resolved": 3}

    def test_unresolved_runs_counts_what_is_left(self, db):
        a_run("github-runner-1", "a", "2026-01-01T00:00:00Z")
        assert history.unresolved_runs() == 1
        history.backfill_runner_ids(db)
        assert history.unresolved_runs() == 0


class TestMigratingADatabaseThatAlreadyExists:
    """The production path, exercised rather than assumed.

    The live history.db predates this column. CREATE TABLE IF NOT EXISTS does
    nothing to a table that already exists, so the column can only arrive by
    ALTER - and an ALTER over real history is the one step in this task that
    could lose something.
    """

    def old_shape(self, path):
        """A history database as it was before runner_id, with rows in it."""
        import sqlite3
        c = sqlite3.connect(path)
        c.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " runner TEXT NOT NULL, registration TEXT, job_name TEXT NOT NULL,"
            " started_at TEXT NOT NULL, ended_at TEXT, duration_s INTEGER,"
            " result TEXT, UNIQUE(runner, job_name, started_at))")
        for i in range(3):
            c.execute(
                "INSERT INTO runs (runner, job_name, started_at, result)"
                " VALUES (?, ?, ?, 'Succeeded')",
                (f"github-runner-{i}", "build-base",
                 f"2026-01-0{i + 1}T00:00:00Z"))
        c.commit()
        c.close()

    def test_the_column_is_added_without_losing_a_row(self, tmp_path,
                                                      monkeypatch):
        path = str(tmp_path / "old-history.db")
        self.old_shape(path)
        monkeypatch.setattr(history, "DB_PATH", path)

        history.init()

        with history._conn() as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            count = c.execute("SELECT count(*) FROM runs").fetchone()[0]
        assert "runner_id" in columns
        assert count == 3

    def test_the_older_columns_are_added_too(self, tmp_path, monkeypatch):
        """provider and forge_task_id predate this work and must still arrive,
        because the backfill reads provider."""
        path = str(tmp_path / "old-history.db")
        self.old_shape(path)
        monkeypatch.setattr(history, "DB_PATH", path)

        history.init()

        with history._conn() as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        assert {"provider", "forge_task_id"} <= columns

    def test_the_existing_history_backfills(self, tmp_path, monkeypatch):
        path = str(tmp_path / "old-history.db")
        self.old_shape(path)
        monkeypatch.setattr(history, "DB_PATH", path)
        history.init()
        control = str(tmp_path / "control.db")
        schema.init(control)

        result = history.backfill_runner_ids(SpecStore(control))

        assert result == {"specs_created": 3, "runs_resolved": 3}
        assert history.unresolved_runs() == 0
