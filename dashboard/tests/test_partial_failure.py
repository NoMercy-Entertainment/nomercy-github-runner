"""No half instances: the three places a crash can land during creation.

An ordinary failure is handled by the flow itself - it compensates before it
returns (T-0305). A crash is different: the controller process dies, nothing
compensates, and whatever was half made stays half made. The design's answer
(12.5) is to record every step's intent before taking it, name everything from
the runner_id so it can be found again, and sweep anything left mid-creation
past its deadline with the same compensations.

A crash is simulated with an exception that is not an Exception, so it passes
through every handler in the flow and the reconciler exactly as a dying process
would: no compensation runs and no state is written after it.

Each crash point is driven to one of the two outcomes T-0308 allows, and the
test checks both halves of the second one: no instance left on the worker, and
no record left at the forge.

  1. create, before record    the unit exists; the spec does not say so
  2. record, before register  the unit is recorded; nothing registered
  3. register, before confirm registered and recorded; not yet online
"""
import pytest

import providers as P
from control import inventory as inv
from control.provision import ProvisioningFlow
from control.reconciler import Reconciler
from control.service import RunnerService
from store import schema, storage
from store.fleets import FleetStore, fleet_id
from tests.fake_platform import Crash, FakeAgent, FakeForges
from tests.fake_runtime import UnitRuntime

GH = fleet_id("github", "linux", "x64")
FJ = fleet_id("forgejo", "linux", "x64")
ENV = {"FORGEJO_INSTANCE_URL": "https://git.example",
       "FORGEJO_RUNNER_LABELS": "docker:docker://node:20"}


class Minter:
    org = "NoMercy-Entertainment"
    crash = False

    def registration_token(self):
        if Minter.crash:
            Minter.crash = False
            raise Crash("the controller died minting a token")
        return "tok-0123456789abcdef"


@pytest.fixture
def world(tmp_path, monkeypatch):
    UnitRuntime.reset()
    Minter.crash = False
    for provider in P.ALL:
        monkeypatch.setattr(provider, "forge_client", lambda env: Minter())
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed(ENV)
    service = RunnerService(path, runtimes={
        ("github", "linux"): "tests.fake_runtime:UnitRuntime",
        ("forgejo", "linux"): "tests.fake_runtime:UnitRuntime"})
    service.inventory.register_worker("linux-1", inv.HYPERV_LINUX)
    service.inventory.heartbeat("linux-1")
    agent = FakeAgent()
    forges = FakeForges(agent)
    flow = ProvisioningFlow(service, agent, forges, env=ENV,
                            verify_timeout=0, sleep=lambda s: None)
    return service, flow, agent, forges, Reconciler(service, flow)


def crashing_pass(reconciler):
    """One pass that dies part-way, as the process would."""
    with pytest.raises(Crash):
        reconciler.pass_once()
    reconciler._release()         # a dead process's lease expires; skip the wait


def passes(service, reconciler, n=6):
    for _ in range(n):
        service.inventory.heartbeat("linux-1")
        reconciler.pass_once()


def expire_deadlines(service):
    """Put every open operation past its deadline, as time would."""
    with schema.connect(service.specs.path) as c:
        c.execute("UPDATE operations SET deadline_at = '2000-01-01T00:00:00Z'"
                  " WHERE state IN ('pending', 'running')")


def the_runner(service, fid=GH):
    specs = service.specs.list(fleet_id=fid, include_deleted=True)
    assert len(specs) == 1, "the fleet should hold exactly one runner"
    return service.specs.get(specs[0]["runner_id"])


def healthy(service, forges, spec):
    """A serving runner: idle, its one unit present, its one record live."""
    return (spec["actual_state"] == "idle"
            and list(UnitRuntime.units) == [storage.unit_name(
                spec["runner_id"])]
            and len(forges.live_handles()) == 1)


def nothing_left(forges):
    """No unit on any worker and no live record at either forge."""
    return UnitRuntime.units == {} and forges.live_handles() == []


