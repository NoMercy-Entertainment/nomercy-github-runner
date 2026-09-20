"""One provisioning flow: its order, its compensations, and its secret.

Four things the plan asks for, and a class for each.

The step order is asserted, because "create before mint" is not an accident:
a token minted before the unit exists expires while the unit is still being
built, and one minted for a unit that then fails to build is wasted.

A failure at every step runs exactly the compensations of design 12.5, in
reverse. Generated over all five steps rather than written out, so a step
added later without a compensation fails here.

The registration token never reaches a log, an operation, a spec or the audit
table. Checked with a sentinel value that is searched for everywhere after a
run in which the agent deliberately fails with the token in its error.

And one code path serves all six cells: the same sequence, recorded, for every
provider and platform - plus a reading of the source that fails on any branch
naming a platform or a forge.
"""
import ast
import inspect
import sqlite3

import pytest

import providers as P
from control import inventory as inv
from control import provision
from control.provision import (COMPENSATIONS, STEPS,
                               ProvisioningFlow, StepFailed)
from control.reconciler import Reconciler
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore, fleet_id
from tests.fake_platform import FakeAgent, FakeForges
from tests.fake_runtime import UnitRuntime

SENTINEL = "tok-SENTINEL-4c1e77d9ab"

#: A runtime for every cell, so the one flow can be run against all six.
ALL_CELLS = {(p.key, platform): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for platform in P.PLATFORMS}

#: Makes the two self-built Forgejo cells buildable.
BUILT = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner-windows.exe",
         "FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin",
         "FORGEJO_INSTANCE_URL": "https://git.example",
         "FORGEJO_RUNNER_LABELS": "docker:docker://node:20",
         # Windows and macOS read their own: they must not inherit Linux's
         # docker:// labels, having no engine to run them (T-1001).
         "FORGEJO_RUNNER_LABELS_WINDOWS": "windows:host",
         "FORGEJO_RUNNER_LABELS_MACOS": "macos:host",
         "GH_TOKEN": "x", "GITHUB_ORG": "NoMercy-Entertainment"}


class Minter:
    """A forge client that mints the sentinel token."""

    org = "NoMercy-Entertainment"

    def registration_token(self):
        return SENTINEL


@pytest.fixture
def platform(tmp_path, monkeypatch):
    UnitRuntime.reset()
    for provider in P.ALL:
        monkeypatch.setattr(provider, "forge_client", lambda env: Minter())
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed(BUILT)
    service = RunnerService(path, runtimes=dict(ALL_CELLS))
    # Three workers, because two of them are Linux and only one of those
    # drives the macOS appliance (T-0802). What a worker declares it drives
    # is what a runner is placed on.
    for host, kind, drives in (
            ("linux-1", inv.HYPERV_LINUX, "linux-container"),
            ("windows-1", inv.HYPERV_WINDOWS, "windows-process"),
            ("appliance-1", inv.HYPERV_LINUX, "macos-appliance")):
        service.inventory.register_worker(host, kind,
                                          capabilities={"kind": drives})
        service.inventory.heartbeat(host)
    agent = FakeAgent()
    forges = FakeForges(agent)
    flow = ProvisioningFlow(service, agent, forges, env=BUILT,
                            verify_timeout=10, verify_interval=5,
                            sleep=lambda s: None)
    return service, flow, agent, forges


def a_planned(service, provider="github", platform="linux"):
    fid = fleet_id(provider, platform, "x64")
    return service.specs.get(service.planned_ids(service.plan(fid, 1,
                                                              env=BUILT))[0])


def provisioned(service, flow, spec):
    result = flow.provision(spec)
    service.specs.update(spec["runner_id"], spec["spec_version"],
                         actual_state="provisioned", **result)
    return service.specs.get(spec["runner_id"])


