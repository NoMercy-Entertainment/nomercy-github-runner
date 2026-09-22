"""The service every runner action goes through.

Two properties are asserted here over and over, because everything else in the
controller rests on them.

**No call does the work.** Each returns an operation id and leaves. A test
below proves it structurally, by giving the service a runtime table whose only
entry would explode if it were ever instantiated, and then planning against it.

**Impossible is refused before anything exists.** A cell nothing can execute, a
fleet nobody can build, a verb the runner's state forbids - each is refused at
the moment it is asked for, with a reason an operator can act on, and with no
rows written.
"""
import pytest

import providers
from control import states
from control.operations import OperationStore
from control.service import Refused, RunnerService, UnknownRunner
from tests import fake_runtime
from store import schema
from store.fleets import FleetStore, fleet_id
from store.specs import SpecStore

GH_LINUX = fleet_id("github", "linux", "x64")
FJ_WINDOWS = fleet_id("forgejo", "windows", "x64")


#: The two cells this build can execute, named the way the controller and the
#: dashboard name them. Passed explicitly because `RUNTIMES` defaults to empty
#: (T-8): nothing runs a runner in this process, so every caller says which
#: runtime reaches the worker.
LINUX_CELLS = {("github", providers.LINUX): "control.agent_runtime:AgentRuntime",
               ("forgejo", providers.LINUX): "control.agent_runtime:AgentRuntime"}


