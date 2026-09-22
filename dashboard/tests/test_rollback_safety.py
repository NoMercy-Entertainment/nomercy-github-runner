"""Rollback and deadline expiry obey the same job safety rules as buttons."""
import pytest

from control.provision import RollbackHeld
from store import storage
from tests.fake_platform import Crash
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import FJ, GH, expire_deadlines, passes, the_runner, world


def interrupted_registration(world, fleet):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    reconciler.pass_once()
    agent.crash_on_ready = True
    with pytest.raises(Crash):
        reconciler.pass_once()
    return the_runner(service, fleet)


def assert_preserved(world, original):
    service, flow, agent, forges, reconciler = world
    current = service.specs.get(original["runner_id"])
    assert current["registration_id"] == original["registration_id"]
    assert storage.unit_name(original["runner_id"]) in UnitRuntime.units
    assert not any(call[0] in ("remove", "stop") for call in UnitRuntime.log)
    assert not any(call[0] == "deregister" for call in agent.calls)
    assert not forges.deleted


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_expired_registered_runner_that_started_a_job_is_recovered(world, fleet):
    service, flow, agent, forges, reconciler = world
    spec = interrupted_registration(world, fleet)
    forges.busy.add(spec["registration_id"])
    expire_deadlines(service)
    reconciler.pass_once()
    assert_preserved(world, spec)
    assert service.specs.get(spec["runner_id"])["actual_state"] == "busy"


