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
                root="/usr/local/forgejo-runner",
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
            "root": "/usr/local/forgejo-runner",
            "template": "forgejo-runner-darwin-amd64-v12.0.1"}

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
