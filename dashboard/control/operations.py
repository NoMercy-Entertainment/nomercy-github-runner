"""Operations: the record that someone asked, separate from the doing.

Every mutating call in this platform writes one of these and returns its id.
Nothing about that is bookkeeping for its own sake - it is what makes three
otherwise impossible things possible.

**A slow change can be watched.** Creating a runner takes a minute or more:
mint a token, make an execution unit, register, wait for the forge to agree.
A request that held the connection open for all of that would time out in a
proxy long before it finished, and the caller would never learn whether it
worked. An id can be asked about instead.

**A retry is safe.** A caller that did not hear the answer repeats the call
with the same `Idempotency-Key`. The key is unique in the database, so the
repeat finds the first operation and returns it rather than doing the work
again. That is enforced by a constraint, not by a check-then-insert, because a
check-then-insert has a window in which two callers both pass.

**A crash is recoverable.** Every operation carries a deadline. One still
unfinished past it is either stuck or was interrupted, and either way the
sweeper can re-drive it. Without a deadline an operation that died is
indistinguishable from one that is simply slow, and the difference decides
whether touching it would trample on work in progress.

`attempts` and `trace` exist for the same reason: an operation that failed
three times for three different reasons is a different problem from one that
failed the same way three times, and only the trace can tell them apart.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from store import schema

from .redact import redact_mapping

PENDING, RUNNING = "pending", "running"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"

#: An operation in one of these is finished and is never re-driven.
CLOSED = frozenset({SUCCEEDED, FAILED, CANCELLED})
OPEN = frozenset({PENDING, RUNNING})

#: Spec 17.2: slow agent verbs run to the operation deadline, 900s by
#: default. It was 300 until making a unit was measured: `docker create`
#: from a 17 GB image took 58 seconds on a quiet engine and three minutes on
#: a busy one, and every rebuild died at the deadline (2026-09-20).
DEFAULT_DEADLINE_SECONDS = 900


class UnknownOperation(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def _decode(row):
    if row is None:
        return None
    op = dict(row)
    op["trace"] = json.loads(op["trace"]) if op.get("trace") else []
    return op


class OperationStore:
    def __init__(self, path=None):
        self.path = path or schema.DB_PATH

    def _conn(self):
        return schema.connect(self.path)

    # ---- opening -----------------------------------------------------------

    def open(self, verb, runner_id=None, fleet_id=None, requested_by=None,
             idempotency_key=None, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
             note=None):
        """Record an intention. Returns `(operation, created)`.

        `created` is False when the key had been seen before, and a caller that
        gets False must do nothing: the first call either did the work or is
        still doing it. Returning the original rather than raising is what
        makes a retry ordinary instead of an error to handle.

        The uniqueness is the database's, deliberately. Checking first and then
        inserting leaves a window where two callers both find nothing and both
        insert, which is exactly the case a retry creates.
        """
        if idempotency_key:
            existing = self.by_key(idempotency_key)
            if existing:
                return existing, False

        operation_id = str(uuid.uuid4())
        opened = _now()
        trace = [{"at": _iso(opened), "note": note or f"{verb} requested"}]

        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO operations (operation_id, idempotency_key,"
                    " runner_id, fleet_id, verb, requested_by, requested_at,"
                    " state, attempts, deadline_at, trace)"
                    " VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                    (operation_id, idempotency_key, runner_id, fleet_id, verb,
                     requested_by, _iso(opened), PENDING,
                     _iso(opened + timedelta(seconds=deadline_seconds)),
                     json.dumps(trace)))
        except Exception:
            # The unique key lost a race with another caller holding the same
            # one. That caller's operation is the answer; this one never
            # existed. Any other failure re-raises.
            if idempotency_key:
                existing = self.by_key(idempotency_key)
                if existing:
                    return existing, False
            raise

        return self.get(operation_id), True

    # ---- reading -----------------------------------------------------------

    def get(self, operation_id):
        with self._conn() as c:
            return _decode(c.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,)).fetchone())

    def by_key(self, idempotency_key):
        with self._conn() as c:
            return _decode(c.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone())

    def list(self, runner_id=None, fleet_id=None, state=None, limit=100):
        where, params = [], []
        for column, value in (("runner_id", runner_id),
                              ("fleet_id", fleet_id), ("state", state)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM operations {clause}"
                f" ORDER BY requested_at DESC, operation_id LIMIT ?",
                params + [limit]).fetchall()
        return [_decode(r) for r in rows]

    def overdue(self, now=None):
        """Open operations past their deadline, oldest first.

        What the sweeper re-drives. An operation that is merely slow is not in
        here, which is the distinction that keeps a sweep from trampling work
        that is still in progress.
        """
        moment = _iso(now or _now())
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM operations WHERE state IN (?, ?)"
                " AND deadline_at IS NOT NULL AND deadline_at < ?"
                " ORDER BY requested_at", (PENDING, RUNNING, moment)
            ).fetchall()
        return [_decode(r) for r in rows]

    # ---- driving -----------------------------------------------------------

    def attempt(self, operation_id, note):
        """Record that work is being tried, and say what.

        Increments `attempts` and appends to the trace in one statement, so an
        attempt cannot be counted without being described. A count with no
        description cannot distinguish three failures with three causes from
        three failures with one.
        """
        return self._append(operation_id, note, state=RUNNING, bump=True)

    def note(self, operation_id, text):
        """Add to the trace without counting an attempt."""
        return self._append(operation_id, text)

    def progress(self, operation_id):
        """Where a multi-step operation has got to, or {} if nowhere yet.

        Stored in `result` while the operation is open and replaced by the
        outcome when it closes. A restart is idle before it starts and idle
        again when it finishes, so the runner's state alone cannot say which;
        this can.
        """
        operation = self.get(operation_id)
        if not operation or operation["state"] in CLOSED:
            return {}
        try:
            return json.loads(operation["result"]) if operation["result"] \
                else {}
        except (ValueError, TypeError):
            return {}

    def merge_progress(self, operation_id, update):
        """Change an open operation's progress without losing anyone else's.

        `update` receives the current progress and returns the new one. The
        read and the write happen under one write lock (BEGIN IMMEDIATE), so a
        reconciler recording a restart's phase and an agent's event arriving
        at the same moment cannot each overwrite what the other just wrote.
        Ignored once the operation is closed.
        """
        c = schema.connect(self.path)
        try:
            c.isolation_level = None
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT state, result FROM operations WHERE operation_id = ?",
                (operation_id,)).fetchone()
            if row is None or row["state"] in CLOSED:
                c.execute("ROLLBACK")
                return {}
            try:
                current = json.loads(row["result"]) if row["result"] else {}
            except (ValueError, TypeError):
                current = {}
            updated = update(dict(current))
            c.execute("UPDATE operations SET result = ? WHERE operation_id = ?",
                      (json.dumps(updated), operation_id))
            c.execute("COMMIT")
            return updated
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:           # noqa: BLE001
                pass
            raise
        finally:
            c.close()

    def apply_event(self, host_id, event):
        """Record progress an agent reported for work it was asked to do.

        `host_id` is the worker the event arrived from, proved by its
        certificate. The event must name an open-or-closed operation this
        controller knows, about a runner placed on that worker - one worker
        cannot report progress on another's work. Each event adds a line to
        the operation's trace and records the agent call's state under its
        handle, which is what a caller waiting on that call looks for.

        The operation itself is not closed here. An operation is usually more
        than one agent call - a provision is a create and then a register -
        and only the controller knows when all of them are done.
        """
        if not isinstance(event, dict):
            raise ValueError("an event must be an object")
        state = event.get("state")
        if state not in ("running", "succeeded", "failed"):
            raise ValueError("an event must say running, succeeded or failed")
        handle = event.get("handle")
        if not isinstance(handle, str) or not handle:
            raise ValueError("an event must carry its handle")
        operation = self.get(event.get("operation_id") or "")
        if operation is None:
            raise ValueError("no such operation")
        with schema.connect(self.path) as c:
            row = c.execute(
                "SELECT host_id FROM runner_specs WHERE runner_id = ?",
                (operation["runner_id"] or "",)).fetchone()
        if row is None or row["host_id"] != host_id:
            raise ValueError("that operation's runner is not on this worker")

        verb = str(event.get("verb") or "")[:64]
        self.note(operation["operation_id"],
                  f"{verb} {state} on {host_id} ({handle[:8]})")

        record = {"verb": verb, "state": state}
        if state == "succeeded":
            record["result"] = redact_mapping(event.get("result") or {})
        if state == "failed":
            record["error"] = str(event.get("error") or "")[:500]

        def store(progress):
            calls = dict(progress.get("calls") or {})
            calls[handle] = record
            progress["calls"] = calls
            return progress

        self.merge_progress(operation["operation_id"], store)
        return {"recorded": True}

    def call_state(self, operation_id, handle):
        """What the last event said about one agent call, or None."""
        return (self.progress(operation_id).get("calls") or {}).get(handle)

    def set_progress(self, operation_id, progress):
        """Record how far a multi-step operation has got. Ignored once it is
        closed, for the same reason a late reply cannot rewrite an outcome."""
        with self._conn() as c:
            c.execute(
                "UPDATE operations SET result = ? WHERE operation_id = ?"
                " AND state NOT IN (?, ?, ?)",
                (json.dumps(progress), operation_id,
                 SUCCEEDED, FAILED, CANCELLED))

    def succeed(self, operation_id, result=None):
        return self._close(operation_id, SUCCEEDED, result=result)

    def fail(self, operation_id, error):
        """Close as failed. The message is truncated and must already be
        redacted - a token reaching here would be stored for ever."""
        return self._close(operation_id, FAILED,
                           error=(str(error) or "")[:2000])

    def cancel(self, operation_id, reason="cancelled"):
        return self._close(operation_id, CANCELLED, error=reason)

    def _append(self, operation_id, note, state=None, bump=False):
        with self._conn() as c:
            row = c.execute(
                "SELECT trace, state FROM operations WHERE operation_id = ?",
                (operation_id,)).fetchone()
            if row is None:
                raise UnknownOperation(operation_id)
            trace = json.loads(row["trace"]) if row["trace"] else []
            trace.append({"at": _iso(_now()), "note": str(note)[:500]})

            sets = ["trace = ?"]
            params = [json.dumps(trace)]
            if bump:
                sets.append("attempts = attempts + 1")
            # A closed operation keeps its outcome. A late note is still worth
            # recording, but it must not reopen something already decided.
            if state and row["state"] not in CLOSED:
                sets.append("state = ?")
                params.append(state)
            c.execute(f"UPDATE operations SET {', '.join(sets)}"
                      f" WHERE operation_id = ?", params + [operation_id])
        return self.get(operation_id)

    def _close(self, operation_id, state, result=None, error=None):
        with self._conn() as c:
            row = c.execute(
                "SELECT state, trace FROM operations WHERE operation_id = ?",
                (operation_id,)).fetchone()
            if row is None:
                raise UnknownOperation(operation_id)
            if row["state"] in CLOSED:
                # Already decided. Re-closing would let a late reply from a
                # call that was given up on overwrite the outcome the operator
                # was shown.
                return self.get(operation_id)

            trace = json.loads(row["trace"]) if row["trace"] else []
            trace.append({"at": _iso(_now()), "note": state})
            c.execute(
                "UPDATE operations SET state = ?, result = ?, error = ?,"
                " trace = ? WHERE operation_id = ?",
                (state,
                 json.dumps(result) if result is not None else None,
                 error, json.dumps(trace), operation_id))
        closed = self.get(operation_id)
        self._audit_close(closed, state, error)
        return closed

    def _audit_close(self, op, state, error):
        """How an operation ended, as a row of its own (design 18.4): the
        audit is append-only, so an outcome is never an update to the row
        that accepted it. Written after the close is committed, and never
        allowed to undo it."""
        try:
            from . import audit
            audit.record(self.path, op["verb"], "closed",
                         actor=op.get("requested_by") or "controller",
                         runner_id=op.get("runner_id"),
                         fleet_id=op.get("fleet_id"),
                         operation_id=op["operation_id"],
                         outcome=state + (f": {error}" if error else ""))
        except Exception as e:      # noqa: BLE001
            print(f"[audit] could not record the close of "
                  f"{op.get('operation_id')}: {type(e).__name__}")
