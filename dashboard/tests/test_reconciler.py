"""The reconciler: converging, and the things it must never do.

The plan's four properties each have a class: it converges up and down, it
never overshoots, a second concurrent pass does nothing, and a degraded worker
receives no destructive verb. Its definition of done has one too: repeated
passes over a converged fleet perform no work.

Two more are asserted because the design leans on them and they are easy to
lose. It never aborts a job - a busy runner that must go is drained, never
stopped. And `decide`, the pure function that picks each step, is checked
exhaustively: for every combination of what a runner is, what it should be and
what operation is in flight, it never proposes a step the state machine cannot
take from where the runner stands.

The executor is a fake that records calls. Convergence is measured by its
mutating calls, not by reading the reconciler's own report, so a pass that
claimed to do nothing while quietly calling out would still fail.
"""
import ast
import itertools
import os

import pytest

from control import inventory as inv
from control import states
from control.reconciler import Reconciler, decide
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore, fleet_id
from tests.fake_executor import FakeExecutor
from tests.fake_runtime import ALL_CELLS

GH = fleet_id("github", "linux", "x64")
FJ = fleet_id("forgejo", "linux", "x64")
WORKER = "linux-worker-1"


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed()
    service = RunnerService(path, runtimes=dict(ALL_CELLS))
    service.inventory.register_worker(WORKER, inv.HYPERV_LINUX)
    service.inventory.heartbeat(WORKER)
    executor = FakeExecutor(host_id=WORKER)
    return service, executor, Reconciler(service, executor)


def converge(service, reconciler, passes=20):
    """Run passes until one does nothing, keeping the worker's heartbeat
    fresh. Returns how many passes it took; fails if it never settles."""
    for n in range(1, passes + 1):
        service.inventory.heartbeat(WORKER)
        report = reconciler.pass_once()
        if not report.actions:
            return n
    pytest.fail(f"did not converge in {passes} passes")


def live(service, fid=GH):
    return [s for s in service.specs.list(fleet_id=fid)
            if s["actual_state"] != "absent"]


