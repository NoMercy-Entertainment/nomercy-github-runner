"""T-1303: repair re-runs only what is incomplete, and a half-created instance
converges with no manual cleanup.

The half instance these start from is the one T-0903 leaves on purpose: a
creation that failed after registering, whose record could not be deleted,
so the unit was kept beside it. Repair takes the `failed -> provisioning`
edge and makes that whole - adopting the unit that is there and the record
that is there, rather than making a second of either and stranding the first.
"""
import pytest

from control.service import Refused
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import FJ, passes, the_runner, expire_deadlines  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401


def creates():
    return [c for c in UnitRuntime.log if c[0] == "create"]


def registers(agent):
    return [c for c in agent.calls if c[0] == "register"]


def half_made(world):
    """Registered, never came online, and the record could not be deleted:
    failed, with its unit and its record both still there."""
    service, flow, agent, forges, reconciler = world
    forges.online = False
    forges.delete_ok = False
    service.scale_up(FJ)
    passes(service, reconciler, 3)
    spec = the_runner(service, FJ)
    assert spec["actual_state"] == "registering", "offline is not proof of stopped"
    UnitRuntime.units[spec["exec_unit_ref"]]["stopped"] = True
    expire_deadlines(service)
    passes(service, reconciler, 1)
    spec = the_runner(service, FJ)
    assert spec["actual_state"] == "failed"
    assert spec["registration_id"] and spec["exec_unit_ref"], \
        "both halves are still there"
    forges.online = True
    forges.delete_ok = True
    return spec


class TestRepair:
    def test_the_half_instance_becomes_whole_with_nothing_made_twice(
            self, world):
        service, flow, agent, forges, reconciler = world
        spec = half_made(world)
        made, registered = len(creates()), len(registers(agent))

        service.repair(spec["runner_id"])
        passes(service, reconciler)

        spec = the_runner(service, FJ)
        assert spec["actual_state"] == "idle"
        assert len(creates()) == made, "the unit that was there is adopted"
        assert len(registers(agent)) == registered, \
            "the record that was there is kept, not doubled"
        assert len(forges.live_handles()) == 1
        assert not UnitRuntime.units[spec["exec_unit_ref"]]["stopped"], "repair restarts the existing stopped unit"

    def test_a_record_the_forge_no_longer_has_is_made_again(self, world):
        service, flow, agent, forges, reconciler = world
        spec = half_made(world)
        forges.forget(spec["registration_id"])
        registered = len(registers(agent))

        service.repair(spec["runner_id"])
        passes(service, reconciler)

        assert the_runner(service, FJ)["actual_state"] == "idle"
        assert len(registers(agent)) == registered + 1

    def test_a_forge_that_cannot_be_asked_is_not_registered_with_blind(
            self, world):
        service, flow, agent, forges, reconciler = world
        spec = half_made(world)
        forges.records = lambda provider: None
        registered = len(registers(agent))

        service.repair(spec["runner_id"])
        passes(service, reconciler, 3)

        spec = the_runner(service, FJ)
        assert len(registers(agent)) == registered
        assert "blind" in (spec["last_error"] or "")
        assert spec["exec_unit_ref"], "nothing was compensated either"

    def test_repair_is_only_for_a_failed_runner(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(FJ)
        passes(service, reconciler)
        spec = the_runner(service, FJ)
        assert spec["actual_state"] == "idle"
        with pytest.raises(Refused, match="starts from"):
            service.repair(spec["runner_id"])
