"""T-1501: a fleet's desired capacity converges with nobody watching.

Scale up creates runners; scale down removes them, the idlest first, and
never touches a busy one - it drains it and waits. The capacity operation
closes when the fleet has arrived, not when the change was noticed. These run
whole reconciler passes against the fakes.
"""
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import GH, passes  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401


def fleet_states(service):
    return sorted(s["actual_state"] for s in service.specs.list(fleet_id=GH))


def set_busy(world, spec):
    service, flow, agent, forges, reconciler = world
    forges.busy.add(spec["registration_id"])


class TestScaleUp:
    def test_it_creates_what_is_missing(self, world):
        service, flow, agent, forges, reconciler = world
        service.set_capacity(GH, 3)
        passes(service, reconciler)
        assert fleet_states(service) == ["idle", "idle", "idle"]
        assert len(UnitRuntime.units) == 3

    def test_the_operation_closes_when_the_fleet_arrives(self, world):
        service, flow, agent, forges, reconciler = world
        op = service.set_capacity(GH, 2)
        assert service.operations.get(op)["state"] in ("pending", "running")
        passes(service, reconciler)
        assert service.operations.get(op)["state"] == "succeeded"


class TestScaleDown:
    def test_the_idlest_goes_first(self, world):
        service, flow, agent, forges, reconciler = world
        service.set_capacity(GH, 2)
        passes(service, reconciler)
        busy, idle = service.specs.list(fleet_id=GH)
        set_busy(world, busy)
        passes(service, reconciler, 1)

        service.set_capacity(GH, 1)
        passes(service, reconciler)

        left = service.specs.list(fleet_id=GH)
        assert [s["runner_id"] for s in left] == [busy["runner_id"]]
        assert left[0]["actual_state"] == "busy", "the busy one untouched"

    def test_with_every_runner_busy_it_waits_rather_than_aborting(
            self, world):
        service, flow, agent, forges, reconciler = world
        service.set_capacity(GH, 2)
        passes(service, reconciler)
        for spec in service.specs.list(fleet_id=GH):
            set_busy(world, spec)
        passes(service, reconciler, 1)
        units_before = dict(UnitRuntime.units)

        service.set_capacity(GH, 1)
        passes(service, reconciler)
        assert UnitRuntime.units == units_before, "nothing removed mid-job"
        assert "draining" in fleet_states(service)

        forges.busy.clear()                     # the jobs finish
        passes(service, reconciler)
        assert fleet_states(service) == ["idle"]
        assert len(UnitRuntime.units) == 1

    def test_down_to_zero_leaves_nothing(self, world):
        service, flow, agent, forges, reconciler = world
        service.set_capacity(GH, 2)
        passes(service, reconciler)
        service.set_capacity(GH, 0)
        passes(service, reconciler, 10)
        assert fleet_states(service) == []
        assert UnitRuntime.units == {}
        assert forges.live_handles() == []


class TestNobodyWatching:
    def test_up_then_down_then_up_converges_each_time(self, world):
        service, flow, agent, forges, reconciler = world
        for target in (3, 1, 2):
            service.set_capacity(GH, target)
            passes(service, reconciler, 10)
            assert fleet_states(service) == ["idle"] * target
            assert len(UnitRuntime.units) == target