class TestConvergesUp:
    def test_maintenance_enabled_during_a_pass_holds_the_next_runner(self, world):
        service, executor, reconciler = world
        service.scale_up(GH, by=2)
        reconciler.pass_once()
        register = executor.register
        def enter_maintenance(*args, **kwargs):
            result = register(*args, **kwargs)
            with schema.connect(service.specs.path) as c:
                c.execute("INSERT INTO platform_settings VALUES ('maintenance','true')")
            return result
        executor.register = enter_maintenance
        report = reconciler.pass_once()
        assert sum(c[0] == "register" for c in executor.mutations()) == 1
        assert ("platform", "maintenance") in report.held
        assert sorted(s["actual_state"] for s in live(service)) == ["idle", "provisioned"]

    def test_a_fleet_reaches_its_capacity(self, world):
        service, executor, reconciler = world
        service.scale_up(GH, by=3)

        converge(service, reconciler)

        runners = live(service)
        assert len(runners) == 3
        assert all(s["actual_state"] == "idle" for s in runners)

    def test_each_runner_went_through_provision_and_register(self, world):
        service, executor, reconciler = world
        service.scale_up(GH, by=2)
        converge(service, reconciler)
        verbs = [c[0] for c in executor.mutations()]
        assert verbs.count("provision") == 2
        assert verbs.count("register") == 2

    def test_a_converged_runner_carries_what_it_was_given(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        spec = live(service)[0]
        assert spec["exec_unit_ref"].startswith("unit-")
        assert spec["host_id"] == WORKER
        assert spec["registration_id"].startswith("reg-")

    def test_it_takes_one_step_per_runner_per_pass(self, world):
        """A pass that drove a runner from planned to idle in one go would hold
        everything else up for minutes, and an interruption would leave no
        record of how far it got."""
        service, executor, reconciler = world
        service.scale_up(GH)
        reconciler.pass_once()          # plans, then provisions
        assert [c[0] for c in executor.mutations()] == ["provision"]
        reconciler.pass_once()          # registers
        assert [c[0] for c in executor.mutations()] == ["provision",
                                                          "register"]

    def test_the_capacity_operation_closes_when_the_fleet_serves(self, world):
        service, executor, reconciler = world
        operation_id = service.scale_up(GH, by=2)
        converge(service, reconciler)
        assert service.operations.get(operation_id)["state"] == "succeeded"

    def test_a_fleet_with_nothing_to_run_on_keeps_its_operation_open(
            self, world):
        """A capacity that cannot be met is not done, and saying so is the
        truth. The sweeper finds it once its deadline passes."""
        service, executor, reconciler = world
        executor.fail_on = {"provision"}
        operation_id = service.scale_up(GH)
        for _ in range(4):
            reconciler.pass_once()
        assert service.operations.get(operation_id)["state"] == "pending"


class TestConvergesDown:
    def test_a_fleet_shrinks_to_its_capacity(self, world):
        service, executor, reconciler = world
        service.scale_up(GH, by=3)
        converge(service, reconciler)

        service.scale_down(GH, by=2)
        converge(service, reconciler)

        assert len(live(service)) == 1

    def test_removed_runners_are_deregistered_then_removed(self, world):
        """Deregistration comes first, or the forge keeps a record of a runner
        that no longer exists."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        service.scale_down(GH)
        converge(service, reconciler)

        verbs = [c[0] for c in executor.mutations()]
        assert verbs.index("deregister") < verbs.index("remove")

    def test_a_removed_runner_keeps_nothing(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        service.scale_down(GH)
        converge(service, reconciler)
        removal = [c for c in executor.calls if c[0] == "remove"][0]
        assert removal[2] == {"keep_data": False}

    def test_a_removed_runner_stays_readable_for_history(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]
        service.scale_down(GH)
        converge(service, reconciler)

        spec = service.specs.get(runner_id)
        assert spec["actual_state"] == "absent"
        assert spec["deleted_at"]

    def test_the_cheapest_runner_goes_first(self, world):
        """A planned runner exists only on paper; losing it costs nothing."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        serving = live(service)[0]["runner_id"]

        service.scale_up(GH)                 # a second, still planned
        service.scale_down(GH)               # before it is ever built
        reconciler.pass_once()

        assert serving in [s["runner_id"] for s in live(service)]


class TestAPlannedRunnerCanBeWithdrawn:
    """The first edge added while building this: without it, a runner that
    existed only on paper could only be cancelled by first building it."""

    def test_scaling_down_before_anything_is_built(self, world):
        service, executor, reconciler = world
        executor.fail_on = {"provision"}      # nothing ever gets built
        service.scale_up(GH, by=2)
        reconciler.pass_once()
        service.scale_down(GH, by=2)

        converge(service, reconciler)

        assert live(service) == []

    def test_withdrawing_touches_nothing_outside(self, world):
        """A spec the fleet does not want - planned while capacity was zero -
        is withdrawn on the next pass without a single call outwards."""
        service, executor, reconciler = world
        runner_id = service.planned_ids(service.plan(GH, 1))[0]
        reconciler.pass_once()
        assert executor.mutations() == []
        assert service.specs.get(runner_id)["actual_state"] == "absent"


class TestAHalfBuiltRunnerIsFinishedBeforeItIsRemoved:
    """A livelock found while writing the reconciler.

    `provisioned` has only one way out: registering. A scale-down that arrived
    between provisioning and registering left the runner there for ever -
    observed on every pass, moved by none.
    """

    def test_it_does_not_stick_at_provisioned(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        reconciler.pass_once()          # planned, then provisioned
        runner_id = live(service)[0]["runner_id"]
        assert service.specs.get(runner_id)["actual_state"] == "provisioned"

        service.set_desired(runner_id, "absent")
        converge(service, reconciler)

        assert service.specs.get(runner_id)["actual_state"] == "absent"


class TestNeverOvershoots:
    def test_the_count_never_exceeds_what_was_asked(self, world):
        """Checked after every single pass, not only at the end: an overshoot
        that is later corrected still ran a runner nobody paid for."""
        service, executor, reconciler = world
        service.scale_up(GH, by=3)
        for _ in range(15):
            service.inventory.heartbeat(WORKER)
            reconciler.pass_once()
            assert len(live(service)) <= 3

    def test_runners_still_booting_are_counted(self, world):
        """Counting only healthy runners would plan a replacement for each one
        still starting, growing the fleet by however many were booting."""
        service, executor, reconciler = world
        service.scale_up(GH, by=2)
        reconciler.pass_once()          # two planned, now provisioned
        reconciler.pass_once()
        reconciler.pass_once()
        assert len(live(service)) == 2

    def test_a_runner_on_its_way_out_still_counts_against_adding(self,
                                                                world):
        """The replacement waits for its predecessor. That dips capacity by
        one, which the design accepts; the alternative is the overshoot it
        forbids."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        leaving = live(service)[0]
        service.specs.update(leaving["runner_id"], leaving["spec_version"],
                             actual_state="busy")
        service.set_desired(leaving["runner_id"], "absent")

        reconciler.pass_once()          # busy: drain, and plan nothing yet

        assert len(live(service)) == 1

    def test_scaling_down_marks_exactly_the_excess(self, world):
        """Only runners still meant to stay are counted when removing, or
        every pass would mark one more until the fleet was empty."""
        service, executor, reconciler = world
        service.scale_up(GH, by=4)
        converge(service, reconciler)
        service.scale_down(GH, by=1)

        reconciler.pass_once()
        reconciler.pass_once()

        wanted = [s for s in live(service) if s["desired_state"] != "absent"]
        assert len(wanted) == 3


class TestASecondPassDoesNothing:
    def test_while_one_holds_the_lease(self, world):
        service, executor, reconciler = world
        service.scale_up(GH, by=2)
        other = Reconciler(service, executor, holder="someone-else")

        assert reconciler._acquire()
        try:
            report = other.pass_once()
        finally:
            reconciler._release()

        assert report.skipped is True
        assert report.actions == []
        assert service.specs.list() == []

    def test_two_concurrent_passes_plan_the_gap_once(self, world):
        """The overshoot the lease exists to prevent: both passes see a fleet
        short by one and both plan the missing runner."""
        import threading
        service, executor, reconciler = world
        service.scale_up(GH)
        others = [Reconciler(service, executor, holder=f"r{i}")
                  for i in range(4)]
        threads = [threading.Thread(target=r.pass_once) for r in others]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(live(service)) == 1

    def test_an_expired_lease_is_taken_over(self, world):
        """A pass that crashed while holding the lease must not stop every
        later one."""
        service, executor, reconciler = world
        with schema.connect(service.specs.path) as c:
            c.execute("INSERT INTO leases (name, holder, expires_at)"
                      " VALUES ('reconciler', 'crashed',"
                      " '2000-01-01T00:00:00Z')")
        assert reconciler.pass_once().skipped is False

    def test_the_lease_is_released_after_a_pass(self, world):
        service, executor, reconciler = world
        reconciler.pass_once()
        other = Reconciler(service, executor, holder="next")
        assert other.pass_once().skipped is False


class TestADegradedWorkerReceivesNoDestructiveVerb:
    def test_removal_is_held(self, world):
        """Unreachable and absent look the same from here. Removing on that
        evidence deletes capacity that was only out of touch."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.specs.update(runner["runner_id"], runner["spec_version"],
                             actual_state="stopped")
        service.set_desired(runner["runner_id"], "absent")
        executor.calls.clear()

        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = ?"
                      " WHERE host_id = ?",
                      ("2000-01-01T00:00:00Z", WORKER))
        report = reconciler.pass_once()

        destructive = {"stop", "deregister", "remove"}
        assert not [c for c in executor.calls if c[0] in destructive]
        assert any("not healthy" in reason for _, reason in report.held)

    def test_it_proceeds_once_the_worker_answers_again(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.specs.update(runner["runner_id"], runner["spec_version"],
                             actual_state="stopped")
        service.set_desired(runner["runner_id"], "absent")
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = ?",
                      ("2000-01-01T00:00:00Z",))
        reconciler.pass_once()

        converge(service, reconciler)       # heartbeats again

        assert service.specs.get(runner["runner_id"])["actual_state"] == \
            "absent"

    def test_a_worker_missing_from_the_inventory_counts_as_unreachable(
            self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.specs.update(runner["runner_id"], runner["spec_version"],
                             actual_state="stopped", host_id=None)
        spec = service.specs.get(runner["runner_id"])
        # Point it at a worker nobody registered.
        with schema.connect(service.specs.path) as c:
            c.execute("INSERT INTO workers (host_id, kind) VALUES"
                      " ('ghost', 'hyperv-linux')")
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             host_id="ghost")
        service.set_desired(runner["runner_id"], "absent")
        executor.calls.clear()

        reconciler.pass_once()

        assert not [c for c in executor.calls if c[0] == "deregister"]


