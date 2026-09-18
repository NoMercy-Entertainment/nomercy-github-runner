"""The audit trail: who asked for what, and what was decided.

Append-only. There is no update and no delete here, and nothing else in the
controller writes the table. A refused request is recorded as carefully as an
accepted one: a refused destroy, or a refused connection to an agent presenting
the wrong certificate, is exactly what an operator needs to find later (design
18.4).

Parameters are redacted by field name before they are stored. What is kept is
the decision and its reason, never a credential that happened to travel with
the request.

This is the minimum the control protocol needs (T-0402, T-0403). T-1803 builds
the rest of NFR-7 on it; design 11.3 and 18.4 disagree on whether the table
also carries a fleet and an outcome, and that task settles it.
"""
import json
from datetime import datetime, timezone

from store import schema

from .redact import redact_mapping


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(path, verb, decision, actor="controller", runner_id=None,
           operation_id=None, parameters=None):
    """Append one row. Returns nothing; there is nothing to act on."""
    with schema.connect(path) as c:
        c.execute(
            "INSERT INTO audit (at, actor, verb, runner_id, operation_id,"
            " decision, parameters) VALUES (?,?,?,?,?,?,?)",
            (_now(), actor, verb, runner_id, operation_id, decision,
             json.dumps(redact_mapping(parameters))
             if parameters is not None else None))


def entries(path, verb=None, limit=100, runner_id=None):
    """Newest first, optionally for one verb or one runner."""
    clauses, params = [], []
    if verb:
        clauses.append("verb = ?")
        params.append(verb)
    if runner_id:
        clauses.append("runner_id = ?")
        params.append(runner_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with schema.connect(path) as c:
        rows = c.execute(f"SELECT * FROM audit {where} ORDER BY id DESC"
                         f" LIMIT ?", params + [limit]).fetchall()
    return [dict(r) for r in rows]
