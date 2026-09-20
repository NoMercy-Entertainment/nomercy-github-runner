"""Adopting a runner that is already serving (T-0802, MIG-4).

The macOS runner has been taking jobs from its appliance for months. It was
installed by hand: its own launchd job, its own directory, its own forge
registration. Making it managed must not mean making it again - a new
registration would strand the old record and a rebuild would interrupt a job.

So `adopt` writes the spec around what is already there: the name it has, the
registration the forge already holds, and the worker whose agent can reach
it. The one remote act is telling that agent which job this runner is; the
reconciler takes it from `provisioned` the ordinary way.

What these tests hold it to: no registration is ever minted, the forge record
is untouched, adopting twice adopts once, and adopting something the forge
does not know is refused before a row is written.
"""
import pytest

from control import main
from store import schema
from store.fleets import fleet_id
from store.specs import SpecStore

MAC = fleet_id("forgejo", "macos", "x64")
HOST = "macos-appliance-1"
NAME = "beaststack-macos-sequoia"
ENV = {"FORGEJO_INSTANCE_URL": "https://forgejo.example",
       "FORGEJO_API_TOKEN": "forgejo-deployment-token-0000"}


class FakeForge:
    """The forge as `adopt` uses it: it lists what it has registered, and
    refuses to be asked for anything else."""

    key = "forgejo"

    def __init__(self, records=None):
        self.records = list(records if records is not None else [
            {"id": "17", "uuid": "b3f1-uuid", "name": NAME,
             "status": "idle"}])
        self.minted = []

    def forge_records(self, env):
        return list(self.records)

    def mint(self, *a, **kw):                       # pragma: no cover
        self.minted.append((a, kw))
        raise AssertionError("adopting must never mint a token")


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    from store.fleets import FleetStore
    FleetStore(path).seed(ENV)
    from control.inventory import Inventory
    Inventory(path).register_worker(
        HOST, kind="hyperv-linux", endpoint="https://172.19.136.46:8443",
        capabilities={"kind": "macos-appliance", "max_runners": 2})
    return path


@pytest.fixture
def forge():
    return FakeForge()


def adopt(db, forge, **changes):
    call = dict(fleet=MAC, name=NAME, host_id=HOST,
                label="org.forgejo.runner",
                template="forgejo-runner-darwin-amd64-v12.0.1",
                db=db, env=ENV, forge=forge)
    call.update(changes)
    return main.adopt(**call)


class TestWhatItWrites:
    def test_the_spec_carries_the_registration_the_forge_already_holds(
            self, db, forge):
        runner_id = adopt(db, forge)
        spec = SpecStore(db).get(runner_id)
        assert spec["registration_id"] == "17"
        assert spec["registration_uuid"] == "b3f1-uuid"

    def test_it_keeps_the_name_the_runner_is_known_by(self, db, forge):
        spec = SpecStore(db).get(adopt(db, forge))
        assert spec["display_name"] == NAME

    def test_it_is_placed_on_the_worker_that_can_reach_it(self, db, forge):
        spec = SpecStore(db).get(adopt(db, forge))
        assert spec["host_id"] == HOST
        assert spec["fleet_id"] == MAC

    def test_it_is_born_planned_like_any_other_runner(self, db, forge):
        """Intent only: the reconciler provisions it, and for a unit that
        exists and a registration the forge still holds, provisioning is
        adoption. A spec written straight into `provisioned` would be this
        command claiming to have seen something it never looked at."""
        spec = SpecStore(db).get(adopt(db, forge))
        assert spec["actual_state"] == "planned"
        assert spec["desired_state"] == "running"

    def test_the_fleet_counts_it_so_the_next_pass_keeps_it(self, db, forge):
        adopt(db, forge)
        from store.fleets import FleetStore
        assert FleetStore(db).get(MAC)["desired_capacity"] == 1

    def test_the_forge_is_never_asked_for_a_token(self, db, forge):
        adopt(db, forge)
        assert forge.minted == []

    def test_the_forge_record_is_left_exactly_as_it_was(self, db, forge):
        before = [dict(r) for r in forge.records]
        adopt(db, forge)
        assert forge.records == before


class TestWhatTheWorkerIsToldLater:
    """The spec carries what the runner already is; the reconciler hands
    that to the worker when it provisions, and the worker adopts rather than
    builds."""

    def test_the_spec_says_which_job_on_the_worker_this_runner_is(self, db,
                                                                  forge):
        spec = SpecStore(db).get(adopt(db, forge))
        assert spec["adopt_unit"] == {
            "label": "org.forgejo.runner",
            "template": "forgejo-runner-darwin-amd64-v12.0.1"}

    def test_it_names_no_path_on_the_worker(self, db, forge):
        """Where the unit's definition lives is the worker's answer."""
        spec = SpecStore(db).get(adopt(db, forge))
        assert not [v for v in spec["adopt_unit"].values()
                    if isinstance(v, str) and "/" in v]

    def test_the_unit_it_asks_for_is_an_adoption_not_an_image(self, db,
                                                              forge):
        from control.agent_runtime import AgentRuntime
        spec = SpecStore(db).get(adopt(db, forge))
        unit = AgentRuntime(wiring=None, host_id=HOST).unit_spec(spec)
        assert unit == {"adopt": spec["adopt_unit"]}
        assert "image" not in unit