class TestNeverAbortsAJob:
    def test_a_busy_runner_that_must_go_is_drained(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.specs.update(runner["runner_id"], runner["spec_version"],
                             actual_state="busy")
        executor.world[runner["runner_id"]] = "busy"    # the forge agrees
        service.set_desired(runner["runner_id"], "absent")
        executor.calls.clear()

        reconciler.pass_once()

        assert [c[0] for c in executor.mutations()] == ["drain"]
        assert service.specs.get(runner["runner_id"])["actual_state"] == \
            "draining"

    def test_it_is_removed_only_after_the_job_finishes(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="busy")
        executor.world[runner_id] = "busy"
        service.set_desired(runner_id, "absent")

        for _ in range(3):
            service.inventory.heartbeat(WORKER)
            reconciler.pass_once()
        assert service.specs.get(runner_id)["actual_state"] == "draining"
        assert "deregister" not in [c[0] for c in executor.mutations()]

        executor.world[runner_id] = "drained"       # the job finished
        converge(service, reconciler)

        assert service.specs.get(runner_id)["actual_state"] == "absent"


class TestConvergedMeansNoWork:
    """T-0302's definition of done."""

    def test_repeated_passes_over_a_converged_fleet_mutate_nothing(self,
                                                                   world):
        service, executor, reconciler = world
        service.scale_up(GH, by=3)
        service.scale_up(FJ, by=2)
        converge(service, reconciler)
        executor.calls.clear()
        versions = {s["runner_id"]: s["spec_version"]
                    for s in service.specs.list()}

        for _ in range(5):
            service.inventory.heartbeat(WORKER)
            report = reconciler.pass_once()
            assert report.actions == []

        assert executor.mutations() == []
        assert {s["runner_id"]: s["spec_version"]
                for s in service.specs.list()} == versions

    def test_an_empty_platform_is_converged_from_the_start(self, world):
        service, executor, reconciler = world
        assert reconciler.pass_once().actions == []


class TestObservation:
    def test_a_job_starting_is_recorded(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]

        executor.world[runner_id] = "busy"
        reconciler.pass_once()

        assert service.specs.get(runner_id)["actual_state"] == "busy"

    def test_an_impossible_observation_is_ignored(self, world):
        """Believing it would write a state the runner never passed through to
        get there."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]

        executor.world[runner_id] = "absent"      # idle -> absent: no edge
        report = reconciler.pass_once()

        assert service.specs.get(runner_id)["actual_state"] == "idle"
        assert any("ignored observation" in r for _, r in report.held)

    def test_a_requested_edge_is_not_accepted_as_an_observation(self, world):
        """idle -> stopping can only be asked for. An executor reporting it
        would be reporting a stop nobody requested."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]
        executor.world[runner_id] = "stopping"
        reconciler.pass_once()
        assert service.specs.get(runner_id)["actual_state"] == "idle"


class TestOperationsAreCarriedOut:
    def a_serving_runner(self, world):
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        executor.calls.clear()
        return live(service)[0]["runner_id"]

    def test_stop_then_start(self, world):
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)

        stop = service.stop(runner_id)
        converge(service, reconciler)
        assert service.specs.get(runner_id)["actual_state"] == "stopped"
        assert service.operations.get(stop)["state"] == "succeeded"

        start = service.start(runner_id)
        converge(service, reconciler)
        assert service.specs.get(runner_id)["actual_state"] == "idle"
        assert service.operations.get(start)["state"] == "succeeded"

    def test_restart_stops_then_starts_and_then_closes(self, world):
        """Idle before and idle after, so only the recorded progress can tell
        a finished restart from one that has not begun."""
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)

        operation_id = service.restart(runner_id)
        converge(service, reconciler)

        assert [c[0] for c in executor.mutations()] == ["drain", "stop", "start"]
        assert service.operations.get(operation_id)["state"] == "succeeded"
        assert service.specs.get(runner_id)["actual_state"] == "idle"

    def test_restart_of_a_runner_that_took_a_job_drains_it_first(self,
                                                                 world):
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)
        operation_id = service.restart(runner_id)
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="busy")
        executor.world[runner_id] = "busy"

        reconciler.pass_once()

        assert [c[0] for c in executor.mutations()] == ["drain"]
        executor.world[runner_id] = "drained"
        converge(service, reconciler)
        assert service.operations.get(operation_id)["state"] == "succeeded"

    def test_recreate_keeps_its_storage_and_its_identity(self, world):
        """The second edge added while building this. Storage is named from the
        runner_id, so the same runner goes back through provisioning rather
        than a new one being created beside the kept data."""
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="drained")

        operation_id = service.recreate(runner_id)
        converge(service, reconciler)

        removal = [c for c in executor.calls if c[0] == "remove"][0]
        assert removal[2] == {"keep_data": True}
        verbs = [c[0] for c in executor.mutations()]
        assert verbs == ["deregister", "remove", "provision", "register"]
        after = service.specs.get(runner_id)
        assert after["actual_state"] == "idle"
        assert after["deleted_at"] is None
        assert service.operations.get(operation_id)["state"] == "succeeded"

    def test_clear_cache_runs_on_an_idle_runner(self, world):
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)
        executor.world[runner_id] = "idle"
        operation_id = service.clear_cache(runner_id)
        reconciler.pass_once()
        reconciler.pass_once()
        operation = service.operations.get(operation_id)
        assert operation["state"] == "succeeded"
        assert "1024" in operation["result"]

    def test_clear_cache_fails_if_the_runner_took_a_job_meanwhile(self,
                                                                  world):
        """Refused rather than taking the cache out from under a build."""
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)
        operation_id = service.clear_cache(runner_id)
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="busy")

        reconciler.pass_once()

        assert "clear_cache" not in [c[0] for c in executor.mutations()]
        assert service.operations.get(operation_id)["state"] == "failed"

    def test_a_failed_step_fails_its_operation_and_says_why(self, world):
        service, executor, reconciler = world
        runner_id = self.a_serving_runner(world)
        executor.fail_on = {"stop"}
        operation_id = service.stop(runner_id)

        reconciler.pass_once()  # quiesce before the stop that fails
        reconciler.pass_once()

        operation = service.operations.get(operation_id)
        assert operation["state"] == "failed"
        assert "stop failed on purpose" in operation["error"]
        assert "stop failed" in service.specs.get(runner_id)["last_error"]

    def test_a_failed_runner_is_not_retried_automatically(self, world):
        """A failure that repeats would be retried for ever, filling the trace
        and hiding the cause. A human repair is one click."""
        service, executor, reconciler = world
        executor.fail_on = {"provision"}
        service.scale_up(GH)
        reconciler.pass_once()
        runner_id = live(service)[0]["runner_id"]
        assert service.specs.get(runner_id)["actual_state"] == "failed"

        calls = len(executor.mutations())
        reconciler.pass_once()
        reconciler.pass_once()
        assert len(executor.mutations()) == calls

    def test_repair_brings_a_failed_runner_back(self, world):
        service, executor, reconciler = world
        executor.fail_on = {"provision"}
        service.scale_up(GH)
        reconciler.pass_once()
        runner_id = live(service)[0]["runner_id"]
        executor.fail_on = set()

        operation_id = service.repair(runner_id)
        converge(service, reconciler)

        assert service.specs.get(runner_id)["actual_state"] == "idle"
        assert service.operations.get(operation_id)["state"] == "succeeded"
        assert service.specs.get(runner_id)["last_error"] is None