class TestTheStepOrder:
    def test_the_five_steps_run_in_the_designs_order(self, platform):
        service, flow, agent, forges = platform
        spec = provisioned(service, flow, a_planned(service))
        flow.register(spec)
        assert flow.trail == list(STEPS)

    def test_the_unit_exists_before_the_token_is_minted(self, platform):
        """A token minted first would expire while the unit was built, and one
        minted for a unit that failed to build would be wasted."""
        assert STEPS.index("create_unit") < STEPS.index("mint_token")

    def test_ready_needs_the_agent_and_the_forge(self, platform):
        """Either alone has already been wrong on this fleet."""
        service, flow, agent, forges = platform
        forges.online = False
        spec = provisioned(service, flow, a_planned(service))
        with pytest.raises(StepFailed) as caught:
            flow.register(spec)
        assert caught.value.step == "verify_online"

    def test_it_waits_for_the_runner_to_come_up(self, platform):
        service, flow, agent, forges = platform
        agent.ready_after = 1
        spec = provisioned(service, flow, a_planned(service))
        result = flow.register(spec)
        assert result["registration_id"]


class TestEveryFailureRunsExactlyItsCompensations:
    """Design 12.5, generated over every step."""

    def fail_at(self, platform, step, monkeypatch):
        service, flow, agent, forges = platform
        spec = a_planned(service)
        if step == "place":
            with schema.connect(service.specs.path) as c:
                c.execute("UPDATE workers SET last_seen_at = NULL")
        if step == "create_unit":
            UnitRuntime.fail_on = {"create"}
        if step in ("place", "create_unit"):
            with pytest.raises(StepFailed) as caught:
                flow.provision(spec)
            return flow, caught.value

        spec = provisioned(service, flow, spec)
        flow.trail.clear()
        if step == "mint_token":
            for provider in P.ALL:
                monkeypatch.setattr(provider, "forge_client",
                                    lambda env: None)
        if step == "register":
            agent.fail_on = {"register"}
        if step == "verify_online":
            forges.online = False
        with pytest.raises(StepFailed) as caught:
            flow.register(spec)
        return flow, caught.value

    @pytest.mark.parametrize("step", STEPS)
    def test_the_compensations_run_in_reverse(self, platform, step,
                                              monkeypatch):
        flow, failure = self.fail_at(platform, step, monkeypatch)

        undone = [t[len("undo:"):] for t in flow.trail
                  if t.startswith("undo:")]
        assert failure.step == step
        assert tuple(undone) == COMPENSATIONS[step]

    def test_every_step_has_a_compensation_entry(self):
        """A step added without one would have no answer to "and if it
        fails?"."""
        assert set(COMPENSATIONS) == set(STEPS)

    def test_a_failed_register_leaves_no_unit_behind(self, platform):
        service, flow, agent, forges = platform
        spec = provisioned(service, flow, a_planned(service))
        agent.fail_on = {"register"}
        with pytest.raises(StepFailed) as caught:
            flow.register(spec)
        assert UnitRuntime.units == {}
        assert caught.value.removed_unit

    def test_a_failed_verify_deregisters_before_removing(self, platform):
        """The forge record first: removing the unit first would leave a
        registration pointing at nothing."""
        service, flow, agent, forges = platform
        forges.online = False
        spec = provisioned(service, flow, a_planned(service))
        with pytest.raises(StepFailed):
            flow.register(spec)
        verbs = [c[0] for c in agent.calls]
        assert "deregister" in verbs
        assert [e[0] for e in UnitRuntime.log][-1] == "remove"

    def test_forgejo_is_deregistered_through_the_api(self, platform):
        """forgejo-runner has no unregister subcommand; only the API can
        delete the record."""
        service, flow, agent, forges = platform
        forges.online = False
        spec = provisioned(service, flow, a_planned(service, "forgejo"))
        with pytest.raises(StepFailed):
            flow.register(spec)
        assert forges.deleted and forges.deleted[0][0] == "forgejo"
        assert "deregister" not in [c[0] for c in agent.calls]

    def test_a_compensation_that_fails_is_reported_not_swallowed(self,
                                                                 platform):
        """It is exactly where something was left behind."""
        service, flow, agent, forges = platform
        spec = provisioned(service, flow, a_planned(service))
        agent.fail_on = {"register"}
        UnitRuntime.fail_on = {"remove"}
        with pytest.raises(StepFailed) as caught:
            flow.register(spec)
        assert "could not undo" in str(caught.value)
        assert "remove_unit" in str(caught.value)

    def test_nothing_is_undone_when_placement_fails(self, platform):
        service, flow, agent, forges = platform
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        with pytest.raises(StepFailed) as caught:
            flow.provision(a_planned(service))
        assert caught.value.compensated == ()
        assert UnitRuntime.log == []


