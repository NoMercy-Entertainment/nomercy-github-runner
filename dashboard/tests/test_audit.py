"""T-1802: the audit is append-only, and records every request - accepted or
refused - with actor, verb, target and reason, and how each operation ended.

Done when a refused destroy is visible after the fact. The audit follows
design 18.4's columns (fleet_id and outcome beside 11.3's), and the database
itself refuses to change or delete a row.
"""
import sqlite3

import pytest

import api_v2
from control import audit
from control.service import Refused
from store import schema
from tests.test_partial_failure import GH, passes, the_runner  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401


def rows(service, **filters):
    return audit.entries(service.operations.path, **filters)


def working(world):
    service, flow, agent, forges, reconciler = world
    service.scale_up(GH)
    passes(service, reconciler)
    spec = the_runner(service)
    forges.busy.add(spec["registration_id"])
    passes(service, reconciler, 1)
    return the_runner(service)


class TestAppendOnly:
    def test_a_row_cannot_be_changed(self, tmp_path):
        path = str(tmp_path / "c.db")
        schema.init(path)
        audit.record(path, "remove", "refused", actor="someone")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with schema.connect(path) as c:
                c.execute("UPDATE audit SET decision = 'accepted'")

    def test_nor_deleted(self, tmp_path):
        path = str(tmp_path / "c.db")
        schema.init(path)
        audit.record(path, "remove", "refused", actor="someone")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with schema.connect(path) as c:
                c.execute("DELETE FROM audit")

    def test_an_older_database_gains_the_columns_and_the_rule(self,
                                                              tmp_path):
        path = str(tmp_path / "old.db")
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY "
                      "AUTOINCREMENT, at TEXT NOT NULL, actor TEXT, verb "
                      "TEXT NOT NULL, runner_id TEXT, operation_id TEXT, "
                      "decision TEXT, parameters TEXT)")
            c.execute("INSERT INTO audit (at, verb) VALUES ('then', 'stop')")
        schema.init(path)
        with schema.connect(path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(audit)")}
            assert {"fleet_id", "outcome"} <= cols
            with pytest.raises(sqlite3.DatabaseError):
                c.execute("DELETE FROM audit")


class TestARefusedDestroyIsVisibleAfterwards:
    def test_it_is_recorded_with_actor_target_and_reason(self, world):
        service = world[0]
        spec = working(world)
        with pytest.raises(Refused):
            service.remove(spec["runner_id"], requested_by="phil@example")
        (row,) = rows(service, verb="remove", decision="refused")
        assert row["actor"] == "phil@example"
        assert row["runner_id"] == spec["runner_id"]
        assert row["fleet_id"] == GH
        assert "abort the job" in row["outcome"]
        assert row["operation_id"] is None, "nothing was opened"

    def test_and_readable_through_the_api(self, client, world, monkeypatch):
        service = world[0]
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: service.operations.path)
        spec = working(world)
        with pytest.raises(Refused):
            service.remove(spec["runner_id"], requested_by="phil@example")
        body = client.get(f"/api/v2/audit?runner_id={spec['runner_id']}"
                          f"&decision=refused").get_json()
        assert [r["verb"] for r in body["audit"]] == ["remove"]
        detail = client.get(f"/api/v2/runners/{spec['runner_id']}").get_json()
        assert detail["audit"][0]["decision"] == "refused"

    def test_a_refused_capacity_change_names_its_fleet(self, world):
        service = world[0]
        with pytest.raises(Refused):
            service.set_capacity("forgejo-windows-x64", 2,
                                 requested_by="phil@example")
        (row,) = rows(service, verb="set_capacity", decision="refused")
        assert row["fleet_id"] == "forgejo-windows-x64"
        assert row["outcome"]


class TestAcceptedAndHowItEnded:
    def test_an_accepted_operation_and_its_outcome_are_two_rows(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        spec = the_runner(service)
        op = service.stop(spec["runner_id"], requested_by="phil@example")
        passes(service, reconciler)

        mine = [r for r in rows(service) if r["operation_id"] == op]
        assert sorted(r["decision"] for r in mine) == ["accepted", "closed"]
        closed = next(r for r in mine if r["decision"] == "closed")
        assert closed["outcome"] == "succeeded"
        assert closed["actor"] == "phil@example"

    def test_a_failed_operation_says_why(self, world, monkeypatch):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        spec = the_runner(service)

        def stuck(spec):
            raise RuntimeError("the unit would not stop")
        monkeypatch.setattr(flow, "stop", stuck)
        op = service.stop(spec["runner_id"])
        passes(service, reconciler, 2)  # confirmed drain, then the failed stop
        closed = [r for r in rows(service, decision="closed")
                  if r["operation_id"] == op]
        assert closed and closed[0]["outcome"].startswith("failed")
        assert "would not stop" in closed[0]["outcome"]

    def test_an_audit_that_cannot_be_written_does_not_change_the_answer(
            self, world, monkeypatch):
        service = world[0]
        service.scale_up(GH)
        passes(service, world[4])
        spec = the_runner(service)

        def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(audit, "record", broken)
        assert service.stop(spec["runner_id"])