#: The state each step starts from. `decide` may only propose a step from one
#: of these, or the reconciler would try to take an edge the machine lacks.
STEP_FROM = {
    "provision": {"planned", "provisioning"},
    "register": {"provisioned"},
    # The last entry of each of these three is a re-drive (T-0308): a step
    # interrupted part-way is taken again from the transitional state it left
    # the runner in, and each then leaves by that state's own edge -
    # stopping -> stopped, starting -> idle, deregistering -> removing.
    "start": {"stopped", "starting"},
    "stop": {"idle", "drained", "stopping"},
    "deregister": {"drained", "stopped", "deregistering"},
    # draining is a re-drive, like stopping for stop.
    "drain": {"idle", "busy", "draining"},
    "cancel_drain": {"drained"},
    "withdraw": {"planned"},
    "remove": {"removing", "failed"},
    "repair": {"failed"},
}


class TestDecideNeverProposesAnImpossibleStep:
    """Exhaustive over every runner state, every desired state and every
    operation in flight, with and without progress recorded."""

    OPERATIONS = [None, "restart", "recreate", "repair", "clear_cache",
                  "stop", "drain"]
    PROGRESS = [{}, {"done": ["stopped"]}, {"done": ["stopped", "started"]},
                {"done": ["rebuilding"]}]

    def cases(self):
        for actual, desired, verb, progress in itertools.product(
                sorted(states.STATES), sorted({"running", "stopped",
                                               "drained", "absent"}),
                self.OPERATIONS, self.PROGRESS):
            spec = {"actual_state": actual, "desired_state": desired}
            operation = {"verb": verb} if verb else None
            yield spec, operation, progress

    def test_every_proposed_step_starts_where_the_runner_is(self):
        checked = 0
        for spec, operation, progress in self.cases():
            action = decide(spec, operation, progress)
            checked += 1
            if action in (None, "observe", "clear_cache"):
                continue
            assert spec["actual_state"] in STEP_FROM[action], (
                f"{action} proposed from {spec['actual_state']} "
                f"(desired {spec['desired_state']}, op {operation})")
        assert checked > 1000

    def test_nothing_that_takes_a_runner_down_is_proposed_while_busy(self):
        """MIG-9, checked over every combination rather than a few examples."""
        for spec, operation, progress in self.cases():
            if spec["actual_state"] != "busy":
                continue
            assert decide(spec, operation, progress) not in (
                "stop", "deregister", "remove", "withdraw")

    def test_an_absent_runner_is_left_alone(self):
        for spec, operation, progress in self.cases():
            if spec["actual_state"] == "absent":
                assert decide(spec, operation, progress) is None


