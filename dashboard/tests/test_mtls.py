"""Mutual TLS between the controller and its agents (T-0402).

Real handshakes, against a real agent server on localhost, with certificates
minted by the control plane's own authority. Nothing is mocked below the TLS
layer - a test that faked the handshake would prove only that the fake agreed.

The plan's four refusals each have a test, and each must give its own reason:
a foreign authority, a wrong subject, a changed fingerprint, and plain HTTP.
Two more are asserted because the whole arrangement leans on them: nothing is
sent to an agent before it has been verified - the request body can carry a
registration token - and the agent refuses a client that is anything but the
controller, including another agent holding a perfectly valid certificate from
the same authority.
"""
import http.client
import os

import pytest

from control import agent_client as ac
from control import audit, ca
from control.agent_client import AgentClient, AgentRefused, AgentUnreachable
from control.inventory import HYPERV_LINUX, Inventory
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


@pytest.fixture
def agent(pki, db):
    server, runtime, registrar, pem = serve(pki)
    enrol(db, "linux-1", server, pem)
    yield server, runtime, registrar, pem
    server.stop()


class TestTheRightAgentIsReached:
    def test_a_verified_call_succeeds(self, pki, db, agent):
        client = controller(pki, db)
        assert client.call("linux-1", "hello")["host_id"] == "linux-1"

    def test_a_verb_reaches_the_runtime(self, pki, db, agent):
        server, runtime, registrar, pem = agent
        controller(pki, db).call("linux-1", "exec_unit.status",
                                 {"runner_id": RID})
        assert runtime.calls == [("status", RID)]

    def test_the_fingerprint_is_the_same_from_the_file_and_the_wire(
            self, pki, db, agent):
        """The pin is computed from the issued file and compared with what the
        live connection presents. If the two encodings disagreed, every call
        would be refused."""
        server, runtime, registrar, pem = agent
        assert Inventory(db).get("linux-1")["certificate_fingerprint"] == \
            ca.fingerprint(pem)
        assert controller(pki, db).call("linux-1", "hello")


class TestEachRefusalHasItsOwnReason:
    def reason_of(self, fn):
        with pytest.raises(AgentRefused) as caught:
            fn()
        return caught.value.reason

    def test_a_certificate_from_another_authority(self, pki, db, tmp_path):
        other = ca.create_ca("somebody else's CA")
        server, runtime, _, pem = serve(pki, authority=other, name="foreign")
        enrol(db, "linux-1", server, pem)
        try:
            reason = self.reason_of(
                lambda: controller(pki, db).call("linux-1", "hello"))
        finally:
            server.stop()
        assert reason == ac.WRONG_CA

    def test_one_of_our_own_workers_at_another_ones_address(self, pki, db):
        """Chains perfectly - it is ours - but it is not the worker asked
        for."""
        server, runtime, _, pem = serve(pki, subject="linux-2")
        enrol(db, "linux-1", server, pem)
        try:
            reason = self.reason_of(
                lambda: controller(pki, db).call("linux-1", "hello"))
        finally:
            server.stop()
        assert reason == ac.WRONG_SUBJECT

    def test_the_right_worker_with_a_certificate_nobody_pinned(self, pki,
                                                               db):
        """What an unexpected re-issue looks like. Only the pin can see it."""
        server, runtime, _, pem = serve(pki)
        reissued = pki.issue("linux-1", "agent", name="old")[2]
        enrol(db, "linux-1", server, reissued)      # pinned to another cert
        try:
            reason = self.reason_of(
                lambda: controller(pki, db).call("linux-1", "hello"))
        finally:
            server.stop()
        assert reason == ac.FINGERPRINT_CHANGED

    def test_plain_http(self, pki, db, agent):
        server, runtime, registrar, pem = agent
        enrol(db, "linux-1", server, pem, scheme="http")
        assert self.reason_of(
            lambda: controller(pki, db).call("linux-1", "hello")) == \
            ac.PLAIN_HTTP

    def test_plain_http_is_refused_before_anything_connects(self, pki, db):
        """Nothing listens at this address. A client that tried to connect
        would report it unreachable; this one never gets that far."""
        Inventory(db).register_worker("linux-9", HYPERV_LINUX,
                                      endpoint="http://127.0.0.1:1",
                                      certificate_fingerprint="sha256:x")
        assert self.reason_of(
            lambda: controller(pki, db).call("linux-9", "hello")) == \
            ac.PLAIN_HTTP

    def test_a_worker_with_no_pin_is_not_trusted_on_first_use(self, pki, db,
                                                               agent):
        server, runtime, registrar, pem = agent
        enrol(db, "linux-1", server, None)
        assert self.reason_of(
            lambda: controller(pki, db).call("linux-1", "hello")) == \
            ac.NOT_PINNED

    def test_the_four_reasons_are_four_different_words(self):
        assert len({ac.WRONG_CA, ac.WRONG_SUBJECT, ac.FINGERPRINT_CHANGED,
                    ac.PLAIN_HTTP}) == 4


class TestNothingIsSentBeforeTheAgentIsVerified:
    """The body can carry a registration token."""

    PLAN = {"url": "https://git.example", "token": "tok-SENTINEL-55443322",
            "labels": "self-hosted"}

    def test_an_impostor_receives_no_request(self, pki, db):
        server, runtime, registrar, pem = serve(pki, subject="linux-2")
        enrol(db, "linux-1", server, pem)
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "runner.register",
                                         {"runner_id": RID,
                                          "plan": self.PLAN})
        finally:
            server.stop()
        assert registrar.calls == []

    def test_a_certificate_nobody_pinned_receives_no_request(self, pki, db):
        server, runtime, registrar, pem = serve(pki)
        other_pin = pki.issue("linux-1", "agent", name="pinned")[2]
        enrol(db, "linux-1", server, other_pin)
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "runner.register",
                                         {"runner_id": RID,
                                          "plan": self.PLAN})
        finally:
            server.stop()
        assert registrar.calls == []


