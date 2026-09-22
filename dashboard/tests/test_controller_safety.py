"""Behavioral regressions for destructive actions and placement boundaries."""
import threading
import time

import pytest

from control import placement
from control.reconciler import Reconciler
from control.service import Refused
from store import schema
from tests.test_reconciler import GH, WORKER, converge, live, world


def serving(world, count=1):
    service, executor, reconciler = world
    service.scale_up(GH, by=count)
    converge(service, reconciler)
    for spec in live(service):
        executor.world[spec["runner_id"]] = "idle"
    executor.calls.clear()
    return live(service)


@pytest.mark.parametrize("verb", ["stop", "restart", "clear_cache"])
def test_stale_idle_cannot_end_a_job(world, verb):
    service, executor, reconciler = world
    spec = serving(world)[0]
    operation = service.act(spec["runner_id"], verb)
    executor.world[spec["runner_id"]] = "busy"
    reconciler.pass_once()
    assert not {"stop", "clear_cache"}.intersection(c[0] for c in executor.calls)
    if verb == "clear_cache":
        assert service.operations.get(operation)["state"] == "failed"
    else:
        assert service.specs.get(spec["runner_id"])["actual_state"] == "draining"


def test_cache_clear_drains_even_when_currently_idle(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    service.clear_cache(spec["runner_id"])
    reconciler.pass_once()
    assert [c[0] for c in executor.mutations()] == ["drain"]
    reconciler.pass_once()
    assert [c[0] for c in executor.mutations()] == ["drain", "clear_cache"]
    reconciler.pass_once()
    assert executor.mutations()[-1][0] == "cancel_drain"


def test_cache_clear_does_not_trust_unavailable_observation(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    operation = service.clear_cache(spec["runner_id"])
    executor.world[spec["runner_id"]] = None
    reconciler.pass_once()
    assert not executor.mutations()
    assert service.operations.get(operation)["state"] == "failed"


def test_unbuildable_fleet_does_not_block_other_runners(world, monkeypatch):
    service, executor, reconciler = world
    spec = serving(world)[0]
    real = reconciler._converge_capacity
    def converge_one(fleet, report):
        if fleet["fleet_id"] != GH:
            raise Refused("no template")
        return real(fleet, report)
    monkeypatch.setattr(reconciler, "_converge_capacity", converge_one)
    service.stop(spec["runner_id"])
    report = reconciler.pass_once()
    assert report.errors
    assert any(c[0] == "drain" for c in executor.calls)


def test_fleet_recreates_one_at_a_time(world):
    service, executor, reconciler = world
    specs = serving(world, 2)
    for spec in specs:
        service.recreate(spec["runner_id"])
    for _ in range(16):
        reconciler.pass_once()
        assert sum(s["actual_state"] not in {"idle", "busy"}
                   for s in live(service)) <= 1
    assert sum(c[0] == "remove" for c in executor.calls) == 2


def test_fleet_cache_clear_takes_one_runner_at_a_time(world):
    """"Clear all cache" asks every idle runner at once; each clear drains
    its runner first, so without turns the whole fleet was out of service
    together (2026-09-22 review)."""
    service, executor, reconciler = world
    specs = serving(world, 3)
    for spec in specs:
        service.clear_cache(spec["runner_id"])
    for _ in range(20):
        reconciler.pass_once()
        assert sum(s["actual_state"] not in {"idle", "busy"}
                   for s in live(service)) <= 1
    assert sum(c[0] == "clear_cache" for c in executor.calls) == 3
    assert all(s["actual_state"] == "idle" for s in live(service))


def test_failed_replacement_halts_remaining_recreates(world):
    service, executor, reconciler = world
    for spec in serving(world, 2):
        service.recreate(spec["runner_id"])
    executor.fail_on.add("provision")
    for _ in range(12):
        reconciler.pass_once()
    assert sum(c[0] == "remove" for c in executor.calls) == 1
    assert sum(s["actual_state"] == "idle" for s in live(service)) == 1
    assert all(o["state"] == "failed" for s in live(service)
               for o in service.operations.list(runner_id=s["runner_id"])
               if o["verb"] == "recreate")


def test_failed_recreate_waits_for_explicit_retry_and_preserves_data(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    operation = service.recreate(spec["runner_id"])
    executor.fail_on.add("remove")
    for _ in range(8):
        reconciler.pass_once()
    removals = [c for c in executor.calls if c[0] == "remove"]
    assert len(removals) == 1 and removals[0][2]["keep_data"] is True
    assert service.operations.get(operation)["state"] == "failed"
    executor.fail_on.clear()
    retry = service.recreate(spec["runner_id"])
    for _ in range(8):
        reconciler.pass_once()
    assert service.operations.get(retry)["state"] == "succeeded"
    assert all(c[2]["keep_data"] for c in executor.calls if c[0] == "remove")


def test_recreate_refused_before_destroying_template_less_adoption(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    with schema.connect(service.specs.path) as db:
        db.execute("UPDATE fleets SET template = NULL WHERE fleet_id = ?", (GH,))
    service.inventory.register_worker(WORKER, "hyperv-linux", capabilities={
        "kind": "linux-container", "builds_from": "template", "templates": []})
    service.inventory.heartbeat(WORKER)
    with pytest.raises(Refused, match="replacement"):
        service.recreate(spec["runner_id"])
    assert not executor.mutations()


def test_recreate_rechecks_template_after_drain_before_deleting(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    operation = service.recreate(spec["runner_id"])
    reconciler.pass_once()
    service.inventory.register_worker(WORKER, "hyperv-linux", capabilities={
        "kind": "linux-container", "builds_from": "template", "templates": []})
    service.inventory.heartbeat(WORKER)
    reconciler.pass_once()
    assert not {"deregister", "remove"}.intersection(c[0] for c in executor.calls)
    assert service.operations.get(operation)["state"] == "failed"


def test_memory_default_is_recorded_before_placement(world):
    service, executor, reconciler = world
    service.env["RUNNER_UNIT_MEMORY_GITHUB_LINUX"] = "32g"
    operation = service.plan(GH, 1)
    spec = service.specs.get(service.planned_ids(operation)[0])
    assert spec["memory_limit"] == 32 * 2**30
    host, why = placement.choose(spec, [{"host_id": "small", "capabilities": {
        "memory_bytes": 12 * 2**30}}], [])
    assert host is None and "not enough memory" in why


def test_unknown_memory_does_not_count_as_zero_on_a_budgeted_worker():
    worker = {"host_id": "worker", "capabilities": {"memory_bytes": 12 * 2**30}}
    spec = {"platform": "linux", "memory_limit": None}
    host, why = placement.choose(spec, [worker], [])
    assert host is None and "memory requirements are unknown" in why


def test_settings_cannot_shrink_existing_units_memory_reservation(world):
    service, executor, reconciler = world
    spec = serving(world)[0]
    spec["memory_limit"] = 4 * 2**30
    spec["telemetry"] = {"mem_limit_bytes": 32 * 2**30}
    assert service.reserved_spec(spec)["memory_limit"] == 32 * 2**30


def test_scheduler_selects_worker_with_required_template():
    spec = {"platform": "windows", "architecture": "x64", "runtime_template": "gh-win"}
    workers = [{"host_id": host, "capabilities": {"kind": "windows-process",
        "builds_from": "template", "templates": templates}}
        for host, templates in [("first", []), ("second", ["gh-win"])]]
    assert placement.choose(spec, workers, [])[0] == "second"


def test_slow_pass_renews_lease_and_expired_lease_still_cannot_overlap(world, monkeypatch):
    service, executor, reconciler = world
    serving(world)
    entered, finish = threading.Event(), threading.Event()
    def slow_observe(spec):
        entered.set()
        assert finish.wait(10)
        return "idle"
    executor.observe = slow_observe
    monkeypatch.setattr("control.reconciler.LEASE_RENEW_SECONDS", 0.02)
    thread = threading.Thread(target=reconciler.pass_once)
    thread.start()
    try:
        assert entered.wait(10)
        with schema.connect(service.specs.path) as db:
            db.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z'")
        contender = Reconciler(service, executor)
        assert contender.pass_once().skipped
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with schema.connect(service.specs.path) as db:
                expiry = db.execute("SELECT expires_at FROM leases").fetchone()[0]
            if expiry > "2000-01-01T00:00:00Z":
                break
            time.sleep(0.02)
        assert expiry > "2000-01-01T00:00:00Z"
    finally:
        finish.set()
        thread.join(10)
    assert not thread.is_alive()
