"""Signed job events and lifecycle observations share an atomic commit fence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

import providers
from control.forge_health import ForgeHealth
from control.job_events import JobEvents
from control.reconciler import Reconciler, Report
from store import schema, specs as store_specs
from tests.test_forge_health import plant, NOW, stamp  # noqa: F401
from tests.test_job_events import record
from tests.test_partial_failure import world, passes, FJ  # noqa: F401
from tests.fake_runtime import UnitRuntime


def busy(service, rid):
    with schema.connect(service.specs.path) as c:
        c.execute("UPDATE runner_specs SET actual_state='busy',forge_state='busy',forge_seen_at=? WHERE runner_id=?",
                  (stamp(NOW), rid))
    return service.specs.get(rid)


def test_signed_start_between_last_check_and_move_preserves_busy_without_version_bump(plant, monkeypatch):
    service, rid = plant
    expected = busy(service, rid)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    from control import job_events
    monkeypatch.setattr(job_events, "datetime", Clock)
    class Executor:
        forge_words, forge_labels, forge_observed_at = {}, {}, {}
        def observe(self, spec):
            self.forge_words[rid] = "idle"
            self.forge_observed_at[rid] = stamp(NOW)
            return "idle"
    reconciler = Reconciler(service, Executor())
    original = service.specs.update_observed
    def intervening(spec, captured, **changes):
        # The event lands at exactly the old implementation's separate
        # validation/write window; same second and same spec version.
        JobEvents(service.specs.path).apply("worker", record(rid, event_id="new-signed-start"), source="forge")
        assert service.specs.get(rid)["spec_version"] == expected["spec_version"]
        return original(spec, captured, **changes)
    monkeypatch.setattr(service.specs, "update_observed", intervening)
    report = Report()
    reconciler._do_observe(expected, None, report)
    actual = service.specs.get(rid)
    assert actual["actual_state"] == actual["forge_state"] == "busy"
    assert actual["telemetry"]["forge_job_event"]["event_id"] == "new-signed-start"
    assert actual["spec_version"] == expected["spec_version"]
    assert report.actions == [] and report.held == [(rid, "changed before observation commit")]


@pytest.mark.parametrize("cause", ["registration_id", "registration_uuid", "provider", "actual_state", "timestamp", "marker"])
def test_observation_commit_refuses_changed_evidence_without_writing(plant, cause):
    service, rid = plant
    expected = busy(service, rid)
    with schema.connect(service.specs.path) as c:
        if cause in ("registration_id", "registration_uuid", "provider", "actual_state"):
            value = {"registration_id": "2", "registration_uuid": "new", "provider": "forgejo", "actual_state": "stopped"}[cause]
            c.execute(f"UPDATE runner_specs SET {cause}=? WHERE runner_id=?", (value, rid))
        elif cause == "timestamp":
            c.execute("UPDATE runner_specs SET forge_seen_at=? WHERE runner_id=?", (stamp(NOW + timedelta(seconds=1)), rid))
        else:
            c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                      (json.dumps({"forge_job_event": {"event_id": "new"}}), rid))
    before = service.specs.get(rid)
    assert service.specs.update_observed(expected, stamp(NOW), actual_state="idle", last_error=None) is None
    assert service.specs.get(rid) == before


def test_changed_intent_keeps_existing_optimistic_concurrency_error(plant):
    service, rid = plant
    expected = busy(service, rid)
    service.specs.update(rid, expected["spec_version"], desired_state="drained")
    with pytest.raises(store_specs.StaleSpec):
        service.specs.update_observed(expected, stamp(NOW), actual_state="idle")
    assert service.specs.get(rid)["desired_state"] == "drained"


def test_signed_writer_cannot_enter_between_guard_read_and_state_write(plant, monkeypatch):
    service, rid = plant
    expected = busy(service, rid)
    entered, release, event_entered = (threading.Event() for _ in range(3))
    original_decode = store_specs._decode
    def decode(row):
        result = original_decode(row)
        if threading.current_thread().name.startswith("atomic-guard"):
            entered.set()
            assert release.wait(2)
        return result
    monkeypatch.setattr(store_specs, "_decode", decode)
    def signed():
        event_entered.set()
        return JobEvents(service.specs.path).apply("worker", record(rid, event_id="after-atomic-commit"), source="forge")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="atomic-guard") as writer, ThreadPoolExecutor(max_workers=1) as events:
        update = writer.submit(service.specs.update_observed, expected, stamp(NOW), actual_state="idle")
        try:
            assert entered.wait(1)
            event = events.submit(signed)
            assert event_entered.wait(1) and not event.done()
        finally:
            release.set()
        assert update.result(timeout=2) == expected["spec_version"] + 1
        event.result(timeout=2)
    actual = service.specs.get(rid)
    assert actual["actual_state"] == actual["forge_state"] == "busy"
    assert actual["telemetry"]["forge_job_event"]["event_id"] == "after-atomic-commit"


def test_fresh_offline_shutdown_recovery_uses_new_capture_and_converges(world):
    service, flow, agent, forges, reconciler = world
    service.scale_up(FJ)
    passes(service, reconciler)
    spec = service.specs.list(fleet_id=FJ)[0]
    UnitRuntime.units[spec["exec_unit_ref"]]["stopped"] = True
    forges.online = False
    clock = [datetime.now(timezone.utc)]
    health = ForgeHealth(service, lambda: {}, forges=lambda env: forges, clock=lambda: clock[0])
    health.active = True
    flow.registration_health = health
    health.read(providers.by_key("forgejo"))
    first = service.specs.get(spec["runner_id"])["forge_seen_at"]
    clock[0] += timedelta(seconds=15)
    report = Report()
    reconciler._do_observe(service.specs.get(spec["runner_id"]), None, report)
    actual = service.specs.get(spec["runner_id"])
    assert actual["actual_state"] == "stopped"
    assert actual["forge_state"] == "offline" and actual["forge_seen_at"] == stamp(clock[0])
    assert actual["forge_seen_at"] != first
    assert report.actions == [("observed", spec["runner_id"], "stopped")]