@pytest.mark.parametrize("fleet", [GH, FJ])
@pytest.mark.parametrize("forge_state", ["busy", "offline", "unknown"])
def test_expired_registration_is_preserved_without_quiescence(world, fleet, forge_state, monkeypatch):
    service, flow, agent, forges, reconciler = world
    spec = interrupted_registration(world, fleet)
    agent._ready = False
    if forge_state == "busy":
        forges.busy.add(spec["registration_id"])
    elif forge_state == "offline":
        forges.online = False
    else:
        monkeypatch.setattr(forges, "records", lambda provider: None)
    expire_deadlines(service)
    report = reconciler.pass_once()
    assert_preserved(world, spec)
    assert report.held
    assert service.specs.get(spec["runner_id"])["actual_state"] == "registering"
    assert service.operations.get(spec["current_operation"])["state"] == "running"


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_verify_failure_after_registration_cannot_rollback_a_busy_runner(world, fleet, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    reconciler.pass_once()
    register = agent.register
    def accepts_job(*args):
        ids = register(*args)
        forges.busy.add(ids["registration_id"])
        return ids
    monkeypatch.setattr(agent, "register", accepts_job)
    agent._ready = False
    report = reconciler.pass_once()
    spec = the_runner(service, fleet)
    assert_preserved(world, spec)
    assert report.held
    assert spec["actual_state"] == "registering"
    assert service.operations.progress(spec["current_operation"])["rollback_held"]


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_unknown_forge_blocks_rollback_even_if_worker_is_stopped(world, fleet, monkeypatch):
    service, flow, agent, forges, reconciler = world
    spec = interrupted_registration(world, fleet)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    monkeypatch.setattr(forges, "records", lambda provider: None)
    with pytest.raises(RollbackHeld, match="unknown"):
        flow.abandon(spec)
    assert_preserved(world, spec)


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_known_offline_and_stopped_registration_can_be_rolled_back(world, fleet):
    service, flow, agent, forges, reconciler = world
    spec = interrupted_registration(world, fleet)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    forges.online = False
    assert flow.abandon(spec) == ("deregister", "remove_unit")
    assert not UnitRuntime.units


def test_registration_with_lost_reply_cannot_discard_its_storage(world):
    service, flow, agent, forges, reconciler = world
    spec = interrupted_registration(world, GH)
    unknown = dict(spec, registration_id=None, registration_uuid=None)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    with pytest.raises(RollbackHeld, match="reply is unknown"):
        flow.abandon(unknown)
    assert_preserved(world, spec)


def _lost_reply(world, fleet, monkeypatch, attempted_ago, registered):
    """A registration whose reply was lost, `attempted_ago` seconds back."""
    import control.provision as provision
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    reconciler.pass_once()
    if registered:
        agent.fail_on = {"register"}
    else:
        agent.fail_before_register = True
    reconciler.pass_once()
    spec = the_runner(service, fleet)
    assert spec["actual_state"] == "registering"
    assert "reply is unknown" in (spec["last_error"] or "")
    now = provision.time.time()
    monkeypatch.setattr(provision.time, "time", lambda: now + attempted_ago)
    return spec


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_a_lost_reply_is_rolled_back_once_the_forge_shows_nothing_by_that_name(world, fleet, monkeypatch):
    """GitHub's config.cmd refused before registering and the template said
    nothing, so the controller held the runner for good, though GitHub had
    no record of it (2026-09-22). Once the registration could no longer be
    in flight, a forge that lists no runner of that name proves there is
    nothing to deregister."""
    service, flow, agent, forges, reconciler = world
    spec = _lost_reply(world, fleet, monkeypatch, attempted_ago=600, registered=False)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    assert flow.abandon(service.specs.get(spec["runner_id"])) == ("deregister", "remove_unit")
    assert not UnitRuntime.units


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_a_lost_reply_is_held_while_the_registration_may_still_be_in_flight(world, fleet, monkeypatch):
    service, flow, agent, forges, reconciler = world
    spec = _lost_reply(world, fleet, monkeypatch, attempted_ago=30, registered=False)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    with pytest.raises(RollbackHeld, match="reply is unknown"):
        flow.abandon(service.specs.get(spec["runner_id"]))
    assert UnitRuntime.units


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_a_lost_reply_is_held_when_the_forge_lists_that_name(world, fleet, monkeypatch):
    """The name is only ever used to prove absence. A record by that name
    may be this runner's, so its storage and unit stay."""
    service, flow, agent, forges, reconciler = world
    spec = _lost_reply(world, fleet, monkeypatch, attempted_ago=600, registered=True)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    with pytest.raises(RollbackHeld, match="reply is unknown"):
        flow.abandon(service.specs.get(spec["runner_id"]))
    assert UnitRuntime.units


@pytest.mark.parametrize("fleet", [GH, FJ])
def test_a_lost_reply_is_held_when_the_forge_cannot_be_read(world, fleet, monkeypatch):
    service, flow, agent, forges, reconciler = world
    spec = _lost_reply(world, fleet, monkeypatch, attempted_ago=600, registered=False)
    UnitRuntime.units[storage.unit_name(spec["runner_id"])]["stopped"] = True
    monkeypatch.setattr(forges, "records", lambda provider: None)
    with pytest.raises(RollbackHeld, match="reply is unknown"):
        flow.abandon(service.specs.get(spec["runner_id"]))


@pytest.mark.parametrize("fleet", [GH, FJ])
@pytest.mark.parametrize("failure", ["create", "verify"])
def test_failed_replacement_preserves_previous_data_and_halts_siblings(world, fleet, failure, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet, by=2)
    passes(service, reconciler)
    original = service.specs.list(fleet_id=fleet)
    assert all(spec["actual_state"] == "idle" for spec in original)
    data = {storage.unit_name(spec["runner_id"]): {"work": b"previous build", "cache": b"compiled objects"}
            for spec in original}
    previous = dict(data)
    remove = UnitRuntime.remove
    def remove_with_storage(runtime, ref, keep_data=False):
        remove(runtime, ref, keep_data=keep_data)
        if not keep_data:
            data.pop(ref.handle, None)
    monkeypatch.setattr(UnitRuntime, "remove", remove_with_storage)
    operations = [service.recreate(spec["runner_id"]) for spec in original]
    if failure == "create":
        UnitRuntime.fail_on.add("create")
    else:
        agent._ready = False
    passes(service, reconciler, n=14)
    assert data == previous
    removals = [call for call in UnitRuntime.log if call[0] == "remove"]
    assert len(removals) == 2, "old unit and failed replacement are both removed"
    assert all(call[2] is True for call in removals)
    assert len({call[1] for call in removals}) == 1, "the sibling was never rebuilt"
    assert len(UnitRuntime.units) == 1
    assert all(service.operations.get(op)["state"] == "failed" for op in operations)


@pytest.mark.parametrize("fleet", [GH, FJ])
@pytest.mark.parametrize("status", ["raises", "unknown"])
def test_unknown_worker_never_replaces_or_forgets_a_registered_unit(world, fleet, status, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    passes(service, reconciler)
    spec = the_runner(service, fleet)
    UnitRuntime.log.clear()
    def uncertain(*args):
        if status == "raises":
            raise RuntimeError("worker status timeout")
        return None
    monkeypatch.setattr(UnitRuntime, "status", uncertain)
    monkeypatch.setattr(forges, "records", lambda provider: None)
    with pytest.raises(RollbackHeld):
        flow.provision(spec)
    assert_preserved(world, spec)
    assert not UnitRuntime.log


@pytest.mark.parametrize("fleet", [GH, FJ])
@pytest.mark.parametrize("forge_state", ["busy", "unknown", "delete-failed"])
def test_absent_unit_replacement_retains_identity_until_forge_cleanup_succeeds(world, fleet, forge_state, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    passes(service, reconciler)
    spec = the_runner(service, fleet)
    UnitRuntime.units.clear()  # confirmed absent, with persistent storage retained
    UnitRuntime.log.clear()
    if forge_state == "busy":
        forges.busy.add(spec["registration_id"])
    elif forge_state == "unknown":
        monkeypatch.setattr(forges, "records", lambda provider: None)
    else:
        monkeypatch.setattr(forges, "delete", lambda *args: False)
    with pytest.raises(RollbackHeld):
        flow.provision(spec)
    after = service.specs.get(spec["runner_id"])
    assert after["registration_id"] == spec["registration_id"]
    assert after["registration_uuid"] == spec["registration_uuid"]
    assert not UnitRuntime.log, "no replacement create or storage removal is safe"
    assert not forges.deleted


@pytest.mark.parametrize("existing", [False, True])
def test_read_failure_before_create_recovers_on_next_pass_without_sweeping(world, existing, monkeypatch):
    service, flow, agent, forges, reconciler = world
    service.scale_up(GH)
    if existing:
        UnitRuntime.crash_after_create = True
        with pytest.raises(Crash):
            reconciler.pass_once()
    status = UnitRuntime.status
    def unavailable(*args):
        raise RuntimeError("temporary worker outage")
    monkeypatch.setattr(UnitRuntime, "status", unavailable)
    report = reconciler.pass_once()
    spec = the_runner(service)
    assert spec["actual_state"] == "provisioning"
    assert report.held
    monkeypatch.setattr(UnitRuntime, "status", status)
    expire_deadlines(service)  # a recovered worker must not trigger destructive sweep
    reconciler.pass_once()
    assert the_runner(service)["actual_state"] == "provisioned"
    passes(service, reconciler)
    assert the_runner(service)["actual_state"] == "idle"
    assert len([call for call in UnitRuntime.log if call[0] == "create"]) == 1
    assert not any(call[0] == "remove" for call in UnitRuntime.log)
