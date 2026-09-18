"""Heartbeats: sent by the agent, received and checked by the controller.

End to end over real mutual TLS - the agent's sender, the controller's
receiver, the inventory behind it. The plan's three tests are here: missed
beats degrade a worker, a degraded worker is sent nothing destructive, and
`unknown` survives a round trip. Its definition of done is too: both
`workers.last_seen_at` and `runner_specs.last_seen_at` are driven by beats.

The class after those is the one that keeps the arrangement honest. A beat's
sender is who its certificate says, not who its payload says, and a worker can
report only on runners placed on it. Otherwise one worker could keep a dead one
looking healthy, or rewrite another's runners.
"""
from datetime import datetime, timedelta, timezone

import pytest

from control import agent_client as ac
from control import audit, ca
from control import inventory as inv
from control.agent_client import AgentRefused
from control.receiver import Receiver, server_context
from store import schema
from store.specs import SpecStore
from tests.agent_harness import PKI, RID, controller, enrol, serve

from agent import heartbeat as hb                         # noqa: E402
from agent import tls as agent_tls                        # noqa: E402
from agent.tests.fakes import FakeRegistrar, FakeRuntime  # noqa: E402
from agent.verbs import Agent                             # noqa: E402

OTHER = "550e8400-e29b-41d4-a716-446655440000"


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


def worker(pki, db, host_id="linux-1", units=None, enrolled=True,
           pin=True):
    """An enrolled worker that can beat: its certificate, pinned, and an
    agent whose runtime reports `units`."""
    cert, key, pem = pki.issue(host_id, "agent", name=f"{host_id}-beat")
    if enrolled:
        inventory = inv.Inventory(db)
        inventory.register_worker(
            host_id, inv.HYPERV_LINUX,
            certificate_fingerprint=ca.fingerprint(pem) if pin else None)
    runtime = FakeRuntime(units=units)
    agent = Agent(host_id, runtime, FakeRegistrar(), version="0.4")
    return agent, agent_tls.client_context(cert, key, pki.ca_file)


def sender(agent, context, receiver):
    return hb.HeartbeatSender(
        agent, f"https://127.0.0.1:{receiver.port}/v1/heartbeat", context,
        timeout=5)


def placed(db, host_id="linux-1"):
    """A runner spec placed on `host_id`, which is created if it does not
    exist yet - the spec's foreign key requires it, and enrolling it properly
    afterwards only updates the row."""
    if host_id and inv.Inventory(db).get(host_id) is None:
        inv.Inventory(db).register_worker(host_id, inv.HYPERV_LINUX)
    store = SpecStore(db)
    runner_id = store.create(provider="github", platform="linux",
                             host_id=host_id)
    return runner_id


class TestBeatsDriveLastSeen:
    """T-0404's definition of done."""

    def test_a_beat_makes_the_worker_healthy(self, pki, db, receiver):
        agent, context = worker(pki, db)
        assert inv.Inventory(db).health("linux-1") == inv.UNKNOWN

        assert sender(agent, context, receiver).send_once() is True

        assert inv.Inventory(db).health("linux-1") == inv.HEALTHY
        assert inv.Inventory(db).get("linux-1")["last_seen_at"]

    def test_a_beat_records_the_agents_version_and_capabilities(
            self, pki, db, receiver):
        agent, context = worker(pki, db)
        sender(agent, context, receiver).send_once()
        row = inv.Inventory(db).get("linux-1")
        assert row["agent_version"] == "0.4"
        assert row["capabilities"]["job_containers"] is True

    def test_a_beat_drives_each_runners_last_seen(self, pki, db, receiver):
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "running"}])

        sender(agent, context, receiver).send_once()

        spec = SpecStore(db).get(runner_id)
        assert spec["last_seen_at"]
        assert spec["unit_state"] == "running"

    def test_a_beat_does_not_disturb_a_change_being_made(self, pki, db,
                                                         receiver):
        """An observation every ten seconds, outside spec_version. Inside it,
        every change a person made would collide with the next beat."""
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "running"}])
        before = SpecStore(db).get(runner_id)["spec_version"]
        sender(agent, context, receiver).send_once()
        assert SpecStore(db).get(runner_id)["spec_version"] == before