class TestPlacement:
    def test_a_linux_runner_goes_to_a_linux_worker(self, platform):
        service, flow, agent, forges = platform
        assert flow.provision(a_planned(service))["host_id"] == "linux-1"

    def test_a_windows_runner_goes_to_a_windows_worker(self, platform):
        service, flow, agent, forges = platform
        result = flow.provision(a_planned(service, "github", "windows"))
        assert result["host_id"] == "windows-1"

    def test_macos_is_an_appliance_on_the_linux_worker_that_has_one(
            self, platform):
        """No apple-host kind: the appliance is an execution unit of a Linux
        worker (16.4). Which Linux worker is not a matter of kind - both are
        `hyperv-linux` - but of what each declares it drives, the same way
        placement reads what each declares it can hold."""
        service, flow, agent, forges = platform
        result = flow.provision(a_planned(service, "github", "macos"))
        assert result["host_id"] == "appliance-1"

    def test_a_linux_runner_is_never_placed_on_an_appliance_host(
            self, platform):
        """It would find no engine there."""
        service, flow, agent, forges = platform
        with schema.connect(service.specs.path) as c:
            c.execute("DELETE FROM workers WHERE host_id = 'linux-1'")
        with pytest.raises(Exception, match="macos-appliance|no worker"):
            flow.provision(a_planned(service, "github", "linux"))

    def test_the_least_loaded_worker_is_chosen(self, platform):
        service, flow, agent, forges = platform
        service.inventory.register_worker("linux-2", inv.HYPERV_LINUX)
        service.inventory.heartbeat("linux-2")
        first = a_planned(service)
        service.specs.update(first["runner_id"], first["spec_version"],
                             host_id="linux-1")
        assert flow.provision(a_planned(service))["host_id"] == "linux-2"

    def test_no_healthy_worker_is_a_named_failure(self, platform):
        service, flow, agent, forges = platform
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        with pytest.raises(StepFailed, match="no healthy hyperv-linux"):
            flow.provision(a_planned(service))

    def test_a_placed_runner_keeps_its_worker(self, platform):
        """Its storage is there."""
        service, flow, agent, forges = platform
        spec = a_planned(service)
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             host_id="linux-1")
        service.inventory.register_worker("linux-2", inv.HYPERV_LINUX)
        service.inventory.heartbeat("linux-2")
        assert flow.provision(service.specs.get(
            spec["runner_id"]))["host_id"] == "linux-1"

    def test_the_unit_is_created_with_its_own_storage_names(self, platform):
        service, flow, agent, forges = platform
        spec = a_planned(service)
        flow.provision(spec)
        from store import storage as store_storage
        handle = store_storage.unit_name(spec["runner_id"])
        storage = UnitRuntime.units[handle]["storage"]
        assert storage["work"] == f"rnr-{spec['runner_id']}-work"


class TestTheTokenGoesNowhere:
    """T-0305's security line, checked with a sentinel value."""

    def run_a_failing_registration(self, platform):
        service, flow, agent, forges = platform
        agent.fail_on = {"register"}
        agent.raise_with = f"refused registration with token {SENTINEL}"
        reconciler = Reconciler(service, flow)
        service.scale_up(fleet_id("github", "linux", "x64"))
        reconciler.pass_once()          # provision
        reconciler.pass_once()          # register: fails, with the token
        return service

    def everything_stored(self, path):
        with sqlite3.connect(path) as c:
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                for row in c.execute(f"SELECT * FROM {table}"):
                    yield table, " ".join(str(v) for v in row)

    def test_it_is_in_no_table(self, platform):
        service = self.run_a_failing_registration(platform)
        for table, row in self.everything_stored(service.specs.path):
            assert SENTINEL not in row, f"the token reached {table}"

    def test_the_failure_is_still_recorded_just_without_it(self, platform):
        """Redaction must not become silence."""
        service = self.run_a_failing_registration(platform)
        spec = service.specs.list()[0]
        assert "refused registration" in spec["last_error"]
        assert "[redacted]" in spec["last_error"]

    def test_it_is_in_no_output(self, platform, capsys):
        self.run_a_failing_registration(platform)
        captured = capsys.readouterr()
        assert SENTINEL not in captured.out
        assert SENTINEL not in captured.err

    def test_the_agent_did_receive_it(self, platform):
        """The one place it is meant to go."""
        service, flow, agent, forges = platform
        spec = provisioned(service, flow, a_planned(service))
        flow.register(spec)
        assert ("register", "linux-1", spec["exec_unit_ref"], True) in \
            agent.calls

    def test_a_successful_registration_stores_ids_not_the_token(self,
                                                                 platform):
        service, flow, agent, forges = platform
        spec = provisioned(service, flow, a_planned(service))
        result = flow.register(spec)
        assert set(result) == {"registration_id", "registration_uuid"}
        assert SENTINEL not in str(result)


