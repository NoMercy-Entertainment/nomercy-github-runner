"""Protocol versions: an older or newer agent is refused visibly (T-0406).

Major versions only, carried on every request as X-Protocol-Version. A side
that meets a major it does not implement says so, and the controller marks the
worker degraded with both versions in the reason - rather than guessing at
what the other side meant, or failing somewhere later with an error that says
nothing about versions.

Both directions are covered: the controller calling an agent, and an agent's
heartbeat arriving at the controller. So are both kinds of mismatch, an older
agent and a newer one, because "rather than fail obscurely" has to hold for
each. The last class is the definition of done: the reason is visible in the
dashboard.
"""
import os

import pytest

from control import agent_client as ac
from control import audit, ca
from control import inventory as inv
from control.agent_client import AgentRefused
from control.receiver import Receiver, server_context
from store import schema
from tests.agent_harness import PKI, controller, enrol, serve

from agent import protocol as agent_protocol             # noqa: E402
from agent import tls as agent_tls                        # noqa: E402
from agent.link import ControllerLink                     # noqa: E402


@pytest.fixture
def pki(tmp_path):
    return PKI(str(tmp_path))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return path


@pytest.fixture
def receiver(pki, db):
    cert, key, _ = pki.issue("controller", "receiver", name="receiver")
    r = Receiver(inv.Inventory(db), server_context(cert, key, pki.ca_file),
                 audit_path=db).start()
    yield r
    r.stop()


def agent_speaking(pki, db, majors):
    server, runtime, _, pem = serve(pki, supported_majors=majors)
    enrol(db, "linux-1", server, pem)
    return server, runtime


class TestTheControllerCallingAnAgentOfAnotherVersion:
    @pytest.mark.parametrize("theirs", [0, 2], ids=["older", "newer"])
    def test_it_is_refused_as_a_mismatch(self, pki, db, theirs):
        server, runtime = agent_speaking(pki, db, {theirs})
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        assert caught.value.reason == ac.PROTOCOL_MISMATCH

    @pytest.mark.parametrize("theirs", [0, 2], ids=["older", "newer"])
    def test_the_worker_is_degraded_with_both_versions_in_the_reason(
            self, pki, db, theirs):
        server, _ = agent_speaking(pki, db, {theirs})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        inventory = inv.Inventory(db)
        assert inventory.health("linux-1") == inv.DEGRADED
        reason = inventory.health_reason("linux-1")
        assert f"controller speaks {ac.PROTOCOL_MAJOR}" in reason
        assert f"this agent speaks {theirs}" in reason

    def test_nothing_reaches_the_runtime(self, pki, db):
        server, runtime = agent_speaking(pki, db, {2})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "exec_unit.status",
                                         {"runner_id": ac.uuid.uuid4().hex})
        finally:
            server.stop()
        assert runtime.calls == []
        assert ("protocol", "1") in server.refusals

    def test_it_is_audited(self, pki, db):
        server, _ = agent_speaking(pki, db, {2})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        assert audit.entries(db)[0]["decision"] == \
            f"refused: {ac.PROTOCOL_MISMATCH}"

    def test_an_agent_that_says_one_thing_and_reports_another_is_believed_about_the_version(  # noqa: E501
            self, pki, db, monkeypatch):
        """It let a major-1 request through but reports major 2 in hello.
        The report is the more specific statement, so it wins."""
        server, _ = agent_speaking(pki, db, {ac.PROTOCOL_MAJOR})
        monkeypatch.setattr(agent_protocol, "PROTOCOL_MAJOR", 2)
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        assert caught.value.reason == ac.PROTOCOL_MISMATCH
        assert inv.Inventory(db).health("linux-1") == inv.DEGRADED

    def test_the_same_version_goes_through(self, pki, db):
        server, _ = agent_speaking(pki, db, {ac.PROTOCOL_MAJOR})
        try:
            assert controller(pki, db).call("linux-1", "hello")["host_id"] \
                == "linux-1"
        finally:
            server.stop()


