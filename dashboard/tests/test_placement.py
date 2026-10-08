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


G = 2 ** 30


class TestTheHardwareBoundsTheChoice:
    """A runner is never placed on a host that has fewer logical CPUs than
    its width or less physical memory than its memory limit (GitHub #5).
    The width rides on the spec placement is handed as a transient
    `cpu_width` - a pinned runner has no window before it has a host."""

    def test_a_host_narrower_than_the_width_is_passed_over(self):
        spec = dict(SPEC, cpu_width=16)
        assert placement.choose(spec, [w("small", host_cores=8),
                                       w("big", host_cores=56)], [])[0] == "big"
        host, why = placement.choose(spec, [w("small", host_cores=8)], [])
        assert host is None and "small" in why and "8 logical CPUs" in why

    def test_a_window_already_cut_is_its_width(self):
        spec = dict(SPEC, cpu_limit="32-47")
        host, why = placement.choose(spec, [w("guest", host_cores=8)], [])
        assert host is None and "16" in why

    def test_a_quota_is_its_number_of_cores(self):
        host, _ = placement.choose(dict(SPEC, cpu_limit="2.5"),
                                   [w("tiny", host_cores=2)], [])
        assert host is None
        assert placement.choose(dict(SPEC, cpu_limit="2.5"),
                                [w("ok", host_cores=4)], [])[0] == "ok"

    def test_less_physical_memory_than_the_limit_is_passed_over(self):
        host, why = placement.choose(dict(SPEC, memory_limit=16 * G),
                                     [w("small", memory_total_bytes=8 * G)], [])
        assert host is None and "small" in why and "physical memory" in why

    def test_the_hardware_report_is_read_first(self):
        host, why = placement.choose(dict(SPEC, cpu_width=16),
                                     [w("w", host_cores=56,
                                        hardware={"logical_cpus": 8})], [])
        assert host is None

    def test_unknown_hardware_does_not_refuse(self):
        assert placement.choose(dict(SPEC, cpu_width=16), [w("silent")], [])[0] == "silent"


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

    def test_a_runner_bigger_than_the_host_waits_and_says_why(self, world):
        from store import schema
        service, flow, agent, forges, reconciler = world
        service.inventory.register_worker(
            "linux-1", inv.HYPERV_LINUX,
            capabilities={"kind": "linux-container", "memory_total_bytes": 8 * G})
        # Settings refuses 16 GiB on this host, so it was saved before the
        # host shrank.
        with schema.connect(service.fleets.path) as c:
            c.execute("UPDATE fleets SET memory_limit=? WHERE fleet_id=?", (16 * G, GH))
        service.scale_up(GH, by=1)
        service.inventory.heartbeat("linux-1")
        reconciler.pass_once()
        report = reconciler.pass_once()
        spec = service.specs.list(fleet_id=GH)[0]
        assert spec["actual_state"] == "planned" and not spec["host_id"]
        assert "cpu_width" not in spec
        assert any("physical memory" in why for _, why in report.held)
        service.inventory.register_worker(
            "linux-1", inv.HYPERV_LINUX,
            capabilities={"kind": "linux-container", "memory_total_bytes": 32 * G})
        passes(service, reconciler)
        assert service.specs.list(fleet_id=GH)[0]["actual_state"] == "idle"