class TestOneCodePathForAllSixCells:
    """T-0305's definition of done."""

    CELLS = [(p.key, platform) for p in P.ALL for platform in P.PLATFORMS]

    @pytest.mark.parametrize("provider,platform_name", CELLS)
    def test_each_cell_runs_the_identical_sequence(self, platform, provider,
                                                   platform_name):
        service, flow, agent, forges = platform
        spec = a_planned(service, provider, platform_name)
        spec = provisioned(service, flow, spec)
        flow.register(spec)
        assert flow.trail == list(STEPS)

    def test_there_are_six(self):
        assert len(self.CELLS) == 6

    def test_the_flow_never_branches_on_a_platform_or_a_forge(self):
        """The tables at the top of the module are where platforms may appear;
        they are dictionaries, not branches."""
        tree = ast.parse(inspect.getsource(provision))
        names = set(P.PLATFORMS) | {p.key for p in P.ALL}
        attrs = {"LINUX", "WINDOWS", "MACOS", "GITHUB", "FORGEJO"}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.Compare)):
                continue
            test = node.test if isinstance(node, (ast.If, ast.IfExp)) \
                else node
            for part in ast.walk(test):
                if isinstance(part, ast.Constant) and part.value in names:
                    pytest.fail(f"branch on {part.value!r}, line "
                                f"{node.lineno}")
                if isinstance(part, ast.Attribute) and part.attr in attrs:
                    pytest.fail(f"branch on {part.attr}, line {node.lineno}")


class TestDrivenByTheReconciler:
    def test_a_fleet_comes_up_through_the_real_flow(self, platform):
        service, flow, agent, forges = platform
        reconciler = Reconciler(service, flow)
        service.scale_up(fleet_id("github", "linux", "x64"), by=2)
        for _ in range(5):
            service.inventory.heartbeat("linux-1")
            reconciler.pass_once()
        runners = service.specs.list(
            fleet_id=fleet_id("github", "linux", "x64"))
        assert len(runners) == 2
        assert all(r["actual_state"] == "idle" for r in runners)
        assert all(r["registration_id"] for r in runners)

    def test_a_failed_registration_leaves_a_clean_failed_runner(self,
                                                               platform):
        """Failed, with the references to what the compensations removed
        cleared - so a later remove does not go looking for a unit that is
        gone."""
        service, flow, agent, forges = platform
        agent.fail_on = {"register"}
        reconciler = Reconciler(service, flow)
        service.scale_up(fleet_id("github", "linux", "x64"))
        reconciler.pass_once()
        reconciler.pass_once()
        spec = service.specs.list()[0]
        assert spec["actual_state"] == "failed"
        assert spec["exec_unit_ref"] is None
        assert UnitRuntime.units == {}

    def test_a_job_seen_at_the_forge_is_recorded(self, platform):
        service, flow, agent, forges = platform
        reconciler = Reconciler(service, flow)
        service.scale_up(fleet_id("github", "linux", "x64"))
        for _ in range(3):
            reconciler.pass_once()
        spec = service.specs.list()[0]
        forges.busy.add(spec["registration_id"])
        reconciler.pass_once()
        assert service.specs.get(spec["runner_id"])["actual_state"] == "busy"

    def test_a_drained_runner_is_seen_as_drained_once_the_forge_is_idle(
            self, platform):
        service, flow, agent, forges = platform
        reconciler = Reconciler(service, flow)
        service.scale_up(fleet_id("github", "linux", "x64"))
        for _ in range(3):
            reconciler.pass_once()
        spec = service.specs.list()[0]
        forges.busy.add(spec["registration_id"])
        reconciler.pass_once()                   # busy
        service.drain(spec["runner_id"])
        reconciler.pass_once()                   # draining
        forges.busy.clear()
        reconciler.pass_once()                   # job done: drained
        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "drained"