class TestCrashAfterCreateBeforeRecord:
    """The unit exists; the spec still says provisioning, with no handle."""

    def crash(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        spec = the_runner(service)
        assert spec["actual_state"] == "provisioning"
        assert spec["exec_unit_ref"] is None
        assert len(UnitRuntime.units) == 1, "the crash left a unit behind"
        return spec

    def test_the_worker_was_recorded_before_the_unit_was_built(self, world):
        """12.5's first row. Without it the retry could place the runner
        elsewhere and orphan the unit on the first worker."""
        spec = self.crash(world)
        assert spec["host_id"] == "linux-1"

    def test_the_next_pass_adopts_the_unit_and_finishes(self, world):
        """Converges to healthy, with one unit - not a second one beside the
        first, and not a failure on a name collision with itself."""
        service, flow, agent, forges, reconciler = world
        spec = self.crash(world)

        passes(service, reconciler)

        spec = service.specs.get(spec["runner_id"])
        assert healthy(service, forges, spec)
        creates = [e for e in UnitRuntime.log if e[0] == "create"]
        assert len(creates) == 1, "the unit was adopted, not made twice"

    def test_past_its_deadline_it_is_swept_to_nothing(self, world):
        """If nothing finishes it in time, the sweep removes the unit by the
        name derived from the runner_id - the handle was never recorded."""
        service, flow, agent, forges, reconciler = world
        spec = self.crash(world)
        expire_deadlines(service)

        reconciler.pass_once()

        spec = service.specs.get(spec["runner_id"])
        assert spec["actual_state"] == "failed"
        assert nothing_left(forges)
        assert "interrupted while provisioning" in spec["last_error"]


class TestCrashAfterRecordBeforeRegister:
    """The unit is recorded. Nothing has been registered."""

    def test_recorded_as_provisioned_it_simply_registers_next(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        reconciler.pass_once()                     # provisioned, recorded
        assert the_runner(service)["actual_state"] == "provisioned"

        passes(service, reconciler)                # the crash changed nothing

        assert healthy(service, forges, the_runner(service))

    def test_crashed_inside_registering_it_is_swept_to_nothing(self, world):
        """Moved to registering, then died before any forge was contacted. It
        is not re-driven, because from here it cannot be told apart from a
        registration whose reply was lost - and registering twice would leave
        a record nobody owns. Past its deadline it is swept."""
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        reconciler.pass_once()
        Minter.crash = True
        crashing_pass(reconciler)
        spec = the_runner(service)
        assert spec["actual_state"] == "registering"
        assert spec["registration_id"] is None

        passes(service, reconciler, 2)             # observed, not re-driven
        assert [c for c in agent.calls if c[0] == "register"] == []

        expire_deadlines(service)
        reconciler.pass_once()

        spec = service.specs.get(spec["runner_id"])
        assert spec["actual_state"] == "failed"
        assert nothing_left(forges)
        assert spec["exec_unit_ref"] is None


class TestCrashAfterRegisterBeforeConfirm:
    """Registered, and the ids recorded the moment the agent confirmed them.
    The controller died waiting for the runner to come online."""

    def crash(self, world, fid=GH):
        service, flow, agent, forges, reconciler = world
        service.scale_up(fid)
        reconciler.pass_once()
        agent.crash_on_ready = True
        crashing_pass(reconciler)
        spec = the_runner(service, fid)
        assert spec["actual_state"] == "registering"
        return spec

    def test_the_ids_were_recorded_before_the_wait(self, world):
        """12.5: registration_id written with the agent's confirmation. This
        is what makes the forge record findable after the crash."""
        spec = self.crash(world)
        assert spec["registration_id"]

    def test_if_it_came_up_it_is_seen_and_kept(self, world):
        """The runner came online while the controller was down. Observing
        that is the `registering -> idle` edge: healthy, nothing redone."""
        service, flow, agent, forges, reconciler = world
        spec = self.crash(world)

        passes(service, reconciler)

        spec = service.specs.get(spec["runner_id"])
        assert healthy(service, forges, spec)
        assert len([c for c in agent.calls if c[0] == "register"]) == 1

    def test_if_it_never_came_up_it_is_swept_record_and_all(self, world):
        """No instance and no forge record: the record is deleted by the id
        that was written before the wait."""
        service, flow, agent, forges, reconciler = world
        forges.online = False
        spec = self.crash(world)
        passes(service, reconciler, 2)
        expire_deadlines(service)

        reconciler.pass_once()

        spec = service.specs.get(spec["runner_id"])
        assert spec["actual_state"] == "failed"
        assert nothing_left(forges)
        assert spec["registration_id"] is None

    def test_forgejo_is_swept_through_its_api(self, world):
        """forgejo-runner cannot unregister itself; only the API delete removes
        the record, so the sweep must use it."""
        service, flow, agent, forges, reconciler = world
        forges.online = False
        spec = self.crash(world, FJ)
        expire_deadlines(service)

        reconciler.pass_once()

        assert forges.deleted and forges.deleted[0][0] == "forgejo"
        assert nothing_left(forges)
        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "failed"


class TestTheSweepItself:
    def test_it_does_not_touch_a_runner_that_is_merely_slow(self, world):
        """Before the deadline, a runner mid-creation is left to finish."""
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)

        report = reconciler.pass_once()      # deadline not passed

        assert not any(a[0] == "swept" for a in report.actions)

    def test_it_never_sweeps_a_draining_runner(self, world):
        """A drain can wait hours on a job. Its deadline passing is not a
        reason to take the runner down - MIG-9."""
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        spec = the_runner(service)
        forges.busy.add(spec["registration_id"])   # a long job, still going
        reconciler.pass_once()                     # observed: busy
        service.drain(spec["runner_id"])
        reconciler.pass_once()                     # draining
        expire_deadlines(service)

        report = reconciler.pass_once()

        assert not any(a[0] == "swept" for a in report.actions)
        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "draining"

    def test_it_holds_for_a_worker_that_is_not_answering(self, world):
        """The sweep is destructive, so the same gate applies: unreachable and
        absent look the same from here."""
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        expire_deadlines(service)
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")

        report = reconciler.pass_once()

        assert any("sweep held" in r for _, r in report.held)
        assert len(UnitRuntime.units) == 1, "nothing was removed"

    def test_a_swept_runner_still_counts_against_its_fleet(self, world):
        """Freeing its slot while its failure is unexplained would let the
        fleet grow past what was asked; a human repair or remove decides."""
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        expire_deadlines(service)
        reconciler.pass_once()

        passes(service, reconciler, 3)

        assert len(service.specs.list(fleet_id=GH)) == 1

    def test_the_operation_says_why_it_failed(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        spec = the_runner(service)
        operation_id = spec["current_operation"]
        expire_deadlines(service)

        reconciler.pass_once()

        operation = service.operations.get(operation_id)
        assert operation["state"] == "failed"
        assert "interrupted while provisioning" in operation["error"]
        assert "remove_unit" in operation["error"]


class TestAnInterruptedStepIsTakenAgain:
    """Not a creation state, so not swept - re-driven instead. Each of these
    has one way forward and is idempotent, and nothing reports them."""

    def test_a_stop_interrupted_after_it_began_finishes(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        spec = the_runner(service)
        service.stop(spec["runner_id"])
        spec = service.specs.get(spec["runner_id"])
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             actual_state="stopping")   # died mid-stop

        passes(service, reconciler, 2)

        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "stopped"


class TestNoBackDoorToADegradedWorker:
    """Found by switching adoption off in a mutation test.

    A failed provision runs the flow's compensations, and those remove the
    unit. So a provision sent to a worker the controller does not trust could
    dispatch a removal there - the destructive verb the design forbids, by the
    back door. Provision and register are now held behind the same gate.
    """

    def test_a_placed_runner_is_not_provisioned_on_a_silent_worker(self,
                                                                   world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        before = list(UnitRuntime.log)

        report = reconciler.pass_once()

        assert UnitRuntime.log == before, "nothing was sent to the worker"
        assert any("provision held" in r for _, r in report.held)

    def test_it_resumes_when_the_worker_answers(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        UnitRuntime.crash_after_create = True
        crashing_pass(reconciler)
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        reconciler.pass_once()

        passes(service, reconciler)             # heartbeats again

        assert healthy(service, forges, the_runner(service))

    def test_an_unplaced_runner_is_still_placed_elsewhere(self, world):
        """The gate is about the worker a runner is already on. A new runner
        is placed on a healthy one, so it is not held."""
        service, flow, agent, forges, reconciler = world
        service.inventory.register_worker("linux-2", inv.HYPERV_LINUX)
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL"
                      " WHERE host_id = 'linux-1'")
        service.inventory.heartbeat("linux-2")
        service.scale_up(GH)

        reconciler.pass_once()

        assert the_runner(service)["host_id"] == "linux-2"


class TestAnOverdueOperationIsCarriedOnAfterARestart:
    """T-0306's "an operation past its deadline is re-driven", end to end.

    Design 17.3: the controller restarts mid-operation, and operations still
    running past their deadline are re-driven - safe because every step is
    idempotent on the runner_id. The operation tests only show such an
    operation is listed as overdue; this shows something then finishes it.
    """

    def test_a_stop_cut_off_by_a_restart_is_finished_by_the_next_controller(
            self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        spec = the_runner(service)
        operation_id = service.stop(spec["runner_id"])
        spec = service.specs.get(spec["runner_id"])
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             actual_state="stopping")    # died mid-stop
        expire_deadlines(service)
        assert [o["operation_id"] for o in service.operations.overdue()] == \
            [operation_id]

        restarted = Reconciler(service, flow)          # a new process
        passes(service, restarted, 2)

        assert service.specs.get(spec["runner_id"])["actual_state"] == \
            "stopped"
        assert service.operations.get(operation_id)["state"] == "succeeded"
        assert service.operations.overdue() == []