class TestMissedBeatsDegrade:
    def test_three_missed_beats_is_degraded(self, pki, db, receiver):
        agent, context = worker(pki, db)
        sender(agent, context, receiver).send_once()
        later = datetime.now(timezone.utc) + timedelta(
            seconds=inv.HEARTBEAT_SECONDS * 3 + 1)
        assert inv.Inventory(db).health("linux-1", now=later) == \
            inv.DEGRADED

    def test_two_missed_beats_is_not(self, pki, db, receiver):
        agent, context = worker(pki, db)
        sender(agent, context, receiver).send_once()
        later = datetime.now(timezone.utc) + timedelta(
            seconds=inv.HEARTBEAT_SECONDS * 2)
        assert inv.Inventory(db).health("linux-1", now=later) == \
            inv.HEALTHY

    def test_the_interval_is_the_designs_ten_seconds(self):
        """Not 17.2's five: that is how long one beat may take, not how
        often they come."""
        assert inv.HEARTBEAT_SECONDS == 10
        assert hb.INTERVAL == 10


class TestADegradedWorkerGetsNothingDestructive:
    def test_the_client_refuses_to_send_one(self, pki, db):
        server, runtime, _, pem = serve(pki)
        inventory = enrol(db, "linux-1", server, pem)
        with schema.connect(db) as c:
            c.execute("UPDATE workers SET last_seen_at = ?",
                      ("2000-01-01T00:00:00Z",))
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "exec_unit.remove",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert caught.value.reason == ac.WORKER_NOT_HEALTHY
        assert runtime.calls == []
        assert audit.entries(db)[0]["decision"] == \
            f"refused: {ac.WORKER_NOT_HEALTHY}"

    def test_nor_to_a_worker_that_has_never_beaten(self, pki, db):
        """Unknown is not healthy."""
        server, runtime, _, pem = serve(pki)
        enrol(db, "linux-1", server, pem)
        with schema.connect(db) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        try:
            with pytest.raises(AgentRefused) as caught:
                controller(pki, db).call("linux-1", "runner.deregister",
                                         {"runner_id": RID})
        finally:
            server.stop()
        assert caught.value.reason == ac.WORKER_NOT_HEALTHY

    def test_a_read_still_goes_through(self, pki, db):
        """Being unsure a worker is alive is a reason not to destroy things on
        it, not a reason not to look."""
        server, runtime, _, pem = serve(pki)
        enrol(db, "linux-1", server, pem)
        with schema.connect(db) as c:
            c.execute("UPDATE workers SET last_seen_at = NULL")
        try:
            controller(pki, db).call("linux-1", "exec_unit.status",
                                     {"runner_id": RID})
        finally:
            server.stop()
        assert runtime.calls == [("status", RID)]

    def test_every_destructive_verb_is_covered(self):
        assert {"exec_unit.stop", "exec_unit.remove", "runner.deregister",
                "exec_unit.clear_cache", "exec_unit.restart"} <= \
            ac.DESTRUCTIVE_VERBS


class TestUnknownSurvivesARoundTrip:
    def test_a_unit_the_agent_cannot_read_stays_unknown(self, pki, db,
                                                        receiver):
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "unknown"}])
        sender(agent, context, receiver).send_once()
        assert SpecStore(db).get(runner_id)["unit_state"] == "unknown"

    def test_a_word_nobody_defined_becomes_unknown_not_a_guess(
            self, pki, db, receiver):
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "exploding"}])
        sender(agent, context, receiver).send_once()
        assert SpecStore(db).get(runner_id)["unit_state"] == "unknown"

    def test_a_worker_never_heard_from_is_unknown_not_degraded(self, db):
        inv.Inventory(db).register_worker("fresh", inv.HYPERV_LINUX)
        assert inv.Inventory(db).health("fresh") == inv.UNKNOWN

    def test_a_runtime_that_cannot_list_its_units_claims_nothing(
            self, pki, db, receiver):
        """An empty list would say "I run nothing". The beat says it could not
        tell, and what the controller knew stays as it was."""
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "running"}])
        sender(agent, context, receiver).send_once()
        agent.runtime.units = RuntimeError("the engine is not answering")

        sender(agent, context, receiver).send_once()

        assert SpecStore(db).get(runner_id)["unit_state"] == "running"
        assert hb.build(agent)["instances_error"] is True
        assert "instances" not in hb.build(agent)

    def test_a_unit_the_beat_does_not_mention_is_left_alone(self, pki, db,
                                                            receiver):
        """Silence about a unit is not evidence about it."""
        runner_id = placed(db)
        agent, context = worker(pki, db, units=[
            {"runner_id": runner_id, "state": "running"}])
        sender(agent, context, receiver).send_once()
        agent.runtime.units = []
        sender(agent, context, receiver).send_once()
        assert SpecStore(db).get(runner_id)["unit_state"] == "running"


