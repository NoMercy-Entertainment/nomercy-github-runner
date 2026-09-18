"""Per-verb authorization, checked at both ends (T-0403).

The controller refuses a verb that is not in its policy for a worker before
opening a connection. The worker refuses a verb that is not in its own policy
before reading the request body. The plan's definition of done is that both
checks exist and both are tested - so each is tested with the other one wide
open, which is the only way to show it holds on its own.

The class that matters most is the last. The controller's policy lives in the
same column as what an agent says about itself, and heartbeats write that
column. If a heartbeat could write the policy too, an agent could grant itself
any verb it liked, and the controller-side check would be decoration.
"""
import pytest

from control import agent_client as ac
from control import audit
from control.agent_client import AgentRefused
from control.inventory import DISCOVERY, HYPERV_LINUX, Inventory
from store import schema
from tests.agent_harness import PKI, RID, controller, enrol, serve


@pytest.fixture
def pki(tmp_path):
    return PKI(str(tmp_path))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return path


def agent(pki, db, worker_allows=None, controller_allows=None):
    server, runtime, registrar, pem = serve(pki, permitted=worker_allows)
    enrol(db, "linux-1", server, pem, verbs=controller_allows)
    return server, runtime, registrar


class TestTheControllerRefusesFirst:
    """Its own policy, with the worker's wide open."""

    def test_a_verb_it_does_not_permit_is_refused(self, pki, db):
        server, runtime, _ = agent(pki, db,
                                   controller_allows={"exec_unit.status"})
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "exec_unit.stop",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert caught.value.reason == ac.NOT_PERMITTED

    def test_nothing_reaches_the_worker(self, pki, db):
        """Refused before a connection is opened, so the worker has nothing
        to refuse - its own record of refusals stays empty."""
        server, runtime, _ = agent(pki, db,
                                   controller_allows={"exec_unit.status"})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "exec_unit.stop",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert runtime.calls == []
        assert list(server.refusals) == []

    def test_the_refusal_is_audited(self, pki, db):
        server, _, _ = agent(pki, db, controller_allows=set())
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "runner.deregister",
                                         {"runner_id": RID})
        finally:
            server.stop()
        row = audit.entries(db)[0]
        assert row["decision"] == f"refused: {ac.NOT_PERMITTED}"
        assert row["verb"] == "runner.deregister"


class TestTheWorkerRefusesAgain:
    """Its own policy, with the controller's wide open."""

    def test_a_verb_it_does_not_serve_is_refused(self, pki, db):
        server, runtime, _ = agent(pki, db,
                                   worker_allows={"exec_unit.status"})
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "exec_unit.stop",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert caught.value.reason == ac.NOT_PERMITTED_BY_AGENT
        assert runtime.calls == []

    def test_the_worker_records_what_it_refused(self, pki, db):
        server, _, _ = agent(pki, db, worker_allows={"exec_unit.status"})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "exec_unit.stop",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert ("not-permitted", "exec_unit.stop") in server.refusals

    def test_and_the_controller_audits_it_as_the_workers_refusal(self, pki,
                                                                 db):
        """A different reason from its own, so the trail says which side
        said no."""
        server, _, _ = agent(pki, db, worker_allows={"exec_unit.status"})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "exec_unit.stop",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert audit.entries(db)[0]["decision"] == \
            f"refused: {ac.NOT_PERMITTED_BY_AGENT}"


class TestBothAgree:
    def test_a_verb_both_permit_goes_through(self, pki, db):
        server, runtime, _ = agent(pki, db,
                                   worker_allows={"exec_unit.status"},
                                   controller_allows={"exec_unit.status"})
        try:
            controller(pki, db).call("linux-1", "exec_unit.status",
                                     {"runner_id": RID})
        finally:
            server.stop()
        assert runtime.calls == [("status", RID)]

    def test_discovery_is_always_allowed(self, pki, db):
        """How a worker is learned about. With nothing permitted on either
        side, it can still be asked who it is and what it serves."""
        server, _, _ = agent(pki, db, worker_allows=set(),
                             controller_allows=set())
        try:
            client = controller(pki, db)
            assert client.call("linux-1", "hello")["host_id"] == "linux-1"
            served = client.call("linux-1", "capabilities")["verbs"]
        finally:
            server.stop()
        assert set(served) == DISCOVERY

    def test_the_worker_reports_its_own_policy(self, pki, db):
        server, _, _ = agent(pki, db, worker_allows={"exec_unit.status"})
        try:
            served = controller(pki, db).call("linux-1",
                                              "capabilities")["verbs"]
        finally:
            server.stop()
        assert set(served) == {"exec_unit.status"} | DISCOVERY


class TestThePolicyIsTheControllersAlone:
    """An agent cannot grant itself a verb."""

    @pytest.fixture
    def inventory(self, db):
        inventory = Inventory(db)
        inventory.register_worker("linux-1", HYPERV_LINUX,
                                  capabilities={"job_containers": True})
        inventory.permit("linux-1", {"exec_unit.status"})
        return inventory

    def test_a_heartbeat_cannot_widen_it(self, inventory):
        inventory.heartbeat("linux-1", capabilities={
            "verbs": sorted(ac.VERB_NAMES), "job_containers": False})
        assert inventory.permitted_verbs("linux-1") == \
            {"exec_unit.status"} | DISCOVERY

    def test_but_the_rest_of_what_it_declares_is_taken(self, inventory):
        """It is the authority on what it is; only the policy is not its."""
        inventory.heartbeat("linux-1", capabilities={"job_containers": False})
        assert inventory.get("linux-1")["capabilities"][
            "job_containers"] is False

    def test_re_registering_cannot_widen_it(self, inventory):
        inventory.register_worker("linux-1", HYPERV_LINUX, capabilities={
            "verbs": sorted(ac.VERB_NAMES)})
        assert inventory.permitted_verbs("linux-1") == \
            {"exec_unit.status"} | DISCOVERY

    def test_re_registering_without_capabilities_keeps_them(self, inventory):
        """The first version replaced the column with nothing whenever an
        agent re-announced itself without listing its capabilities."""
        inventory.register_worker("linux-1", HYPERV_LINUX)
        assert inventory.get("linux-1")["capabilities"]["job_containers"] \
            is True
        assert "exec_unit.status" in inventory.permitted_verbs("linux-1")

    def test_permit_replaces_rather_than_adds(self, inventory):
        """What a worker may be asked for is one statement an operator made,
        not the sum of every grant ever issued."""
        inventory.permit("linux-1", {"exec_unit.logs"})
        assert inventory.permitted_verbs("linux-1") == \
            {"exec_unit.logs"} | DISCOVERY

    def test_permit_knows_only_protocol_verbs(self, inventory):
        with pytest.raises(ValueError, match="not protocol verbs"):
            inventory.permit("linux-1", {"shell"})

    def test_a_worker_nobody_permitted_can_only_be_discovered(self, db):
        inventory = Inventory(db)
        inventory.register_worker("fresh", HYPERV_LINUX)
        assert inventory.permitted_verbs("fresh") == DISCOVERY
