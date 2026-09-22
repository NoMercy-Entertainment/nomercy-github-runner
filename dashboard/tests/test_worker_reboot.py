"""Stopped manual-start services recover only on fresh, agreeing evidence."""
import pytest

from runtime.base import ExecUnitStatus
from store import schema
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import world, GH, FJ, passes, the_runner


def rebooted(world, fleet, actual="idle"):
    service, flow, agent, forges, reconciler = world
    service.scale_up(fleet)
    passes(service, reconciler)
    spec = the_runner(service, fleet)
    assert spec["actual_state"] == "idle" and not spec["current_operation"]
    if actual != "idle":
        service.specs.update(spec["runner_id"], spec["spec_version"], actual_state=actual)
    UnitRuntime.units[spec["exec_unit_ref"]]["stopped"] = True
    UnitRuntime.log.clear()
    return service.specs.get(spec["runner_id"])


@pytest.mark.parametrize("fleet", [GH, FJ])
@pytest.mark.parametrize("actual", ["idle", "busy"])
def test_reboot_recovers_existing_registration_without_recreation(world, fleet, actual):
    service, flow, agent, forges, reconciler = world
    spec = rebooted(world, fleet, actual)
    registered = len([c for c in agent.calls if c[0] == "register"])
    assert flow.observe_fresh(spec) == "stopped"
    passes(service, reconciler, 4)
    current = the_runner(service, fleet)
    assert current["actual_state"] == "idle"
    assert current["registration_id"] == spec["registration_id"]
    assert len([c for c in agent.calls if c[0] == "register"]) == registered
    assert not {"create", "remove"}.intersection(c[0] for c in UnitRuntime.log)
    assert any(c[0] == "start" for c in UnitRuntime.log)


@pytest.mark.parametrize("evidence", ["forge_unknown", "forge_busy", "unit_unknown", "unit_running", "unit_absent", "worker_stale"])
def test_reboot_recovery_refuses_incomplete_or_busy_evidence(world, monkeypatch, evidence):
    service, flow, agent, forges, reconciler = world
    spec = rebooted(world, GH)
    if evidence == "forge_unknown":
        monkeypatch.setattr(forges, "records", lambda provider: None)
    elif evidence == "forge_busy":
        forges.busy.add(spec["registration_id"])
    elif evidence.startswith("unit_"):
        status = {"unit_unknown": ExecUnitStatus(exists=True, running=None),
                  "unit_running": ExecUnitStatus(exists=True, running=True),
                  "unit_absent": ExecUnitStatus(exists=False, running=False)}[evidence]
        monkeypatch.setattr(UnitRuntime, "status", lambda self, ref: status)
    else:
        with schema.connect(service.specs.path) as db:
            db.execute("UPDATE workers SET last_seen_at = NULL")
    assert flow.observe_fresh(spec) != "stopped"
    reconciler.pass_once()
    assert not any(c[0] == "start" for c in UnitRuntime.log)


def test_maintenance_prevents_reboot_recovery(world):
    service, flow, agent, forges, reconciler = world
    rebooted(world, GH)
    with schema.connect(service.specs.path) as db:
        db.execute("INSERT INTO platform_settings VALUES ('maintenance','true')")
    reconciler.pass_once()
    assert not any(c[0] == "start" for c in UnitRuntime.log)


def test_desired_stopped_never_recovers_automatically(world):
    service, flow, agent, forges, reconciler = world
    spec = rebooted(world, GH)
    service.specs.update(spec["runner_id"], spec["spec_version"], desired_state="stopped", actual_state="stopped")
    passes(service, reconciler, 3)
    assert not any(c[0] == "start" for c in UnitRuntime.log)


def test_completed_operation_pointer_does_not_block_reboot_recovery(world):
    service, flow, agent, forges, reconciler = world
    spec = rebooted(world, GH)
    operation, _ = service.operations.open("start", runner_id=spec["runner_id"], requested_by="test")
    service.operations.succeed(operation["operation_id"])
    service.specs.update(spec["runner_id"], spec["spec_version"], current_operation=operation["operation_id"])
    passes(service, reconciler, 4)
    current = the_runner(service)
    assert current["actual_state"] == "idle" and current["current_operation"] is None
    assert any(c[0] == "start" for c in UnitRuntime.log)