@pytest.fixture
def service(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed()
    return RunnerService(path, runtimes=dict(LINUX_CELLS))


class TestEveryCallReturnsAnOperation:
    def test_plan_does(self, service):
        operation_id = service.plan(GH_LINUX, 2)
        assert service.operations.get(operation_id) is not None

    def test_set_desired_does(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        operation_id = service.set_desired(runner_id, "stopped")
        assert service.operations.get(operation_id)["verb"] == "set_desired"

    def test_act_does(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")
        operation_id = service.act(runner_id, "drain")
        assert service.operations.get(operation_id)["verb"] == "drain"

    def test_the_operation_records_who_asked(self, service):
        operation_id = service.plan(GH_LINUX, 1, requested_by="sub-admin")
        assert service.operations.get(
            operation_id)["requested_by"] == "sub-admin"


class TestNoCallPerformsTheWork:
    def test_planning_never_touches_a_runtime(self, tmp_path):
        """Structural, not incidental: the table's only entry names something
        that raises if it is ever instantiated. Planning still succeeds."""
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed()
        service = RunnerService(
            path, runtimes={("github", providers.LINUX):
                            "tests.fake_runtime:NeverBuilt"})

        operation_id = service.plan(GH_LINUX, 1)

        assert service.operations.get(operation_id)["state"] == "succeeded"

    def test_a_planned_runner_exists_only_on_paper(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        spec = service.specs.get(runner_id)
        assert spec["actual_state"] == "planned"
        assert spec["exec_unit_ref"] is None
        assert spec["registration_id"] is None

    def test_act_does_not_move_the_actual_state(self, service):
        """`actual_state` belongs to the reconciler, which is the only thing
        that has witnessed anything."""
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")

        service.act(runner_id, "stop")

        assert service.specs.get(runner_id)["actual_state"] == "idle"

    def test_act_records_the_intent_instead(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")
        service.act(runner_id, "stop")
        assert service.specs.get(runner_id)["desired_state"] == "stopped"


class TestRuntimeSelectionIsATable:
    def test_a_registered_cell_resolves(self, service):
        from control.agent_runtime import AgentRuntime
        assert service.runtime_for("github", "linux") is AgentRuntime

    def test_selection_is_data_not_a_conditional(self, tmp_path):
        """Replacing the table replaces the answer. The moment this becomes an
        `if platform == 'windows'` the design has lost the property it exists
        for."""
        path = str(tmp_path / "control.db")
        schema.init(path)
        service = RunnerService(
            path, runtimes={("github", providers.WINDOWS):
                            "tests.fake_runtime:Placeholder"})
        resolved = service.runtime_for("github", "windows")
        assert resolved is fake_runtime.Placeholder
        assert not service.can_execute("github", "linux")

    def test_an_unregistered_cell_says_what_this_build_can_run(self, service):
        """A platform that is not built yet is not a KeyError; it is a fact
        about this build, and the message says which."""
        with pytest.raises(Refused) as caught:
            service.runtime_for("github", "windows")
        assert "no runtime is registered" in str(caught.value)
        assert "linux" in str(caught.value)

    def test_every_registered_cell_can_actually_be_imported(self, service):
        """A typo in the table would only surface the first time someone tried
        to build that cell, which is the worst moment to find out."""
        for provider_key, platform in service.runtimes:
            assert service.runtime_for(provider_key, platform)


class TestImpossibleIsRefusedBeforeAnythingExists:
    def test_an_unavailable_fleet_is_refused_with_its_reason(self, service):
        with pytest.raises(Refused) as caught:
            service.plan(FJ_WINDOWS, 1)
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in str(caught.value)

    def test_a_refused_plan_creates_no_specs(self, service):
        """Some of five is worse than none: the reconciler would dutifully
        build them."""
        with pytest.raises(Refused):
            service.plan(FJ_WINDOWS, 3)
        assert service.specs.list() == []

    def test_a_refused_plan_opens_no_operation(self, service):
        with pytest.raises(Refused):
            service.plan(FJ_WINDOWS, 1)
        assert service.operations.list() == []

    def test_a_cell_with_no_runtime_is_refused_at_plan_time(self, service):
        """GitHub supports macOS, so the fleet is available - but this build
        has no runtime for it, and finding that out four steps later would mean
        cleaning up a half-created runner."""
        macos = fleet_id("github", "macos", "x64")
        assert service.fleets.get(macos)["available"] is True
        with pytest.raises(Refused, match="no runtime is registered"):
            service.plan(macos, 1)
        assert service.specs.list() == []

    def test_an_unknown_fleet_is_refused(self, service):
        with pytest.raises(Refused, match="no fleet"):
            service.plan("github-plan9-x64", 1)

    def test_support_is_asked_again_rather_than_trusted(self, service):
        """Seeding may have run long ago, or before someone built the
        artefact. The stored flag is a cache, not the answer."""
        with schema.connect(service.fleets.path) as c:
            c.execute("UPDATE fleets SET available = 1,"
                      " unavailable_reason = NULL WHERE fleet_id = ?",
                      (FJ_WINDOWS,))
        with pytest.raises(Refused, match="cannot be built"):
            service.plan(FJ_WINDOWS, 1)

    def test_a_plan_of_zero_is_a_mistake_not_a_no_op(self, service):
        with pytest.raises(ValueError):
            service.plan(GH_LINUX, 0)


class TestPlanningProducesSpecs:
    def test_one_spec_per_instance(self, service):
        service.plan(GH_LINUX, 3)
        assert len(service.specs.list()) == 3

    def test_each_carries_the_fleets_settings(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        spec = service.specs.get(runner_id)
        assert spec["fleet_id"] == GH_LINUX
        assert spec["provider"] == "github"
        assert spec["platform"] == "linux"
        assert spec["runtime_template"]

    def test_each_wants_to_run(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        assert service.specs.get(runner_id)["desired_state"] == "running"

    def test_every_spec_has_its_own_identity(self, service):
        ids = service.planned_ids(service.plan(GH_LINUX, 5))
        assert len(set(ids)) == 5

    def test_the_operation_result_names_them(self, service):
        operation_id = service.plan(GH_LINUX, 2)
        assert len(service.planned_ids(operation_id)) == 2


class TestVerbsAreCheckedAgainstTheState:
    def a_runner(self, service, state):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state=state)
        return runner_id

    def test_a_legal_verb_is_accepted(self, service):
        assert service.act(self.a_runner(service, "idle"), "drain")

    def test_an_illegal_verb_is_refused_with_a_reason(self, service):
        with pytest.raises(Refused) as caught:
            service.act(self.a_runner(service, "busy"), "stop")
        assert "busy" in str(caught.value)
        assert "stop starts from" in str(caught.value)

    def test_a_busy_runner_cannot_be_stopped(self, service):
        """MIG-9, at the only door that leads to stopping one."""
        with pytest.raises(Refused):
            service.act(self.a_runner(service, "busy"), "stop")

    def test_clear_cache_says_which_states_it_needs(self, service):
        with pytest.raises(Refused) as caught:
            service.act(self.a_runner(service, "busy"), "clear_cache")
        assert "drained" in str(caught.value)

    def test_a_read_is_not_something_to_schedule(self, service):
        with pytest.raises(Refused, match="is a read"):
            service.act(self.a_runner(service, "idle"), "fetch_logs")

    def test_a_fleet_verb_is_not_a_runner_verb(self, service):
        with pytest.raises(Refused, match="acts on a fleet"):
            service.act(self.a_runner(service, "idle"), "scale_up")

    def test_an_unknown_verb_is_an_error(self, service):
        with pytest.raises(ValueError, match="unknown verb"):
            service.act(self.a_runner(service, "idle"), "delete_everything")

    def test_an_unknown_runner_is_an_error(self, service):
        with pytest.raises(UnknownRunner):
            service.act("not-a-runner", "stop")

    def test_every_mutating_verb_is_reachable_from_some_state(self, service):
        """A verb no state allows would be dead code that looks like a
        feature."""
        mutating = states.VERBS - states.READS - states.FLEET_VERBS
        for verb in mutating - {"create"}:
            assert any(states.allows(verb, s) for s in states.STATES), verb


class TestOneOperationAtATime:
    def test_a_second_verb_is_refused_while_one_is_in_flight(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")
        service.act(runner_id, "drain")

        with pytest.raises(Refused, match="already in flight"):
            service.act(runner_id, "stop")

    def test_the_refusal_names_the_operation_to_look_at(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")
        first = service.act(runner_id, "drain")
        with pytest.raises(Refused) as caught:
            service.act(runner_id, "stop")
        assert first in str(caught.value)

    def test_once_it_finishes_another_is_accepted(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")
        first = service.act(runner_id, "drain")
        service.operations.succeed(first)

        service.specs.update(service.specs.get(runner_id)["runner_id"],
                             service.specs.get(runner_id)["spec_version"],
                             actual_state="drained")
        assert service.act(runner_id, "stop")


class TestIdempotency:
    def test_a_repeated_plan_does_not_double_the_fleet(self, service):
        first = service.plan(GH_LINUX, 2, idempotency_key="k1")
        second = service.plan(GH_LINUX, 2, idempotency_key="k1")

        assert first == second
        assert len(service.specs.list()) == 2

    def test_a_repeated_verb_returns_the_first_operation(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle")

        first = service.act(runner_id, "drain", idempotency_key="k2")
        second = service.act(runner_id, "drain", idempotency_key="k2")

        assert first == second

    def test_a_repeated_set_desired_writes_once(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.set_desired(runner_id, "stopped", idempotency_key="k3")
        version = service.specs.get(runner_id)["spec_version"]

        service.set_desired(runner_id, "stopped", idempotency_key="k3")

        assert service.specs.get(runner_id)["spec_version"] == version


class TestSetDesired:
    def test_it_writes_only_the_desired_state(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        before = service.specs.get(runner_id)["actual_state"]

        service.set_desired(runner_id, "absent")

        after = service.specs.get(runner_id)
        assert after["desired_state"] == "absent"
        assert after["actual_state"] == before

    def test_an_invented_state_is_refused(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        with pytest.raises(ValueError, match="not a desired state"):
            service.set_desired(runner_id, "gone-ish")

    def test_an_unknown_runner_is_an_error(self, service):
        with pytest.raises(UnknownRunner):
            service.set_desired("not-a-runner", "stopped")


class TestAnIdempotencyKeyIdentifiesOneRequest:
    """A retry must be the one safe move a caller has.

    The first version of `act` checked "is something already in flight" before
    it checked the key, so a retry of a call whose answer was lost came back
    refused - and the caller was left with no safe move at all, because the
    thing in flight was its own first attempt.
    """

    def a_runner(self, service, state="idle"):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state=state)
        return runner_id

    def test_a_retry_while_the_first_is_still_running_returns_it(self,
                                                                 service):
        runner_id = self.a_runner(service)
        first = service.act(runner_id, "drain", idempotency_key="k")
        assert service.act(runner_id, "drain", idempotency_key="k") == first

    def test_the_retry_opens_no_second_operation(self, service):
        runner_id = self.a_runner(service)
        service.act(runner_id, "drain", idempotency_key="k")
        service.act(runner_id, "drain", idempotency_key="k")
        assert len(service.operations.list(runner_id=runner_id)) == 1

    def test_a_retry_after_the_state_moved_on_still_returns_it(self, service):
        """The runner is draining now, so `drain` is no longer legal from
        here. A retry must still find its operation rather than be told the
        verb is impossible - the verb already happened."""
        runner_id = self.a_runner(service)
        first = service.act(runner_id, "drain", idempotency_key="k")
        spec = service.specs.get(runner_id)
        service.specs.update(runner_id, spec["spec_version"],
                             actual_state="draining")

        assert service.act(runner_id, "drain", idempotency_key="k") == first

    def test_a_key_reused_for_another_verb_is_a_collision(self, service):
        """Returning the other operation would answer a question nobody
        asked."""
        runner_id = self.a_runner(service)
        service.act(runner_id, "drain", idempotency_key="k")
        with pytest.raises(Refused, match="already used for"):
            service.act(runner_id, "stop", idempotency_key="k")

    def test_a_key_reused_on_another_runner_is_a_collision(self, service):
        first = self.a_runner(service)
        second = self.a_runner(service)
        service.act(first, "drain", idempotency_key="k")
        with pytest.raises(Refused, match="already used for"):
            service.act(second, "drain", idempotency_key="k")