class TestRemovingWhatAFailedCreateLeft:
    """The first Windows unit failed inside `create`, and its compensation
    failed too, so the spec recorded no handle. A removal that asked for
    nothing then left the runner's directory tree on the worker for good -
    seen on 2026-09-19. Every unit's name comes from its runner_id, so it
    can always be asked for, and a runtime with nothing by that name treats
    the removal as done."""

    def test_it_is_asked_for_by_the_name_that_runner_s_unit_has(self,
                                                                platform):
        from store import storage
        service, flow, _, _ = platform
        spec = a_planned(service)
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             host_id="linux-1")
        spec = service.specs.get(spec["runner_id"])
        assert not spec["exec_unit_ref"], "nothing was recorded"

        flow.remove(spec)

        removed = [c for c in UnitRuntime.log if c[0] == "remove"]
        assert removed and removed[-1][1] == storage.unit_name(
            spec["runner_id"])

    def test_a_runner_that_was_never_placed_asks_for_nothing(self, platform):
        service, flow, _, _ = platform
        spec = a_planned(service)
        before = len(UnitRuntime.log)

        flow.remove(spec)               # no worker, so nothing anywhere

        assert len(UnitRuntime.log) == before


class TestTheForgeIsAskedOncePerPass:
    """Every runner's observation needs the forge's runner list, and asking
    per runner is how a token's hour is spent: fifteen runners on a pass
    every fifteen seconds is 3600 calls an hour, against a limit of 5000
    shared with everything else. GitHub answered 403 - rate limit exceeded -
    and every card on the page went `unknown` (2026-09-20).

    So one pass asks once. The window is shorter than a pass, and a failure
    is never remembered: 17.2's "never the last list that did arrive" is
    about failures, not about two reads a second apart.
    """

    def test_several_runners_in_one_pass_cost_one_call(self, platform):
        service, flow, agent, forges = platform
        asked = []
        real = forges.records
        forges.records = lambda p: asked.append(p) or real(p)
        specs = [a_planned(service) for _ in range(3)]
        for spec in specs:
            flow._records(P.GITHUB)
        assert len(asked) == 1

    def test_the_next_pass_asks_again(self, platform, monkeypatch):
        service, flow, agent, forges = platform
        asked = []
        real = forges.records
        forges.records = lambda p: asked.append(p) or real(p)
        flow._records(P.GITHUB)
        monkeypatch.setattr(flow, "_forge_read_at",
                            {k: v - 999 for k, v in flow._forge_read_at.items()})
        flow._records(P.GITHUB)
        assert len(asked) == 2

    def test_a_forge_that_could_not_be_asked_is_not_remembered(self, platform):
        service, flow, agent, forges = platform
        forges.records = lambda p: None
        assert flow._records(P.GITHUB) is None
        answers = []
        forges.records = lambda p: answers.append(p) or []
        assert flow._records(P.GITHUB) == []
        assert answers, "a failure must not be cached as an answer"

    def test_each_forge_is_cached_on_its_own(self, platform):
        service, flow, agent, forges = platform
        asked = []
        forges.records = lambda p: asked.append(p.key) or []
        flow._records(P.GITHUB)
        flow._records(P.FORGEJO)
        assert asked == ["github", "forgejo"]


    def test_a_loop_waiting_for_the_forge_reads_it_afresh(self, platform):
        """Verification waits for the forge to change its mind. Serving it
        a remembered answer would make it wait for something it could never
        see - a registration that timed out while the runner was online."""
        service, flow, agent, forges = platform
        asked = []
        real = forges.records
        forges.records = lambda p: asked.append(p) or real(p)
        flow._records(P.GITHUB)
        flow._records(P.GITHUB, fresh=True)
        assert len(asked) == 2
