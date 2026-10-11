"""Durable, ordered job events, fenced to the runner's owning worker."""
import json
import re
from datetime import datetime, timezone

from store import schema

RESULTS = frozenset({"success", "failure", "cancelled", "timed_out", "skipped", "neutral",
                     "action_required", "stale"})


def _at(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("A UTC event timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid event timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("An event timestamp must include its timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def validate(body):
    if not isinstance(body, dict):
        raise ValueError("A job event must be an object")
    fields = ("event_id", "runner_id", "job", "repo", "commit", "run_id", "run_attempt", "job_id")
    result = {key: body.get(key) for key in fields}
    for key, value in result.items():
        if value is not None and (not isinstance(value, str) or len(value) > (200 if key == "job" else 150)):
            raise ValueError("Invalid " + key)
    if not result["event_id"] or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", result["event_id"]):
        raise ValueError("A bounded event identity is required")
    from store.storage import check
    result["runner_id"] = check(result["runner_id"])
    if body.get("state") not in ("started", "finished"):
        raise ValueError("Invalid job event state")
    result["state"] = body["state"]
    result["at"] = _at(body.get("at"))
    result["result"] = body.get("result")
    if result["result"] is not None and (not isinstance(result["result"], str) or result["result"] not in RESULTS):
        raise ValueError("Invalid job result")
    if result["state"] == "started" and result["result"] is not None:
        raise ValueError("A started job cannot have a result")
    return result


class JobEvents:
    def __init__(self, path):
        self.path = path

    def apply(self, host_id, body, source="worker"):
        event = validate(body)
        if source not in ("worker", "forge"):
            raise ValueError("Unknown event source")
        event["source"] = source
        with schema.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT host_id,telemetry,provider FROM runner_specs WHERE runner_id=?",
                                     (event["runner_id"],)).fetchone()
            if row is None or row["host_id"] != host_id:
                raise ValueError("Job event does not belong to this worker")
            payload = json.dumps(event, sort_keys=True)
            previous = connection.execute("SELECT payload FROM job_events WHERE host_id=? AND event_id=?",
                                          (host_id, event["event_id"])).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise ValueError("Event identity already has different content")
                return {"duplicate": True}
            received = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute("INSERT INTO job_events(host_id,event_id,runner_id,provider,received_at,payload) "
                                        "VALUES(?,?,?,?,?,?)", (host_id, event["event_id"], event["runner_id"],
                                        row["provider"], received, payload))
            telemetry = json.loads(row["telemetry"] or "{}")
            current = telemetry.get("job_event") or {}
            def rank(item):
                job_id = item.get("job_id") or ""
                return (item.get("at", ""), int(job_id) if job_id.isdigit() else 0,
                        item.get("state") == "finished")
            if rank(event) >= rank(current):
                telemetry["job_event"] = event
                telemetry["job"] = event["job"] if event["state"] == "started" else None
                connection.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                                   (json.dumps(telemetry), event["runner_id"]))
                if source == "forge":
                    # Signed forge events are authoritative observations;
                    # worker log events never grant themselves this right.
                    state = "busy" if event["state"] == "started" else "idle"
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    connection.execute("UPDATE runner_specs SET forge_state=?,forge_seen_at=?,"
                                       "actual_state=CASE WHEN actual_state IN ('idle','busy') THEN ? ELSE actual_state END "
                                       "WHERE runner_id=?", (state, stamp, state, event["runner_id"]))
            connection.execute("INSERT INTO audit(at,actor,verb,runner_id,decision,parameters) VALUES(?,?,?,?,?,?)",
                               (received, ("forge:" + row["provider"] if source == "forge" else "worker:" + host_id),
                                "job", event["runner_id"], event["state"], payload))
            return {"sequence": cursor.lastrowid, "duplicate": False}

    def after(self, sequence, limit=200):
        with schema.connect(self.path) as connection:
            rows = connection.execute("SELECT sequence,provider,received_at,payload FROM job_events "
                                      "WHERE sequence>? ORDER BY sequence LIMIT ?", (sequence, limit)).fetchall()
            return [{"sequence": row["sequence"], "provider": row["provider"],
                     "received_at": row["received_at"], **json.loads(row["payload"])} for row in rows]