class TestOnlyTheReconcilerWritesActualState:
    """Checked in the source. The service records what is wanted; a second
    writer of what is would be claiming to know something it did not see."""

    def test_nothing_else_in_the_controller_moves_it(self):
        here = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "control")
        offenders = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name == "reconciler.py":
                continue
            tree = ast.parse(open(os.path.join(here, name),
                                  encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr in ("update", "create")):
                    continue
                for kw in node.keywords:
                    if kw.arg != "actual_state":
                        continue
                    creating_a_plan = (
                        name == "service.py" and func.attr == "create"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == "planned")
                    if not creating_a_plan:
                        offenders.append(f"{name}:{node.lineno}")
        assert offenders == [], (
            "only the reconciler writes actual_state; the one exception is a "
            f"new spec being born `planned`: {offenders}")


class TestAnOperationThatStartsFromFailedIsNotFailedByIt:
    """A bug found while writing this file.

    The reconciler fails an operation whose runner has gone to `failed`. The
    first version applied that to every operation - including `repair`, whose
    starting point IS `failed`. A repair was failed by the state it was asked
    to fix, before it had taken a single step.
    """

    def broken_runner(self, world):
        service, executor, reconciler = world
        executor.fail_on = {"provision"}
        service.scale_up(GH)
        reconciler.pass_once()
        executor.fail_on = set()
        runner_id = live(service)[0]["runner_id"]
        assert service.specs.get(runner_id)["actual_state"] == "failed"
        return runner_id

    def test_removing_a_broken_runner(self, world):
        service, executor, reconciler = world
        runner_id = self.broken_runner(world)

        operation_id = service.remove(runner_id)
        converge(service, reconciler)

        assert service.operations.get(operation_id)["state"] == "succeeded"
        assert service.specs.get(runner_id)["actual_state"] == "absent"

    def test_an_operation_that_cannot_start_from_failed_is_failed(self,
                                                                   world):
        """The rule still holds where it should: a `stop` in flight on a
        runner that has broken has failed."""
        service, executor, reconciler = world
        service.scale_up(GH)
        converge(service, reconciler)
        runner_id = live(service)[0]["runner_id"]
        operation_id = service.stop(runner_id)
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="failed")

        reconciler.pass_once()

        assert service.operations.get(operation_id)["state"] == "failed"


