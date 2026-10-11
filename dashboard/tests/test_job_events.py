import json

import pytest

from control.inventory import Inventory
from control.job_events import JobEvents
from store import schema
from store.specs import SpecStore


@pytest.fixture
def events(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    Inventory(path).register_worker("worker-a", "hyperv-linux")
    rid = SpecStore(path).create(provider="github", platform="linux", host_id="worker-a")
    return JobEvents(path), rid


def record(rid, state="started", **fields):
    return {"kind": "job", "event_id": "delivery-" + state, "runner_id": rid,
            "state": state, "job": "CI / test", "repo": "NoMercy-Entertainment/example",
            "run_id": "123", "run_attempt": "1", "job_id": "456", "commit": "a" * 40,
            "at": "2026-10-11T01:00:00Z", **fields}


def test_job_events_are_fenced_and_idempotent(events):
    store, rid = events
    body = record(rid)
    with pytest.raises(ValueError, match="belong"):
        store.apply("another-worker", body)
    assert not store.after(0)
    assert store.apply("worker-a", body)["duplicate"] is False
    assert store.apply("worker-a", body)["duplicate"] is True
    assert len(store.after(0)) == 1
    with pytest.raises(ValueError, match="different content"):
        store.apply("worker-a", {**body, "job": "forged"})


def test_out_of_order_start_cannot_resurrect_a_finished_job(events):
    store, rid = events
    store.apply("worker-a", record(rid, "finished", result="success"))
    store.apply("worker-a", record(rid))
    spec = SpecStore(store.path).get(rid)
    assert spec["telemetry"]["job"] is None
    assert spec["telemetry"]["job_event"]["result"] == "success"
    assert len(store.after(0)) == 2
    assert len(store.after(1)) == 1


def test_sampled_heartbeat_does_not_overwrite_event_state(events):
    store, rid = events
    store.apply("worker-a", record(rid, "finished", result="failure"))
    inventory = Inventory(store.path)
    with schema.connect(store.path) as connection:
        inventory._merge_telemetry(connection, rid, {"job": "old sampled name"}, "2026-10-11T01:01:00Z")
    assert SpecStore(store.path).get(rid)["telemetry"]["job"] is None


def test_new_job_in_same_second_is_not_hidden_by_previous_completion(events):
    store, rid = events
    store.apply("worker-a", record(rid, "finished", result="success"))
    store.apply("worker-a", record(rid, event_id="new-start", job_id="789", job="next job"))
    assert SpecStore(store.path).get(rid)["telemetry"]["job"] == "next job"


def test_worker_event_cannot_change_forge_state_or_release_a_drain(events):
    store, rid = events
    specs = SpecStore(store.path)
    spec = specs.get(rid)
    specs.update(rid, spec["spec_version"], actual_state="draining")
    with schema.connect(store.path) as connection:
        connection.execute("UPDATE runner_specs SET forge_state='busy' WHERE runner_id=?", (rid,))
    store.apply("worker-a", record(rid, "finished", result="success", source="forge"))
    assert specs.get(rid)["forge_state"] == "busy"
    assert specs.get(rid)["actual_state"] == "draining"


@pytest.mark.parametrize("fields", [
    {"event_id": "../../secret"}, {"at": "not a timestamp"}, {"at": "2026-10-11T01:00:00"},
    {"result": {}}, {"result": "success"}, {"state": "made-up"}, {"job": "a" * 201},
])
def test_bad_events_leave_no_history_or_telemetry(events, fields):
    store, rid = events
    with pytest.raises(ValueError):
        store.apply("worker-a", record(rid, **fields))
    assert not store.after(0)
    assert not SpecStore(store.path).get(rid).get("telemetry")


def test_database_watch_receives_atomic_replace_without_polling(tmp_path):
    from dbwatch import DatabaseChanges
    path = tmp_path / "control.db"
    path.write_text("before")
    watch = DatabaseChanges(path)
    try:
        replacement = tmp_path / "replacement"
        replacement.write_text("after")
        replacement.replace(path)
        assert watch.wait(2)
    finally:
        watch.close()


def test_event_stream_replays_from_cursor_and_closes_watcher(events, client, monkeypatch):
    import api_v2
    import dbwatch
    store, rid = events
    store.apply("worker-a", record(rid))
    store.apply("worker-a", record(rid, "finished", at="2026-10-11T01:02:00Z", result="success"))
    monkeypatch.setattr(api_v2, "_db_path", lambda: store.path)
    closed = []
    class Watch:
        def __init__(self, path):
            assert path == store.path
        def close(self):
            closed.append(True)
        def wait(self):
            raise AssertionError("Replay must not wait for a notification")
    monkeypatch.setattr(dbwatch, "DatabaseChanges", Watch)
    response = client.get("/api/v2/events/jobs", headers={"Last-Event-ID": "1"}, buffered=False)
    try:
        iterator = iter(response.response)
        assert next(iterator) == b"retry: 1000\n\n"
        message = next(iterator).decode()
        assert "id: 2\n" in message
        assert json.loads(message.split("data: ", 1)[1])["result"] == "success"
    finally:
        response.close()
    assert closed == [True]