class TestTheSenderIsWhoItsCertificateSays:
    def test_a_beat_naming_another_worker_is_refused(self, pki, db,
                                                     receiver):
        """Without this one worker could keep a dead one looking healthy."""
        agent, context = worker(pki, db, host_id="linux-1")
        inv.Inventory(db).register_worker("linux-2", inv.HYPERV_LINUX)
        agent.host_id = "linux-2"                   # lies in the payload

        assert sender(agent, context, receiver).send_once() is False
        assert inv.Inventory(db).health("linux-2") == inv.UNKNOWN
        assert "rejected" in audit.entries(db)[0]["decision"]

    def test_a_worker_cannot_report_on_anothers_runner(self, pki, db,
                                                       receiver):
        theirs = placed(db, host_id=None)
        with schema.connect(db) as c:
            c.execute("INSERT INTO workers (host_id, kind) VALUES"
                      " ('linux-2', 'hyperv-linux')")
            c.execute("UPDATE runner_specs SET host_id = 'linux-2'"
                      " WHERE runner_id = ?", (theirs,))
        agent, context = worker(pki, db, units=[
            {"runner_id": theirs, "state": "absent"}])

        sender(agent, context, receiver).send_once()

        spec = SpecStore(db).get(theirs)
        assert spec["unit_state"] is None
        assert spec["last_seen_at"] is None

    def test_a_certificate_for_no_enrolled_worker_is_refused(
            self, pki, db, receiver):
        agent, context = worker(pki, db, enrolled=False)
        assert sender(agent, context, receiver).send_once() is False
        assert audit.entries(db)[0]["decision"] == "refused: unknown-worker"

    def test_a_certificate_nobody_pinned_is_refused(self, pki, db, receiver):
        agent, context = worker(pki, db)
        with schema.connect(db) as c:
            c.execute("UPDATE workers SET certificate_fingerprint ="
                      " 'sha256:someone-else'")
        assert sender(agent, context, receiver).send_once() is False
        assert audit.entries(db)[0]["decision"] == \
            "refused: fingerprint-changed"


class TestTheSenderChecksWhoItTalksTo:
    def test_it_sends_nothing_to_a_receiver_that_is_not_the_controller(
            self, pki, db):
        """The beat says which runners live on this worker. That is for the
        controller and nobody else."""
        cert, key, _ = pki.issue("linux-9", "agent", name="impostor")
        received = []

        class Recording(Receiver):
            def take(self, what, host_id, body):
                received.append(body)
                return {}

        impostor = Recording(inv.Inventory(db),
                             server_context(cert, key, pki.ca_file),
                             audit_path=db).start()
        agent, context = worker(pki, db)
        try:
            assert sender(agent, context, impostor).send_once() is False
        finally:
            impostor.stop()
        assert received == []

    def test_it_refuses_plain_http(self, pki, db):
        agent, context = worker(pki, db)
        with pytest.raises(ValueError):
            hb.HeartbeatSender(agent, "http://127.0.0.1:1/v1/heartbeat",
                               context)


class TestWhatABeatCarries:
    def test_version_protocol_served_verbs_units_and_counters(self, pki, db):
        agent, _ = worker(pki, db, units=[{"runner_id": RID,
                                           "state": "stopped"}])
        beat = hb.build(agent)
        assert beat["host_id"] == "linux-1"
        assert beat["agent_version"] == "0.4"
        assert beat["protocol_major"] == 1
        assert "hello" in beat["served"]
        assert beat["instances"] == [{"runner_id": RID, "state": "stopped"}]
        assert "refused_connections" in beat["counters"]

    def test_it_carries_no_secret(self, pki, db):
        agent, _ = worker(pki, db)
        text = str(hb.build(agent)).lower()
        assert "token" not in text and "key" not in text