class TestAddingAndRemovingIsTheWholeInterface:
    """An operator's model, and the one the page now offers: there are
    runners or there are not. Adding one adds one; removing one removes one.

    Until 2026-09-20 removing a runner left the fleet wanting the same
    number, so the next pass planned a replacement - correct by the design's
    own rule, and baffling to use: the runner you removed came straight back,
    and the only way to shrink a fleet was a number box beside the buttons.

    Capacity is still what the controller keeps true, and still what brings
    a fleet back after a reboot. It is no longer something anyone has to
    type.
    """

    def wants(self, service, fid=GH):
        return service.fleets.get(fid)["desired_capacity"]

    def test_removing_a_runner_lowers_what_the_fleet_wants(self, world):
        service, executor, reconciler = world
        service.create(GH)
        converge(service, reconciler)
        before = self.wants(service)
        service.retire(live(service)[0]["runner_id"], requested_by="operator")
        assert self.wants(service) == before - 1

    def test_so_the_next_pass_plans_no_replacement(self, world):
        service, executor, reconciler = world
        service.create(GH)
        converge(service, reconciler)
        service.retire(live(service)[0]["runner_id"], requested_by="operator")
        converge(service, reconciler)
        assert live(service) == []

    def test_it_never_goes_below_zero(self, world):
        service, executor, reconciler = world
        service.create(GH)
        converge(service, reconciler)
        runner = live(service)[0]["runner_id"]
        service.retire(runner, requested_by="operator")
        service.retire(runner, requested_by="operator",
                       idempotency_key="again")
        assert self.wants(service) >= 0

    def test_a_recreate_is_not_a_removal(self, world):
        """Recreate rebuilds the runners a fleet has; it does not make the
        fleet smaller."""
        service, executor, reconciler = world
        service.create(GH)
        converge(service, reconciler)
        before = self.wants(service)
        service.recreate(live(service)[0]["runner_id"],
                         requested_by="operator")
        assert self.wants(service) == before


