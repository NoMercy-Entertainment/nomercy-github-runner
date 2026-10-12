import json
import os
import sys

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


def test_later_worker_completion_preserves_signed_result(events):
    store, rid = events
    store.apply("worker-a", record(rid, "finished", result="failure"), source="forge")
    store.apply("worker-a", record(rid, "finished", event_id="hook-finished", job_id=None,
                                  job="short-hook-job", result=None, at="2026-10-11T01:00:05Z"))
    telemetry = SpecStore(store.path).get(rid)["telemetry"]
    assert telemetry["job_event"]["result"] == "failure"
    assert telemetry["forge_job_event"]["result"] == "failure"
    assert len(store.after(0)) == 2


def test_opaque_hook_identity_cannot_erase_signed_numeric_job_result(events):
    store, rid = events
    store.apply("worker-a", record(rid, "finished", result="failure"), source="forge")
    store.apply("worker-a", record(rid, "finished", event_id="opaque-hook-finished", job_id="hook:" + "a" * 32,
                                  job="short-hook-job", result=None, at="2026-10-11T01:00:05Z"))
    telemetry = SpecStore(store.path).get(rid)["telemetry"]
    assert telemetry["job_event"]["result"] == "failure"
    assert telemetry["forge_job_event"]["result"] == "failure"
    assert len(store.after(0)) == 2


def test_future_worker_timestamp_cannot_freeze_signed_forge_state(events):
    store, rid = events
    store.apply("worker-a", record(rid, event_id="future-worker", at="9999-01-01T00:00:00Z"))
    store.apply("worker-a", record(rid), source="forge")
    assert SpecStore(store.path).get(rid)["forge_state"] == "busy"
    store.apply("worker-a", record(rid, "finished", result="success", at="2026-10-11T01:01:00Z"), source="forge")
    spec = SpecStore(store.path).get(rid)
    assert spec["forge_state"] == "idle"
    assert spec["telemetry"]["job_event"]["result"] == "success"
    assert len(store.after(0)) == 3


def test_distinct_running_worker_job_does_not_lose_name_to_prior_forge_result(events):
    store, rid = events
    store.apply("worker-a", record(rid, event_id="new-worker", run_id="124", job_id="999", job="next job",
                                  at="2026-10-11T01:02:00Z"))
    store.apply("worker-a", record(rid, "finished", result="success", at="2026-10-11T01:01:00Z"), source="forge")
    spec = SpecStore(store.path).get(rid)
    assert spec["telemetry"]["job"] == "next job"
    assert spec["forge_state"] == "idle"
    assert spec["telemetry"]["forge_job_event"]["result"] == "success"


@pytest.mark.parametrize("state", ["started", "finished"])
@pytest.mark.parametrize("fields", [{"job": None}, {"run_id": None}, {"job": None, "run_id": None}])
def test_incomplete_worker_observation_retains_signed_active_job_and_evidence(events, state, fields):
    store, rid = events
    signed = record(rid)
    store.apply("worker-a", signed, source="forge")
    body = record(rid, state, event_id="incomplete", at="2026-10-11T01:00:05Z", **fields)
    store.apply("worker-a", body)
    spec = SpecStore(store.path).get(rid)
    assert spec["forge_state"] == "busy"
    assert spec["telemetry"]["job"] == signed["job"]
    assert spec["telemetry"]["job_event"]["source"] == "forge"
    assert len(store.after(0)) == 2
    assert store.after(1)[0]["job"] == body["job"]


def test_signed_completion_repairs_preexisting_incomplete_display(events):
    store, rid = events
    stale = record(rid, event_id="old-incomplete", source="worker", job=None, run_id=None,
                   at="2026-10-11T01:02:00+00:00")
    with schema.connect(store.path) as connection:
        connection.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                           (json.dumps({"job_event": stale, "job": None,
                                        "job_event_received_at": "2026-10-11T01:02:01+00:00"}), rid))
    store.apply("worker-a", record(rid, "finished", result="failure", at="2026-10-11T01:01:00Z"), source="forge")
    telemetry = SpecStore(store.path).get(rid)["telemetry"]
    assert telemetry["job_event"]["result"] == "failure"
    assert telemetry["job_event"]["source"] == "forge"


def test_older_forge_result_cannot_overwrite_newer_signed_job(events):
    store, rid = events
    store.apply("worker-a", record(rid, event_id="new-forge", job_id="999", at="2026-10-11T01:02:00Z"), source="forge")
    store.apply("worker-a", record(rid, "finished", result="success", at="2026-10-11T01:01:00Z"), source="forge")
    spec = SpecStore(store.path).get(rid)
    assert spec["forge_state"] == "busy"
    assert spec["telemetry"]["forge_job_event"]["job_id"] == "999"


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux descriptor limit")
def test_database_watch_receives_changes_with_many_open_files(tmp_path):
    import resource
    from dbwatch import DatabaseChanges
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 1100:
        pytest.skip("Process descriptor limit is below the regression boundary")
    path = tmp_path / "control.db"
    path.write_text("before")
    handles, watch = [], None
    try:
        while not handles or handles[-1] < 1024:
            handles.append(os.open(os.devnull, os.O_RDONLY))
        watch = DatabaseChanges(path)
        replacement = tmp_path / "replacement"
        replacement.write_text("after")
        replacement.replace(path)
        assert watch.wait(2)
    finally:
        if watch:
            watch.close()
        for descriptor in handles:
            os.close(descriptor)


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
