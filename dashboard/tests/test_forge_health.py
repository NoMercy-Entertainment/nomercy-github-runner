"""Real DB/convergence races: health never waits for unit lifecycle work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import json
from types import SimpleNamespace

import pytest

import cards
import providers
from control.forge_health import ForgeHealth, MAX_AGE
from control.reconciler import Reconciler
from control.service import RunnerService
from store import schema
from tests.test_partial_failure import world, passes, FJ  # noqa: F401

GH = providers.by_key("github")
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def plant(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    service = RunnerService(path)
    service.inventory.register_worker("worker", "hyperv-linux")
    rid = service.specs.create(provider="github", platform="linux", host_id="worker",
                               actual_state="idle", registration_id="1")
    with schema.connect(path) as c:
        c.execute("UPDATE runner_specs SET unit_state='running',last_seen_at=? WHERE runner_id=?", (stamp(NOW), rid))
    return service, rid


def transport(records):
    class Transport:
        calls = 0

        def __init__(self, env):
            pass

        def records(self, provider):
            type(self).calls += 1
            return records
    return Transport


def test_blocked_lifecycle_does_not_stall_other_registration_health(world, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(FJ)
    service.scale_up(FJ)
    passes(service, reconciler)
    first, other = service.specs.list(fleet_id=FJ)
    health = ForgeHealth(service, lambda: {}, interval=0.02, forges=lambda env: forges)
    health.active = True
    flow.registration_health = health
    health.read(providers.by_key("forgejo"))
    entered, release, observed, stopped = (threading.Event() for _ in range(4))
    original_drain, original_records = agent.drain, forges.records

    def drain(host, ref):
        entered.set()
        assert release.wait(3)
        return original_drain(host, ref)

    def records(provider):
        result = original_records(provider)
        if forges.busy:
            observed.set()
        return result

    monkeypatch.setattr(agent, "drain", drain)
    monkeypatch.setattr(forges, "records", records)
    service.drain(first["runner_id"])
    thread = threading.Thread(target=health.run, args=(stopped,))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reconciler.pass_once)
        try:
            assert entered.wait(1) and not future.done()
            before_agent = list(agent.calls)
            forges.busy.add(other["registration_id"])
            thread.start()
            assert observed.wait(1)
            # Complete one native cadence read (the event fires inside its
            # transport) without waiting for lifecycle convergence.
            health.read(providers.by_key("forgejo"), due=True)
            refreshed = service.specs.get(other["runner_id"])
            assert refreshed["forge_state"] == "busy"
            assert refreshed["actual_state"] == "idle"
            assert not future.done() and agent.calls == before_agent
            service.inventory.accept_heartbeat("linux-1", {"host_id": "linux-1",
                "instances": [{"runner_id": other["runner_id"], "state": "running"}]})
            card = cards.from_spec(service.specs.get(other["runner_id"]), worker_reachable=True)
            assert card["state"] == "busy" and card["online"] is True
        finally:
            stopped.set()
            release.set()
            if thread.ident is not None:
                thread.join(1)
            future.result(timeout=3)
    assert not thread.is_alive() and not health.active


def test_cached_health_reply_keeps_capture_time_and_uses_one_routine_read(plant):
    service, rid = plant
    clock = [NOW]
    monotonic = [0.0]
    wire = transport([{"id": 1, "status": "online", "busy": False, "labels": ["Linux"]}])
    health = ForgeHealth(service, lambda: {}, forges=wire, clock=lambda: clock[0], monotonic=lambda: monotonic[0])
    health.read(GH, due=True)
    original = service.specs.get(rid)["forge_seen_at"]
    clock[0] += timedelta(seconds=10)
    monotonic[0] = 10
    health.read(GH, due=True)
    for _ in range(20):
        records, captured = health.snapshot(GH)
        assert captured == original
        records[0]["status"] = "offline"  # Consumers never mutate the shared snapshot.
    assert wire.calls == 1
    assert health.snapshot(GH)[0][0]["status"] == "online"
    assert service.specs.get(rid)["forge_seen_at"] == original
    clock[0] += timedelta(seconds=MAX_AGE)
    assert health.snapshot(GH)[0] is None


def test_failure_clears_known_health_without_refreshing_confirmation(plant):
    service, rid = plant
    clock = [NOW]
    good = transport([{"id": 1, "status": "online", "busy": False, "labels": []}])
    health = ForgeHealth(service, lambda: {}, forges=good, clock=lambda: clock[0])
    health.read(GH)
    clock[0] += timedelta(seconds=1)
    health.forges = transport(None)
    health.read(GH)
    spec = service.specs.get(rid)
    assert spec["forge_state"] == "unknown" and spec["forge_seen_at"] == stamp(NOW)
    assert health.snapshot(GH)[0] is None
    card = cards.from_spec(spec, worker_reachable=True, now=clock[0])
    assert card["reachable"] is True and card["state"] == "unknown"
    assert card["online"] is False and card["attention"] is True


@pytest.mark.parametrize("replacement", ["identity", "signed_event", "same_second_signed_event"])
def test_inflight_old_reply_cannot_overwrite_replacement_or_new_signed_evidence(plant, replacement):
    service, rid = plant
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        def __init__(self, env):
            pass

        def records(self, provider):
            entered.set()
            assert release.wait(2)
            return [{"id": 1, "status": "offline", "busy": False, "labels": ["old"]}]

    health = ForgeHealth(service, lambda: {}, forges=Blocked, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(health.read, GH)
        try:
            assert entered.wait(1)
            later = stamp(NOW if replacement == "same_second_signed_event" else NOW + timedelta(seconds=1))
            with schema.connect(service.specs.path) as c:
                c.execute("UPDATE runner_specs SET registration_id=?,forge_state='busy',"
                          "forge_seen_at=?,forge_labels='[\"new\"]',forge_labels_at=? WHERE runner_id=?",
                          ("2" if replacement == "identity" else "1", later, later, rid))
                if replacement == "same_second_signed_event":
                    c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                              (json.dumps({"forge_job_event": {"event_id": "new-signed-job"}}), rid))
        finally:
            release.set()
            future.result(timeout=2)
    spec = service.specs.get(rid)
    assert spec["forge_state"] == "busy" and spec["forge_seen_at"] == later
    assert spec["forge_labels"] == ["new"] and spec["actual_state"] == "idle"
    assert health.snapshot(GH, spec=spec) == (None, None)


def test_cached_lifecycle_observation_does_not_renew_or_overwrite_newer_evidence(plant):
    service, rid = plant
    class Executor:
        forge_words = {rid: "idle"}
        forge_labels = {}
        forge_observed_at = {rid: stamp(NOW)}

    reconciler = Reconciler(service, Executor())
    with schema.connect(service.specs.path) as c:
        c.execute("UPDATE runner_specs SET forge_state='busy',forge_seen_at=? WHERE runner_id=?",
                  (stamp(NOW + timedelta(seconds=1)), rid))
    reconciler._record_forge(service.specs.get(rid))
    assert service.specs.get(rid)["forge_state"] == "busy"
    assert service.specs.get(rid)["forge_seen_at"] == stamp(NOW + timedelta(seconds=1))


def test_stop_wakes_long_cadence_wait_and_joins_without_another_read(plant):
    service, rid = plant
    done, stopped = threading.Event(), threading.Event()
    wire = transport([{"id": 1, "status": "online", "busy": False, "labels": []}])
    health = ForgeHealth(service, lambda: {}, interval=300, forges=wire)
    original = health._record
    def recorded(*args):
        original(*args)
        done.set()
    health._record = recorded
    thread = threading.Thread(target=health.run, args=(stopped,))
    thread.start()
    try:
        assert done.wait(1)
    finally:
        stopped.set()
        thread.join(1)
    assert not thread.is_alive() and not health.active and wire.calls == 1


def test_real_flow_consumes_shared_snapshot_without_more_api_calls_or_new_timestamp(world):
    service, flow, agent, forges, reconciler = world
    service.scale_up(FJ)
    passes(service, reconciler)
    runner = service.specs.list(fleet_id=FJ)[0]
    clock = [datetime.now(timezone.utc)]
    original = forges.records
    calls = []
    forges.records = lambda p: calls.append(p.key) or original(p)
    health = ForgeHealth(service, lambda: {}, forges=lambda env: forges, clock=lambda: clock[0])
    health.active = True
    flow.registration_health = health
    health.read(providers.by_key("forgejo"))
    captured = service.specs.get(runner["runner_id"])["forge_seen_at"]
    clock[0] += timedelta(seconds=20)
    for _ in range(10):
        flow.observe(service.specs.get(runner["runner_id"]))
        reconciler._record_forge(service.specs.get(runner["runner_id"]))
    assert calls == ["forgejo"]
    assert service.specs.get(runner["runner_id"])["forge_seen_at"] == captured
    flow._records(providers.by_key("forgejo"), fresh=True)
    assert calls == ["forgejo", "forgejo"]
    assert service.specs.get(runner["runner_id"])["forge_seen_at"] != captured


def test_controller_run_starts_and_joins_health_while_lifecycle_waits(plant):
    from control.main import Controller
    from control.reconciler import Report
    service, rid = plant
    entered, release, observed, closed = (threading.Event() for _ in range(4))
    wire = transport([{"id": 1, "status": "online", "busy": False, "labels": []}])
    health = ForgeHealth(service, lambda: {}, interval=0.02, forges=wire)
    original = health._record
    def record(*args):
        original(*args)
        observed.set()
    health._record = record
    controller = object.__new__(Controller)
    controller.interval, controller._stop = 0.02, threading.Event()
    controller.forge_health = health
    controller._heartbeat = lambda state=None: None
    controller.receiver = SimpleNamespace(start=lambda: None, stop=closed.set,
                                          server_address=("127.0.0.1", 0))
    def blocked():
        entered.set()
        assert release.wait(2)
        return Report()
    controller.pass_once = blocked
    thread = threading.Thread(target=controller.run, args=(lambda message: None,))
    thread.start()
    try:
        assert entered.wait(1) and observed.wait(1)
        assert thread.is_alive() and service.specs.get(rid)["forge_state"] == "idle"
        assert service.specs.get(rid)["actual_state"] == "idle"
    finally:
        controller.stop()
        release.set()
        thread.join(2)
    assert not thread.is_alive() and closed.is_set() and not health.active