class TestEveryRunnerIsNamedTheSameWay:
    """One name, the same shape for every runner, decided here and used at
    the forge (2026-09-20).

    The fleet had three eras of naming in it - `nomercy-zecti` that GitHub
    invented, `forgejo-runner-1` typed by hand, `beaststack-macos-sequoia`
    that came with the appliance - because each runner kept whatever it was
    called when it arrived. A name is presentation (11.2), so this is a
    presentation rule: the fleet it belongs to and the lowest free number in
    it.
    """

    def names(self, service, fid=GH):
        return sorted(s["display_name"] for s in service.specs.list(fleet_id=fid)
                      if s["actual_state"] != "absent")

    def test_a_planned_runner_is_named_after_its_fleet(self, world):
        service, executor, reconciler = world
        service.plan(GH, 2)
        assert self.names(service) == [f"{GH}-1", f"{GH}-2"]

    def test_the_lowest_free_number_is_taken(self, world):
        service, executor, reconciler = world
        service.plan(GH, 3)
        second = [s for s in service.specs.list(fleet_id=GH)
                  if s["display_name"] == f"{GH}-2"][0]
        service.specs.update(second["runner_id"], second["spec_version"],
                             actual_state="absent")
        service.plan(GH, 1)
        assert self.names(service) == [f"{GH}-1", f"{GH}-2", f"{GH}-3"]

    def test_each_fleet_counts_for_itself(self, world):
        service, executor, reconciler = world
        service.plan(GH, 1)
        service.plan(FJ, 1)
        assert self.names(service) == [f"{GH}-1"]
        assert self.names(service, FJ) == [f"{FJ}-1"]

    def test_the_forge_is_told_that_name(self, world):
        import providers
        service, executor, reconciler = world
        service.plan(GH, 1)
        spec = service.specs.list(fleet_id=GH)[0]
        assert providers._forge_name(spec) == f"{GH}-1"