class TestTheAgentRefusesAnythingButTheController:
    def test_another_agents_certificate_is_not_a_controller(self, pki, db,
                                                            agent):
        """Every agent holds a valid certificate from the same authority. If
        the agent checked only the chain, a compromised worker could command
        every other one."""
        server, runtime, registrar, pem = agent
        impostor = controller(pki, db, subject="linux-2", name="linux-2-cl")
        with pytest.raises(AgentRefused) as caught:
            impostor.call("linux-1", "exec_unit.stop", {"runner_id": RID})
        assert caught.value.reason == ac.REFUSED_BY_AGENT
        assert runtime.calls == []
        assert ("subject", "linux-2") in server.refusals

    def test_a_client_from_another_authority_is_refused(self, pki, db,
                                                        agent):
        server, runtime, registrar, pem = agent
        other = ca.create_ca("somebody else's CA")
        cert, key, _ = pki.issue(ca.CONTROLLER_SUBJECT, "controller",
                                 name="foreign-cl", authority=other)
        # Trusts our CA for the agent, but presents a foreign client cert.
        client = AgentClient(Inventory(db), cert, key, pki.ca_file,
                             audit_path=db, timeout=5)
        with pytest.raises(AgentRefused) as caught:
            client.call("linux-1", "hello")
        assert caught.value.reason == ac.REFUSED_BY_AGENT
        assert runtime.calls == []

    def test_a_plain_text_client_gets_nothing(self, pki, db, agent):
        server, runtime, registrar, pem = agent
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        with pytest.raises((ConnectionError, http.client.HTTPException,
                            OSError)):
            conn.request("POST", "/v1/op/hello", body=b"{}")
            conn.getresponse().read()
        assert server.refusals and server.refusals[-1][0] == "handshake"

    def test_a_client_with_no_certificate_gets_nothing(self, pki, db, agent):
        import ssl
        server, runtime, registrar, pem = agent
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.load_verify_locations(pki.ca_file)
        conn = http.client.HTTPSConnection("127.0.0.1", server.port,
                                           context=context, timeout=5)
        with pytest.raises((ssl.SSLError, ConnectionError,
                            http.client.HTTPException, OSError)):
            conn.request("POST", "/v1/op/hello", body=b"{}")
            conn.getresponse().read()
        assert runtime.calls == []


class TestEveryRefusalIsRecorded:
    def test_it_lands_in_the_audit_trail_with_its_reason(self, pki, db):
        server, runtime, _, pem = serve(pki, subject="linux-2")
        enrol(db, "linux-1", server, pem)
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        rows = audit.entries(db)
        assert rows[0]["decision"] == f"refused: {ac.WRONG_SUBJECT}"
        assert rows[0]["verb"] == "hello"
        assert "linux-1" in rows[0]["parameters"]

    def test_the_record_never_holds_the_request_body(self, pki, db):
        server, runtime, _, pem = serve(pki, subject="linux-2")
        enrol(db, "linux-1", server, pem)
        try:
            with pytest.raises(AgentRefused):
                controller(pki, db).call(
                    "linux-1", "runner.register",
                    {"runner_id": RID,
                     "plan": TestNothingIsSentBeforeTheAgentIsVerified.PLAN})
        finally:
            server.stop()
        assert "SENTINEL" not in str(audit.entries(db))


class TestUnreachableIsNotARefusal:
    def test_nothing_listening_is_reported_as_such(self, pki, db):
        """It proves nothing about who is at the address - only that nobody
        was - so it must not read as a security refusal."""
        Inventory(db).register_worker("linux-9", HYPERV_LINUX,
                                      endpoint="https://127.0.0.1:1",
                                      certificate_fingerprint="sha256:x")
        with pytest.raises(AgentUnreachable):
            controller(pki, db).call("linux-9", "hello")


class TestNoCodePathReachesAnAgentUnverified:
    """T-0402's definition of done, read from the source."""

    CONTROL = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "control")

    def sources(self):
        for name in sorted(os.listdir(self.CONTROL)):
            if name.endswith(".py"):
                with open(os.path.join(self.CONTROL, name),
                          encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_no_plain_http_connection_is_made_anywhere_in_the_controller(
            self):
        for name, source in self.sources():
            assert "HTTPConnection(" not in source.replace(
                "HTTPSConnection(", ""), name

    def test_only_the_agent_client_opens_a_connection(self):
        """The receiver accepts connections from agents; it opens none. It is
        allowed its server-side TLS wrap and nothing that dials out."""
        for name, source in self.sources():
            if name == "agent_client.py":
                continue
            outbound = ["HTTPSConnection(", "urlopen(", "create_connection("]
            if name != "receiver.py":
                outbound.append("wrap_socket(")
            for marker in outbound:
                assert marker not in source, f"{name} uses {marker}"

    def test_the_receiver_only_wraps_the_server_side(self):
        with open(os.path.join(self.CONTROL, "receiver.py"),
                  encoding="utf-8") as fh:
            source = fh.read()
        assert source.count("wrap_socket(") == 1
        assert "server_side=True" in source

    def test_the_client_requires_a_verified_certificate(self, pki, db):
        client = controller(pki, db)
        import ssl
        assert client._context.verify_mode == ssl.CERT_REQUIRED