class TestAnAgentOfAnotherVersionBeating:
    def beat(self, pki, db, receiver, major):
        cert, key, pem = pki.issue("linux-1", "agent", name=f"b{major}")
        inventory = inv.Inventory(db)
        inventory.register_worker("linux-1", inv.HYPERV_LINUX,
                                  certificate_fingerprint=ca.fingerprint(pem))
        link = ControllerLink(f"https://127.0.0.1:{receiver.port}",
                              agent_tls.client_context(cert, key,
                                                       pki.ca_file),
                              protocol_major=major)
        return link.post(agent_protocol.HEARTBEAT_PATH, {
            "host_id": "linux-1", "agent_version": "x"})

    @pytest.mark.parametrize("theirs", [0, 2], ids=["older", "newer"])
    def test_the_beat_is_refused(self, pki, db, receiver, theirs):
        assert self.beat(pki, db, receiver, theirs) is False

    @pytest.mark.parametrize("theirs", [0, 2], ids=["older", "newer"])
    def test_and_the_worker_is_degraded_saying_why(self, pki, db, receiver,
                                                   theirs):
        self.beat(pki, db, receiver, theirs)
        inventory = inv.Inventory(db)
        assert inventory.health("linux-1") == inv.DEGRADED
        assert f"agent sent {theirs}" in inventory.health_reason("linux-1")

    def test_beating_in_the_wrong_version_cannot_keep_it_healthy(
            self, pki, db, receiver):
        """It beat correctly seconds ago. A beat nobody here can read must
        not leave it looking healthy on the strength of that."""
        assert self.beat(pki, db, receiver, ac.PROTOCOL_MAJOR) is True
        assert inv.Inventory(db).health("linux-1") == inv.HEALTHY

        self.beat(pki, db, receiver, 2)

        assert inv.Inventory(db).health("linux-1") == inv.DEGRADED

    def test_a_beat_in_the_right_version_clears_the_mark(self, pki, db,
                                                         receiver):
        self.beat(pki, db, receiver, 2)
        assert self.beat(pki, db, receiver, ac.PROTOCOL_MAJOR) is True
        inventory = inv.Inventory(db)
        assert inventory.health("linux-1") == inv.HEALTHY
        assert inventory.health_reason("linux-1") == ""

    def test_a_beat_with_no_version_at_all_is_refused_too(self, pki, db,
                                                          receiver):
        import http.client
        cert, key, pem = pki.issue("linux-1", "agent", name="bare")
        inv.Inventory(db).register_worker(
            "linux-1", inv.HYPERV_LINUX,
            certificate_fingerprint=ca.fingerprint(pem))
        conn = http.client.HTTPSConnection(
            "127.0.0.1", receiver.port,
            context=agent_tls.client_context(cert, key, pki.ca_file),
            timeout=5)
        conn.request("POST", agent_protocol.HEARTBEAT_PATH, body=b"{}")
        assert conn.getresponse().status == 426
        assert "agent sent none" in inv.Inventory(db).health_reason("linux-1")


class TestTheReasonsAreWords:
    def test_silence_says_how_long(self, db):
        inventory = inv.Inventory(db)
        inventory.register_worker("w", inv.HYPERV_LINUX)
        with schema.connect(db) as c:
            c.execute("UPDATE workers SET last_seen_at ="
                      " '2000-01-01T00:00:00Z'")
        assert inventory.health_reason("w").startswith("no heartbeat for ")

    def test_never_heard_from_says_so(self, db):
        inventory = inv.Inventory(db)
        inventory.register_worker("w", inv.HYPERV_LINUX)
        assert inventory.health_reason("w") == "never heard from"

    def test_a_marked_reason_wins_over_the_arithmetic(self, db):
        inventory = inv.Inventory(db)
        inventory.register_worker("w", inv.HYPERV_LINUX)
        inventory.heartbeat("w")
        inventory.mark_degraded("w", "protocol mismatch: something")
        assert inventory.health("w") == inv.DEGRADED
        assert inventory.health_reason("w") == "protocol mismatch: something"


class TestItIsVisibleInTheDashboard:
    """T-0406's definition of done."""

    @pytest.fixture
    def control_db(self, db, monkeypatch):
        monkeypatch.setattr(schema, "DB_PATH", db)
        return db

    def test_the_mismatch_and_its_reason_are_shown(self, client, control_db,
                                                   pki):
        server, _ = agent_speaking(pki, control_db, {2})
        try:
            with pytest.raises(AgentRefused):
                controller(pki, control_db).call("linux-1", "hello")
        finally:
            server.stop()

        workers = client.get("/api/control/workers").get_json()["workers"]

        row = next(w for w in workers if w["host_id"] == "linux-1")
        assert row["health"] == "degraded"
        assert "protocol mismatch" in row["reason"]
        assert "this agent speaks 2" in row["reason"]

    def test_it_is_served_under_v1_as_well(self, client, control_db):
        assert client.get("/api/v1/control/workers").status_code == 200

    def test_it_is_behind_the_same_sign_in(self, anon_client, control_db):
        assert anon_client.get("/api/control/workers").status_code == 401

    def test_before_the_control_plane_has_run_it_creates_nothing(
            self, client, tmp_path, monkeypatch):
        """Reading must not leave an empty database in the dashboard's volume
        that nothing asked for."""
        missing = str(tmp_path / "never-created.db")
        monkeypatch.setattr(schema, "DB_PATH", missing)
        body = client.get("/api/control/workers").get_json()
        assert body["workers"] == []
        assert not os.path.exists(missing)

    def test_nothing_secret_is_in_it(self, client, control_db, pki):
        server, _ = agent_speaking(pki, control_db, {ac.PROTOCOL_MAJOR})
        server.stop()
        text = client.get("/api/control/workers").get_data(as_text=True)
        assert "fingerprint" not in text
        assert "BEGIN" not in text