class TestAnErrorIsAboutTheRunnerAsItIs:
    """A runner that is serving has no error.

    `last_error` is the reason the runner is where it is. Once it is alive
    again the reason is history, and history is what the operation and the
    audit trail keep. Left on the row it is painted on the card for ever:
    the page showed four idle runners in red over removals that had since
    succeeded, hours after the fact (2026-09-21).
    """

    def serving(self, service, reconciler, error):
        service.create(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.specs.update(runner["runner_id"],
                             service.specs.get(
                                 runner["runner_id"])["spec_version"],
                             last_error=error)
        return service.specs.get(runner["runner_id"])

    def test_coming_back_to_life_clears_it(self, world):
        service, _, reconciler = world
        spec = self.serving(service, reconciler,
                            "2026-09-20T19:14:34Z remove: 500: boom")
        reconciler._move(spec, "busy")
        assert service.specs.get(spec["runner_id"])["last_error"] is None

    def test_a_note_is_not_an_error_and_stays(self, world):
        """The labels a runner registered with, say: not a failure, and the
        card keeps them apart."""
        service, _, reconciler = world
        spec = self.serving(service, reconciler, None)
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             last_note="registered with other labels")
        spec = service.specs.get(spec["runner_id"])
        reconciler._move(spec, "busy")
        assert service.specs.get(spec["runner_id"])["last_note"]

    def test_one_that_is_still_failing_keeps_its_reason(self, world):
        """Only life clears it. A runner on its way out of a failure still
        says what went wrong."""
        service, _, reconciler = world
        spec = self.serving(service, reconciler, "boom")
        reconciler._move(spec, "draining")
        assert service.specs.get(spec["runner_id"])["last_error"] == "boom"


class TestARemovalThatLostItsOperation:
    """A spec in `removing` with no operation open is stranded: nothing will
    move it again. That is what a rebuild left behind when its removal
    failed on volumes that were never there (2026-09-20) - and nothing in
    the design picked it up, because the sweep only looks at creations that
    are overdue.

    The removal is taken again, because a spec resting here is one whose
    removal did not finish: a successful one leaves `provisioning` or
    `absent`. Removing is safe to repeat, and the desired state says where
    it ends - wanted running, the runner is built again; wanted absent, it
    is finished. Assuming the unit was already gone left a runner that had
    been deregistered still running, unmanaged, when its removal had been
    refused for a degraded worker (2026-09-20).
    """

    def stuck(self, service, desired="running"):
        # Wanted by its fleet, or the pass would rightly withdraw it as
        # surplus before anything else looked at it.
        if desired == "running":
            service.fleets.set_capacity(GH, 1)
        runner_id = service.specs.create(
            provider="github", platform="linux", fleet_id=GH,
            host_id=WORKER, actual_state="removing", desired_state=desired,
            exec_unit_ref="a-unit-that-is-gone")
        return service.specs.get(runner_id)

    def test_one_that_should_run_is_built_again(self, world):
        service, executor, reconciler = world
        spec = self.stuck(service, "running")
        reconciler.pass_once()
        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "provisioning"

    def test_one_that_should_go_is_finished(self, world):
        service, executor, reconciler = world
        spec = self.stuck(service, "absent")
        reconciler.pass_once()
        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "absent"

    def test_its_unit_is_removed_rather_than_assumed_gone(self, world):
        service, executor, reconciler = world
        spec = self.stuck(service, "running")
        reconciler.pass_once()
        assert ("remove", spec["runner_id"]) in [(c[0], c[1]) for c
                                                 in executor.calls]

    def test_what_it_keeps_is_what_a_rebuild_keeps(self, world):
        """Its storage: the runner is coming back under the same id."""
        service, executor, reconciler = world
        spec = self.stuck(service, "running")
        reconciler.pass_once()
        removal = next(c for c in executor.calls if c[0] == "remove")
        assert removal[2]["keep_data"] is True

    def test_one_that_should_go_keeps_nothing(self, world):
        service, executor, reconciler = world
        spec = self.stuck(service, "absent")
        reconciler.pass_once()
        removal = next(c for c in executor.calls if c[0] == "remove")
        assert removal[2]["keep_data"] is False

    def test_one_with_its_operation_still_open_is_left_to_it(self, world):
        """The operation drives it; two things driving one runner is how a
        step gets taken twice."""
        service, executor, reconciler = world
        service.create(GH)
        converge(service, reconciler)
        runner = live(service)[0]
        service.recreate(runner["runner_id"])
        before = service.specs.get(runner["runner_id"])["current_operation"]
        assert before
