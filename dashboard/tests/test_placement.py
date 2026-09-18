"""T-1502: a runner goes on a healthy worker of its kind and architecture
that has room, the least loaded first - and when none has room it waits,
saying why, rather than overcommitting one or failing.
"""
import pytest

from control import inventory as inv
from control import placement
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import GH, passes  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401


def w(host, **caps):
    return {"host_id": host, "capabilities": caps}


SPEC = {"runner_id": "r-new", "platform": "linux", "architecture": "x64",
        "memory_limit": 8 * 2 ** 30}


class TestTheChoice:
    def test_the_least_loaded_of_those_that_fit(self):
        placed = [{"runner_id": "a", "host_id": "one"},
                  {"runner_id": "b", "host_id": "one"},
                  {"runner_id": "c", "host_id": "two"}]
        assert placement.choose(SPEC, [w("one"), w("two")], placed) == \
            ("two", None)

    def test_a_full_worker_is_passed_over(self):
        placed = [{"runner_id": "a", "host_id": "one"}]
        assert placement.choose(SPEC, [w("one", max_runners=1), w("two")],
                                placed)[0] == "two"

    def test_the_wrong_architecture_is_passed_over(self):
        arm = dict(SPEC, architecture="arm64")
        assert placement.choose(arm, [w("x", architecture="x64"),
                                      w("a", architecture="arm64")],
                                [])[0] == "a"

    def test_a_worker_that_says_nothing_is_x64(self):
        assert placement.choose(dict(SPEC, architecture="arm64"),
                                [w("x")], [])[0] is None

    def test_memory_is_counted_against_what_the_worker_has(self):
        placed = [{"runner_id": "a", "host_id": "one",
                   "memory_limit": 24 * 2 ** 30}]
        host, why = placement.choose(SPEC, [w("one",
                                               memory_bytes=30 * 2 ** 30)],
                                     placed)
        assert host is None and "not enough memory" in why

    def test_the_limit_is_read_under_runtime_too(self):
        """Where an agent's capabilities put it."""
        placed = [{"runner_id": "a", "host_id": "one"}]
        assert placement.choose(SPEC, [w("one", runtime={"max_runners": 1})],
                                placed)[0] is None

    def test_none_with_room_is_a_refusal_naming_each(self):
        placed = [{"runner_id": "a", "host_id": "one"},
                  {"runner_id": "b", "host_id": "two"}]
        host, why = placement.choose(SPEC, [w("one", max_runners=1),
                                            w("two", max_runners=1)], placed)
        assert host is None
        assert "one: full (1 of 1 runners)" in why
        assert "two: full (1 of 1 runners)" in why

    def test_no_worker_at_all_says_so(self):
        host, why = placement.choose(SPEC, [], [])
        assert host is None and "no healthy hyperv-linux worker" in why

    def test_its_own_old_place_does_not_count_against_it(self):
        placed = [{"runner_id": "r-new", "host_id": "one"}]
        assert placement.choose(SPEC, [w("one", max_runners=1)],
                                placed)[0] == "one"


class TestInThePlatform:
    def test_no_instance_is_placed_on_a_degraded_worker(self, world):
        service, flow, agent, forges, reconciler = world
        service.inventory.register_worker("linux-2", inv.HYPERV_LINUX)
        service.inventory.heartbeat("linux-2")
        service.inventory.mark_degraded("linux-2", "protocol major 2")
        for _ in range(3):
            service.scale_up(GH)
        passes(service, reconciler)
        hosts = {s["host_id"] for s in service.specs.list(fleet_id=GH)}
        assert hosts == {"linux-1"}

    def test_a_full_fleet_waits_in_planned_and_says_why(self, world):
        service, flow, agent, forges, reconciler = world
        service.inventory.register_worker(
            "linux-1", inv.HYPERV_LINUX, capabilities={"max_runners": 2})
        service.scale_up(GH, by=3)
        for _ in range(6):
            service.inventory.heartbeat("linux-1")
            report = reconciler.pass_once()
        states_ = sorted(s["actual_state"]
                         for s in service.specs.list(fleet_id=GH))
        assert states_ == ["idle", "idle", "planned"]
        assert len(UnitRuntime.units) == 2, "nothing overcommitted"
        assert any("full (2 of 2 runners)" in why for _, why in report.held)

    def test_and_goes_on_when_there_is_room(self, world):
        service, flow, agent, forges, reconciler = world
        service.inventory.register_worker(
            "linux-1", inv.HYPERV_LINUX, capabilities={"max_runners": 1})
        service.scale_up(GH, by=2)
        passes(service, reconciler)
        service.inventory.register_worker(
            "linux-1", inv.HYPERV_LINUX, capabilities={"max_runners": 2})
        passes(service, reconciler)
        assert sorted(s["actual_state"]
                      for s in service.specs.list(fleet_id=GH)) == \
            ["idle", "idle"]
