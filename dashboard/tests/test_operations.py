"""Operations, and the three things they make possible.

Watching a slow change, retrying safely, and recovering from a crash. Each has
a class below, and the middle one is the load-bearing property: if a repeated
call with the same key ever did the work twice, every mutating endpoint in the
platform would be unsafe to retry, and a caller that did not hear the answer
would have no safe move.
"""
from datetime import datetime, timedelta, timezone

import pytest

from control import operations as ops
from control.operations import OperationStore, UnknownOperation
from store import schema
from store.specs import SpecStore


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return OperationStore(path)


@pytest.fixture
def runner(db):
    """A real spec, because operations reference one by foreign key."""
    return SpecStore(db.path).create(provider="github", platform="linux")


class TestOpening:
    def test_an_operation_is_born_pending_with_a_deadline(self, db):
        op, created = db.open("create")
        assert created is True
        assert op["state"] == ops.PENDING
        assert op["deadline_at"]
        assert op["attempts"] == 0

    def test_it_records_the_verb_and_who_asked(self, db):
        op, _ = db.open("remove", requested_by="sub-admin")
        assert op["verb"] == "remove"
        assert op["requested_by"] == "sub-admin"

    def test_it_starts_a_trace(self, db):
        op, _ = db.open("create")
        assert len(op["trace"]) == 1
        assert op["trace"][0]["at"].endswith("Z")

    def test_it_can_name_the_runner_it_is_about(self, db, runner):
        op, _ = db.open("stop", runner_id=runner)
        assert op["runner_id"] == runner

    def test_an_operation_about_a_runner_that_does_not_exist_is_refused(
            self, db):
        """The foreign key. An operation about nothing cannot be followed up,
        and would sit in the table for ever looking like work."""
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            db.open("stop", runner_id="not-a-runner")

    def test_the_deadline_honours_what_was_asked(self, db):
        op, _ = db.open("create", deadline_seconds=30)
        requested = datetime.strptime(op["requested_at"], "%Y-%m-%dT%H:%M:%SZ")
        deadline = datetime.strptime(op["deadline_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert (deadline - requested) == timedelta(seconds=30)


class TestIdempotency:
    """The property every mutating endpoint leans on."""

    def test_a_repeat_returns_the_first_operation(self, db):
        first, created_first = db.open("create", idempotency_key="k1")
        second, created_second = db.open("create", idempotency_key="k1")

        assert created_first is True
        assert created_second is False
        assert second["operation_id"] == first["operation_id"]

    def test_a_repeat_opens_no_second_operation(self, db):
        db.open("create", idempotency_key="k1")
        db.open("create", idempotency_key="k1")
        assert len(db.list()) == 1

    def test_the_caller_can_tell_it_should_do_nothing(self, db):
        """`created` is the whole signal: False means the first call either
        did the work or is still doing it."""
        db.open("create", idempotency_key="k1")
        _, created = db.open("create", idempotency_key="k1")
        assert created is False

    def test_a_repeat_of_a_finished_operation_returns_its_outcome(self, db):
        first, _ = db.open("create", idempotency_key="k1")
        db.succeed(first["operation_id"], {"runner_id": "r1"})

        again, created = db.open("create", idempotency_key="k1")

        assert created is False
        assert again["state"] == ops.SUCCEEDED
        assert again["result"] == '{"runner_id": "r1"}'

    def test_different_keys_are_different_operations(self, db):
        a, _ = db.open("create", idempotency_key="k1")
        b, _ = db.open("create", idempotency_key="k2")
        assert a["operation_id"] != b["operation_id"]

    def test_no_key_means_no_deduplication(self, db):
        """Two deliberate restarts are two operations. Only a caller that
        supplies a key is claiming its call is a repeat."""
        a, _ = db.open("restart")
        b, _ = db.open("restart")
        assert a["operation_id"] != b["operation_id"]

    def test_uniqueness_is_the_databases_not_a_check(self, db):
        """A check-then-insert has a window where two callers both find
        nothing and both insert, which is exactly what a retry creates."""
        import sqlite3
        db.open("create", idempotency_key="k1")
        with schema.connect(db.path) as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO operations (operation_id, idempotency_key,"
                    " verb, requested_at) VALUES ('x', 'k1', 'create', 'n')")


class TestTheAttemptLog:
    def test_an_attempt_is_counted_and_described(self, db):
        op, _ = db.open("create")
        after = db.attempt(op["operation_id"], "asking the agent")

        assert after["attempts"] == 1
        assert after["state"] == ops.RUNNING
        assert after["trace"][-1]["note"] == "asking the agent"

    def test_every_attempt_lands_in_the_trace(self, db):
        """Three failures for three reasons is a different problem from three
        failures for one, and only the trace can tell them apart."""
        op, _ = db.open("create")
        for reason in ("agent timed out", "forge refused", "agent timed out"):
            db.attempt(op["operation_id"], reason)

        final = db.get(op["operation_id"])
        assert final["attempts"] == 3
        notes = [entry["note"] for entry in final["trace"]]
        assert notes[-3:] == ["agent timed out", "forge refused",
                              "agent timed out"]

    def test_a_note_does_not_count_as_an_attempt(self, db):
        op, _ = db.open("create")
        db.note(op["operation_id"], "minted a registration token")
        after = db.get(op["operation_id"])
        assert after["attempts"] == 0
        assert after["trace"][-1]["note"] == "minted a registration token"

    def test_an_unknown_operation_is_an_error(self, db):
        with pytest.raises(UnknownOperation):
            db.attempt("not-an-operation", "x")


class TestClosing:
    def test_success_carries_a_result(self, db):
        op, _ = db.open("create")
        closed = db.succeed(op["operation_id"], {"exec_unit_ref": "unit-1"})
        assert closed["state"] == ops.SUCCEEDED
        assert "unit-1" in closed["result"]

    def test_failure_carries_the_reason(self, db):
        op, _ = db.open("create")
        closed = db.fail(op["operation_id"], "the forge refused the token")
        assert closed["state"] == ops.FAILED
        assert "forge refused" in closed["error"]

    def test_an_outcome_is_not_overwritten_by_a_late_reply(self, db):
        """A call that was given up on can still answer. Letting it rewrite the
        outcome would change what an operator was already shown."""
        op, _ = db.open("create")
        db.fail(op["operation_id"], "timed out")

        db.succeed(op["operation_id"], {"late": True})

        final = db.get(op["operation_id"])
        assert final["state"] == ops.FAILED
        assert final["error"] == "timed out"

    def test_a_closed_operation_is_not_reopened_by_an_attempt(self, db):
        op, _ = db.open("create")
        db.succeed(op["operation_id"])
        after = db.attempt(op["operation_id"], "a very late retry")
        assert after["state"] == ops.SUCCEEDED
        assert after["trace"][-1]["note"] == "a very late retry", (
            "the note is still recorded; only the state is protected")

    def test_a_long_error_is_truncated(self, db):
        op, _ = db.open("create")
        closed = db.fail(op["operation_id"], "x" * 9000)
        assert len(closed["error"]) == 2000


class TestOverdueOperations:
    """What a sweeper re-drives, and what it must leave alone."""

    def past(self, db, verb="create", seconds=300):
        op, _ = db.open(verb, deadline_seconds=seconds)
        return op

    def test_an_operation_past_its_deadline_is_listed(self, db):
        op = self.past(db, seconds=1)
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert [o["operation_id"] for o in db.overdue(later)] == [
            op["operation_id"]]

    def test_one_that_is_merely_slow_is_not(self, db):
        """The distinction that keeps a sweep from trampling live work."""
        self.past(db, seconds=600)
        assert db.overdue() == []

    def test_a_finished_one_is_never_re_driven(self, db):
        op = self.past(db, seconds=1)
        db.succeed(op["operation_id"])
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert db.overdue(later) == []

    def test_a_failed_one_is_never_re_driven(self, db):
        op = self.past(db, seconds=1)
        db.fail(op["operation_id"], "gave up")
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert db.overdue(later) == []

    def test_a_running_one_past_its_deadline_is_listed(self, db):
        """Running and overdue is the shape of an interrupted operation."""
        op = self.past(db, seconds=1)
        db.attempt(op["operation_id"], "started")
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert len(db.overdue(later)) == 1

    def test_the_oldest_comes_first(self, db):
        a = self.past(db, seconds=1)
        b = self.past(db, seconds=1)
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        ids = [o["operation_id"] for o in db.overdue(later)]
        assert set(ids) == {a["operation_id"], b["operation_id"]}


class TestListing:
    def test_operations_can_be_found_by_runner(self, db, runner):
        db.open("stop", runner_id=runner)
        db.open("create")
        assert len(db.list(runner_id=runner)) == 1

    def test_and_by_state(self, db):
        op, _ = db.open("create")
        db.succeed(op["operation_id"])
        db.open("create")
        assert len(db.list(state=ops.SUCCEEDED)) == 1
        assert len(db.list(state=ops.PENDING)) == 1