class TestWhatItRefuses:
    def test_a_runner_the_forge_does_not_know(self, db):
        forge = FakeForge(records=[])
        with pytest.raises(main.Refused, match="no runner named"):
            adopt(db, forge)
        assert SpecStore(db).list() == []

    def test_a_worker_that_is_not_enrolled(self, db, forge):
        with pytest.raises(main.Refused, match="no worker"):
            adopt(db, forge, host_id="nowhere")
        assert SpecStore(db).list() == []

    def test_a_fleet_that_does_not_exist(self, db, forge):
        with pytest.raises(main.Refused, match="fleet"):
            adopt(db, forge, fleet="forgejo-plan9-x64")
        assert SpecStore(db).list() == []

    def test_a_runner_that_is_already_adopted(self, db, forge):
        first = adopt(db, forge)
        assert adopt(db, forge) == first, \
            "adopting twice adopts once; it does not make a second spec"
        assert len(SpecStore(db).list()) == 1


class TestAddressingAnAdoptedUnit:
    """A unit's handle is the worker's own word for it - `github-runner-1`
    for a container that was there first. The controller stores it and hands
    it back; it must never read a runner_id out of it, which is what
    `ExecUnitRef` has said from the start and what adoption proves.
    """

    def test_a_ref_carries_the_runner_it_belongs_to(self, db, forge):
        from control.provision import ProvisioningFlow
        from control.agent_runtime import runner_id_of
        from control.service import RunnerService
        from store.specs import SpecStore

        runner_id = adopt(db, forge)
        SpecStore(db).update(runner_id, 1, exec_unit_ref="github-runner-1")
        spec = SpecStore(db).get(runner_id)
        flow = ProvisioningFlow(RunnerService(db, env=ENV), agent=None,
                                forges=None, env=ENV)
        assert runner_id_of(flow._ref(spec)) == runner_id

    def test_a_handle_that_names_no_runner_and_carries_none_is_refused(self):
        from control.agent_runtime import NotBound, runner_id_of
        from runtime.base import ExecUnitKind, ExecUnitRef
        with pytest.raises(NotBound):
            runner_id_of(ExecUnitRef(kind=ExecUnitKind.LINUX_CONTAINER,
                                     handle="github-runner-1"))

    def test_a_unit_this_controller_made_still_yields_its_runner(self):
        from control.agent_runtime import runner_id_of
        from runtime.base import ExecUnitKind, ExecUnitRef
        from store import storage
        rid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        ref = ExecUnitRef(kind=ExecUnitKind.LINUX_CONTAINER,
                          handle=storage.unit_name(rid))
        assert runner_id_of(ref) == rid


class TestUndoingAnAdoption:
    """A compensation undoes what the flow did. For an adopted runner the
    flow made nothing: the unit was serving before this controller knew it,
    and the forge's record is older still.

    This cost three live runners on 2026-09-20. A registration step failed on
    a runner adopted an hour earlier, the flow compensated the way it does
    for a runner it built - deregister, then remove the unit - and removed
    two containers that were serving. The agent had been refusing those
    removals by accident, for an unrelated reason, until that accident was
    fixed.
    """

    def undoing(self, spec, step="verify_online"):
        """What the flow would undo for this spec after `step` failed."""
        from control.provision import ProvisioningFlow
        from control.service import RunnerService
        flow = ProvisioningFlow(RunnerService(":memory:", env=ENV),
                                agent=None, forges=None, env=ENV)
        return flow.compensations(spec, step)

    def test_an_adopted_runner_is_never_removed_or_deregistered(self):
        spec = {"runner_id": "r1", "adopt_unit": {"label": "github-runner-1"}}
        assert self.undoing(spec) == ()

    def test_a_runner_this_controller_built_is_undone_as_before(self):
        spec = {"runner_id": "r1", "adopt_unit": None}
        assert self.undoing(spec) == ("deregister", "remove_unit")
        assert self.undoing(spec, "create_unit") == ("remove_unit",)

    def test_it_holds_for_every_step_that_can_fail(self):
        from control.provision import STEPS
        spec = {"runner_id": "r1", "adopt_unit": {"label": "x"}}
        for step in STEPS:
            assert self.undoing(spec, step) == (), step


