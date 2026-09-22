"""The eighteen verbs, over one service.

Two kinds of assertion. First the exhaustive one the plan asks for: every verb
from every state, accepted where the machine allows it and refused with a
reason everywhere else. It is generated from the state table rather than typed
out, so it cannot fall behind the machine.

Second, the structural one that is the actual point: no verb is implemented
twice for different platforms. It is checked by reading the service's own
source, because "we only have one implementation" is the kind of claim that
stays true right up until someone adds an `if platform == "windows"` to fix
one runner, and then stays believed for a long time afterwards.
"""
import ast
import inspect
import itertools

import pytest

import providers
from control import service as service_module
from control import states
from control.service import Refused, RunnerService
from store import schema
from store.fleets import FleetStore, fleet_id
from tests.fake_runtime import RecordingRuntime

GH_LINUX = fleet_id("github", "linux", "x64")

#: Verbs that move one runner through the machine, checked against its state.
PER_RUNNER = sorted((set(states.VERB_EDGES) | set(states.COMPOSITE)
                     | set(states.GUARDED)) - {"create"})


@pytest.fixture
def service(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed()
    service = RunnerService(path)
    service.inventory.register_worker("linux-1", "hyperv-linux")
    service.inventory.heartbeat("linux-1")
    return service


def runner_in(service, state):
    runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
    service.specs.update(runner_id, 1, actual_state=state)
    return runner_id


class TestEveryVerbHasAMethod:
    def test_all_eighteen_are_methods_on_the_service(self):
        for verb in states.VERBS:
            assert callable(getattr(RunnerService, verb, None)), verb

    def test_create_raises_the_fleets_capacity(self, service):
        """Capacity is the only source of "how many". A runner planned while
        the fleet still wanted zero was withdrawn by the next reconciler pass,
        so creating one means asking the fleet for one more."""
        operation_id = service.create(GH_LINUX, 2)
        assert service.fleets.get(GH_LINUX)["desired_capacity"] == 2
        assert service.operations.get(operation_id)["verb"] == "set_capacity"

    def test_create_does_not_plan_behind_capacitys_back(self, service):
        service.create(GH_LINUX, 2)
        assert service.specs.list() == [], (
            "the reconciler plans; create only states the wish")

    def test_create_of_nothing_is_a_mistake(self, service):
        with pytest.raises(ValueError):
            service.create(GH_LINUX, 0)


class TestEachVerbFromEachState:
    """Generated from the machine, so it cannot fall behind it."""

    CASES = list(itertools.product(PER_RUNNER, sorted(states.STATES)))

    @pytest.mark.parametrize("verb,state", CASES)
    def test_accepted_exactly_where_the_machine_allows_it(self, service,
                                                          verb, state):
        runner_id = runner_in(service, state)
        method = getattr(service, verb)

        if states.allows(verb, state):
            operation_id = method(runner_id)
            assert service.operations.get(operation_id)["verb"] == verb
        else:
            with pytest.raises(Refused) as caught:
                method(runner_id)
            assert state in str(caught.value), (
                "a refusal must say which state it was refused from")

    def test_the_matrix_is_not_empty(self):
        """A parametrisation over an empty product passes vacuously."""
        assert len(self.CASES) > 100


class TestWhatEachVerbAsksFor:
    def test_stop_asks_for_stopped(self, service):
        runner_id = runner_in(service, "idle")
        service.stop(runner_id)
        assert service.specs.get(runner_id)["desired_state"] == "stopped"

    def test_drain_asks_for_drained(self, service):
        runner_id = runner_in(service, "busy")
        service.drain(runner_id)
        assert service.specs.get(runner_id)["desired_state"] == "drained"

    def test_remove_asks_for_absent(self, service):
        runner_id = runner_in(service, "stopped")
        service.remove(runner_id)
        assert service.specs.get(runner_id)["desired_state"] == "absent"

    def test_clear_cache_does_not_change_what_the_runner_should_be(self,
                                                                   service):
        """Clearing a cache does not make a running runner any less meant to
        run."""
        runner_id = runner_in(service, "idle")
        service.clear_cache(runner_id)
        assert service.specs.get(runner_id)["desired_state"] == "running"

    def test_restart_is_recorded_as_itself(self, service):
        """What it decomposes into is written once, in the state module, not
        re-derived here."""
        runner_id = runner_in(service, "idle")
        operation_id = service.restart(runner_id)
        assert service.operations.get(operation_id)["verb"] == "restart"
        assert states.COMPOSITE["restart"] == ("stop", "start")

    def test_recreate_is_recorded_as_itself(self, service):
        runner_id = runner_in(service, "drained")
        operation_id = service.recreate(runner_id)
        assert service.operations.get(operation_id)["verb"] == "recreate"


class TestTheSafetyRulesHoldForEveryVerb:
    def test_nothing_ends_the_work_of_a_busy_runner(self, service):
        """MIG-9, and where it is enforced. A verb that would end the work
        is refused outright."""
        for verb in ("stop", "remove", "deregister", "clear_cache"):
            runner_id = runner_in(service, "busy")
            with pytest.raises(Refused):
                getattr(service, verb)(runner_id)

    def test_what_is_accepted_from_busy_drains_before_anything_else(
            self, service):
        """Restart and recreate are accepted there - rebuilding the runner
        that is working is exactly what an operator means - and the first
        thing done for either is a drain, so the job finishes. The rule is
        about the steps taken, not about the word asked for."""
        from control.reconciler import decide
        for verb in ("restart", "recreate"):
            runner_id = runner_in(service, "busy")
            operation_id = getattr(service, verb)(runner_id)
            spec = service.specs.get(runner_id)
            operation = service.operations.get(operation_id)
            assert decide(spec, operation, ()) == "drain", verb

    def test_a_busy_runner_can_be_drained(self, service):
        assert service.drain(runner_in(service, "busy"))


class TestScaling:
    def test_scale_up_raises_the_target(self, service):
        service.scale_up(GH_LINUX, by=3)
        assert service.fleets.get(GH_LINUX)["desired_capacity"] == 3

    def test_scale_down_lowers_it(self, service):
        service.scale_up(GH_LINUX, by=3)
        service.scale_down(GH_LINUX, by=2)
        assert service.fleets.get(GH_LINUX)["desired_capacity"] == 1

    def test_each_returns_an_operation(self, service):
        operation_id = service.scale_up(GH_LINUX)
        assert service.operations.get(operation_id)["verb"] == "set_capacity"

    def test_scale_down_below_zero_is_refused(self, service):
        with pytest.raises(Refused, match="cannot go"):
            service.scale_down(GH_LINUX)

    def test_an_unbuildable_fleet_cannot_be_scaled_up(self, service):
        with pytest.raises(Refused):
            service.scale_up(fleet_id("forgejo", "windows", "x64"))

    def test_scaling_by_zero_is_a_mistake(self, service):
        with pytest.raises(ValueError):
            service.scale_up(GH_LINUX, by=0)

    def test_scaling_down_touches_no_runner(self, service):
        """It lowers a number. Which runner goes, and only once it is idle, is
        the reconciler's decision - so a scale-down never aborts a job."""
        runner_id = runner_in(service, "busy")
        service.scale_up(GH_LINUX, by=1)
        service.scale_down(GH_LINUX, by=1)
        assert service.specs.get(runner_id)["actual_state"] == "busy"
        assert service.specs.get(runner_id)["desired_state"] == "running"


class TestReads:
    @pytest.fixture
    def reading(self, tmp_path):
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed()
        RecordingRuntime.calls = []
        return RunnerService(path, runtimes={
            ("github", providers.LINUX):
                "tests.fake_runtime:RecordingRuntime"})

    def a_live_runner(self, service):
        runner_id = service.planned_ids(service.plan(GH_LINUX, 1))[0]
        service.specs.update(runner_id, 1, actual_state="idle",
                             exec_unit_ref="github-runner-4")
        return runner_id

    def test_status_shows_intent_and_observation_side_by_side(self, reading):
        """They can disagree, and the disagreement is the interesting part."""
        status = reading.fetch_status(self.a_live_runner(reading))
        assert status["desired_state"] == "running"
        assert status["actual_state"] == "idle"
        assert status["observed"] == {"running": True}

    def test_logs_come_from_the_runtime(self, reading):
        assert reading.fetch_logs(self.a_live_runner(reading),
                                  since_seconds=60) == "a log line"
        assert ("logs", "github-runner-4", 60) in RecordingRuntime.calls

    def test_resources_come_from_the_runtime(self, reading):
        assert reading.inspect_resources(
            self.a_live_runner(reading)) == {"cpu_percent": 3.0}

    def test_the_unit_is_addressed_by_its_kind(self, reading):
        """A guest is never called a container, even in a handle."""
        reading.fetch_status(self.a_live_runner(reading))
        assert RecordingRuntime.calls[0][2] == "linux-container"

    def test_a_runner_with_no_unit_yet_reads_as_nothing(self, reading):
        """Planned runners exist only on paper; asking the runtime about them
        would be asking about something that is not there."""
        runner_id = reading.planned_ids(reading.plan(GH_LINUX, 1))[0]
        assert reading.fetch_logs(runner_id) == ""
        assert reading.inspect_resources(runner_id) is None
        assert reading.fetch_status(runner_id)["observed"] is None
        assert RecordingRuntime.calls == []

    def test_a_read_opens_no_operation(self, reading):
        runner_id = self.a_live_runner(reading)
        before = len(reading.operations.list())
        reading.fetch_status(runner_id)
        reading.fetch_logs(runner_id)
        reading.inspect_resources(runner_id)
        assert len(reading.operations.list()) == before


class TestNoVerbIsImplementedTwice:
    """T-0304's definition of done, checked against the source.

    "We only have one implementation" stays true until someone adds a platform
    branch to fix one runner, and then stays believed long after it stopped
    being true. Reading the source is the only check that cannot be argued
    with.
    """

    def source_tree(self):
        return ast.parse(inspect.getsource(service_module))

    def verb_methods(self):
        cls = next(n for n in self.source_tree().body
                   if isinstance(n, ast.ClassDef) and n.name == "RunnerService")
        return {n.name: n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name in states.VERBS}

    def test_no_verb_mentions_a_platform(self):
        platforms = set(providers.PLATFORMS) | {"LINUX", "WINDOWS", "MACOS"}
        for name, node in self.verb_methods().items():
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(node)
                     if isinstance(n, ast.Attribute)}
            strings = {n.value for n in ast.walk(node)
                       if isinstance(n, ast.Constant)
                       and isinstance(n.value, str)}
            clash = platforms & (names | attrs | strings)
            assert clash == set(), f"{name} branches on {clash}"

    def test_every_mutating_runner_verb_is_one_call_into_act(self):
        """A method that is one line into `act` has nowhere for a per-platform
        variant to live."""
        for name in PER_RUNNER:
            node = self.verb_methods()[name]
            body = [n for n in node.body
                    if not (isinstance(n, ast.Expr)
                            and isinstance(n.value, ast.Constant))]
            assert len(body) == 1, f"{name} has {len(body)} statements"
            call = body[0].value
            assert isinstance(call, ast.Call)
            assert call.func.attr == "act", f"{name} does not go through act"

    def test_the_whole_service_has_no_platform_conditional(self):
        """Wider than the verbs: nowhere in the module may an `if` compare
        against a platform. The tables are where platforms are allowed to
        appear, and they are dictionaries, not branches."""
        for node in ast.walk(self.source_tree()):
            if not isinstance(node, ast.If):
                continue
            for part in ast.walk(node.test):
                if isinstance(part, ast.Constant) and part.value in \
                        providers.PLATFORMS:
                    pytest.fail(f"a branch on {part.value!r} at line "
                                f"{node.lineno}")
                if isinstance(part, ast.Attribute) and part.attr in \
                        ("LINUX", "WINDOWS", "MACOS"):
                    pytest.fail(f"a branch on {part.attr} at line "
                                f"{node.lineno}")

    def test_only_the_reads_construct_a_runtime(self):
        """The mutating path records intent and never touches a runtime. The
        three reads are the stated exception, and nothing else may join them
        without someone noticing."""
        reads = {"fetch_status", "fetch_logs", "inspect_resources"}
        for name, node in self.verb_methods().items():
            calls = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)}
            touches = "_runtime_and_ref" in calls
            assert touches == (name in reads), name