class TestANoteIsNotAnError:
    """A runner that registered with labels of its own still works - it just
    takes different jobs. Every adopted runner has them (T-0802), and while
    that was written into `last_error` every card on the page was red.
    """

    def test_a_drift_is_recorded_as_a_note(self, db, forge):
        from store.specs import SpecStore
        runner_id = adopt(db, forge)
        store = SpecStore(db)
        store.update(runner_id, 1,
                     last_note="2026-09-20T09:45:10Z registered with other "
                               "labels than the fleet's: beast-unit")
        spec = store.get(runner_id)
        assert spec["last_note"]
        assert spec["last_error"] is None

    def test_the_card_keeps_them_apart(self, db, forge):
        from cards import from_spec
        from store.specs import SpecStore
        runner_id = adopt(db, forge)
        SpecStore(db).update(runner_id, 1, last_note="other labels",
                             last_error=None)
        card = from_spec(SpecStore(db).get(runner_id))
        assert card["last_note"] == "other labels"
        assert card["last_error"] is None

    def test_every_card_carries_the_field(self):
        from cards import FIELDS
        assert "last_note" in FIELDS


class TestAnAdoptionEndsWithTheUnitItAdopted:
    """Adopting means driving the unit that was already there. Once that
    unit is gone - a recreate removes it - there is nothing left to adopt,
    and the runner is built from its fleet's image like any other.

    Without this a recreate of an adopted runner removed its container and
    then tried to adopt the container it had just removed, which is a
    failure with no way out but a hand-written database edit (2026-09-20).
    """

    def test_removing_the_unit_forgets_what_it_was_adopted_from(self, db,
                                                                forge):
        from control.reconciler import forget_adoption
        from store.specs import SpecStore
        runner_id = adopt(db, forge)
        store = SpecStore(db)
        store.update(runner_id, 1, exec_unit_ref="org.forgejo.runner")
        forget_adoption(store, store.get(runner_id))
        assert store.get(runner_id)["adopt_unit"] is None

    def test_a_runner_that_was_never_adopted_is_untouched(self, db, forge):
        from control.reconciler import forget_adoption
        from store.specs import SpecStore
        store = SpecStore(db)
        runner_id = store.create(provider="github", platform="linux",
                                 fleet_id="github-linux-x64",
                                 actual_state="removing")
        before = store.get(runner_id)["spec_version"]
        forget_adoption(store, store.get(runner_id))
        assert store.get(runner_id)["spec_version"] == before


class TestACpuWindowSurvivesARebuild:
    """`cpu_limit` is adapter-interpreted: "a cpuset width, a Job Object cap,
    or a vCPU count" (11.1). For a container it is the cpuset - which is the
    only thing that changes what `nproc` reports inside it, and this fleet is
    pinned to 16-core windows precisely so a build sees sixteen.

    A quota (`--cpus`) does not: a rebuilt runner with a quota and no cpuset
    would report all 64 and spawn a job for each (2026-09-20).
    """

    class Wiring:
        images = {}
        memory = {}

    def unit(self, cpu_limit):
        from control.agent_runtime import AgentRuntime
        spec = {"runner_id": "r1", "provider": "github", "platform": "linux",
                "fleet_id": "github-linux-x64", "cpu_limit": cpu_limit,
                "runtime_template": "an-image:1"}
        return AgentRuntime(wiring=self.Wiring(), host_id="w").unit_spec(spec)

    def test_a_window_is_sent_as_a_cpuset(self):
        assert self.unit("0-15")["cpuset"] == "0-15"
        assert "cpus" not in self.unit("0-15")

    def test_a_list_of_cores_is_a_window_too(self):
        assert self.unit("0,2,4,6")["cpuset"] == "0,2,4,6"

    def test_a_plain_number_is_still_a_quota(self):
        assert self.unit("4")["cpus"] == "4"
        assert "cpuset" not in self.unit("4")

    def test_nothing_asked_is_nothing_sent(self):
        unit = self.unit(None)
        assert "cpus" not in unit and "cpuset" not in unit


    def test_a_unit_that_is_no_longer_there_ends_the_adoption(self, db,
                                                              forge):
        """The worker says there is no unit; asking it to adopt one anyway
        is a failure that repeats for ever (2026-09-20)."""
        from control.provision import ProvisioningFlow
        from control.service import RunnerService
        from store.specs import SpecStore

        class Gone:
            exists = False

        class Runtime:
            kind = "linux-container"

            def status(self, ref):
                return Gone()

            def create(self, unit):
                Runtime.asked = dict(unit)
                return "rnr-new"

        service = RunnerService(db, env=ENV)
        runner_id = adopt(db, forge)
        store = SpecStore(db)
        flow = ProvisioningFlow(service, agent=None, forges=None, env=ENV)
        flow._runtime = lambda spec, host: Runtime()
        stale = store.get(runner_id)
        # Written to since this step read it, as a busy pass does.
        store.update(runner_id, stale["spec_version"], last_note="a note")
        flow._step_create_unit(stale, {"host_id": HOST})
        assert store.get(runner_id)["adopt_unit"] is None
        assert "adopt" not in Runtime.asked


class _Report:
    def did(self, *a, **kw):
        pass
