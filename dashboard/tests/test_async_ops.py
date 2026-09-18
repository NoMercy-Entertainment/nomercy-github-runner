"""Asynchronous operations, end to end (T-0405).

Real mutual TLS both ways: the controller calls the agent, and the agent sends
its progress back to the controller's receiver. The plan's three tests are
here - a slow verb returns 202 at once, progress events update the operation,
and a lost reply is recovered by the idempotency key - and so is its
definition of done, that no agent call blocks longer than its fast timeout.

One more property is asserted because the design leans on it: events are a
courtesy, not the record. Asking again with the same key returns the state so
far, so a controller that hears no event at all still gets the outcome, and a
lost event costs time rather than correctness.
"""
import threading
import time

import pytest

from control import agent_client as ac
from control import audit, ca
from control import inventory as inv
from control.agent_client import AgentError
from control.operations import OperationStore
from control.receiver import Receiver, server_context
from store import schema
from store.specs import SpecStore
from tests.agent_harness import PKI, controller, enrol

from agent import protocol                                # noqa: E402
from agent import tls as agent_tls                        # noqa: E402
from agent.link import ControllerLink, EventSender        # noqa: E402
from agent.server import AgentServer                      # noqa: E402
from agent.tests.fakes import FakeRegistrar, FakeRuntime  # noqa: E402
from agent.verbs import Agent                             # noqa: E402


@pytest.fixture
def pki(tmp_path):
    return PKI(str(tmp_path))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return path


@pytest.fixture
def operations(db):
    return OperationStore(db)


@pytest.fixture
def receiver(pki, db, operations):
    cert, key, _ = pki.issue("controller", "receiver", name="receiver")
    r = Receiver(inv.Inventory(db), server_context(cert, key, pki.ca_file),
                 on_event=operations.apply_event, audit_path=db).start()
    yield r
    r.stop()


class Counting(AgentServer):
    """Counts the requests that reach it, to show what a wait costs."""

    handled = 0

    def handle_verb(self, handler, verb, body):
        type(self).handled += 1
        return super().handle_verb(handler, verb, body)


def an_agent(pki, db, receiver=None, runtime=None, host_id="linux-1"):
    """An enrolled, healthy agent. With a receiver, it reports progress to
    it over its own certificate."""
    cert, key, pem = pki.issue(host_id, "agent")
    runtime = runtime or FakeRuntime()
    emit = None
    if receiver is not None:
        link = ControllerLink(f"https://127.0.0.1:{receiver.port}",
                              agent_tls.client_context(cert, key,
                                                       pki.ca_file))
        emit = EventSender(link, sleep=lambda s: None)
    Counting.handled = 0
    server = Counting(Agent(host_id, runtime, FakeRegistrar()),
                      ssl_context=agent_tls.server_context(cert, key,
                                                           pki.ca_file),
                      emit=emit).start()
    enrol(db, host_id, server, pem)
    return server, runtime


def an_operation(db, operations, host_id="linux-1"):
    """An open operation about a runner placed on `host_id`. Returns
    (runner_id, operation_id)."""
    runner_id = SpecStore(db).create(provider="github", platform="linux",
                                     host_id=host_id)
    operation, _ = operations.open("provision", runner_id=runner_id)
    return runner_id, operation["operation_id"]


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


class TestASlowVerbIsAnsweredAtOnce:
    def test_it_comes_back_202_long_before_the_work_is_done(self, pki, db):
        runtime = FakeRuntime()
        runtime.delay = 2
        server, _ = an_agent(pki, db, runtime=runtime)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        try:
            started = time.monotonic()
            answer = controller(pki, db).call(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}})
            took = time.monotonic() - started
        finally:
            server.stop()
        assert answer["accepted"] is True
        assert answer["state"] == "running"
        assert answer["handle"]
        assert took < 1.0

    def test_the_work_still_happens_afterwards(self, pki, db):
        runtime = FakeRuntime()
        runtime.delay = 0.3
        server, _ = an_agent(pki, db, runtime=runtime)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        try:
            controller(pki, db).call("linux-1", "exec_unit.create",
                                     {"runner_id": runner_id,
                                      "spec": {"image": "node:20"}})
            wait_for(lambda: runtime.calls)
        finally:
            server.stop()
        assert runtime.calls[0][0] == "create"

    def test_a_bad_request_is_still_refused_at_once_not_accepted(self, pki,
                                                                 db):
        """Validation happens while the caller waits. A 202 for a request that
        cannot succeed would be a promise the agent already knows it cannot
        keep."""
        server, runtime = an_agent(pki, db)
        try:
            with pytest.raises(AgentError) as caught:
                controller(pki, db).call(
                    "linux-1", "exec_unit.create",
                    {"runner_id": "../../etc", "spec": {}})
        finally:
            server.stop()
        assert caught.value.status == 400
        assert runtime.calls == []

    def test_a_read_is_answered_directly(self, pki, db):
        server, _ = an_agent(pki, db)
        try:
            answer = controller(pki, db).call("linux-1", "hello")
        finally:
            server.stop()
        assert answer["host_id"] == "linux-1"


class TestProgressEventsUpdateTheOperation:
    def test_the_trace_follows_the_work(self, pki, db, receiver, operations):
        server, _ = an_agent(pki, db, receiver=receiver)
        runner_id, operation_id = an_operation(db, operations)
        try:
            answer = controller(pki, db).call(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                operation_id=operation_id)
            wait_for(lambda: (operations.call_state(
                operation_id, answer["handle"]) or {}).get("state") ==
                "succeeded")
        finally:
            server.stop()
        notes = [t["note"] for t in operations.get(operation_id)["trace"]]
        assert any("exec_unit.create running on linux-1" in n for n in notes)
        assert any("exec_unit.create succeeded on linux-1" in n
                   for n in notes)

    def test_the_result_is_kept_under_the_calls_handle(self, pki, db,
                                                       receiver, operations):
        server, _ = an_agent(pki, db, receiver=receiver)
        runner_id, operation_id = an_operation(db, operations)
        try:
            answer = controller(pki, db).call(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                operation_id=operation_id)
            wait_for(lambda: operations.call_state(
                operation_id, answer["handle"]) and operations.call_state(
                operation_id, answer["handle"])["state"] == "succeeded")
        finally:
            server.stop()
        state = operations.call_state(operation_id, answer["handle"])
        assert state["result"]["handle"] == f"rnr-{runner_id}"

    def test_a_failure_arrives_with_its_reason(self, pki, db, receiver,
                                               operations):
        server, _ = an_agent(pki, db, receiver=receiver,
                             runtime=FakeRuntime(raise_with="name in use"))
        runner_id, operation_id = an_operation(db, operations)
        try:
            answer = controller(pki, db).call(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                operation_id=operation_id)
            wait_for(lambda: (operations.call_state(
                operation_id, answer["handle"]) or {}).get("state") ==
                "failed")
        finally:
            server.stop()
        assert "name in use" in operations.call_state(
            operation_id, answer["handle"])["error"]

    def test_an_event_does_not_close_the_operation(self, pki, db, receiver,
                                                   operations):
        """A provision is a create and then a register. Only the controller
        knows when all of its calls are done."""
        server, _ = an_agent(pki, db, receiver=receiver)
        runner_id, operation_id = an_operation(db, operations)
        try:
            answer = controller(pki, db).call(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                operation_id=operation_id)
            wait_for(lambda: (operations.call_state(
                operation_id, answer["handle"]) or {}).get("state") ==
                "succeeded")
        finally:
            server.stop()
        assert operations.get(operation_id)["state"] in ("pending",
                                                         "running")


class TestALostReplyIsRecoveredByItsKey:
    def test_the_same_key_gets_the_same_handle(self, pki, db):
        server, runtime = an_agent(pki, db)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        body = {"runner_id": runner_id, "spec": {"image": "node:20"}}
        client = controller(pki, db)
        try:
            first = client.call("linux-1", "exec_unit.create", body,
                                idempotency_key="k-1")
            again = client.call("linux-1", "exec_unit.create", body,
                                idempotency_key="k-1")
            wait_for(lambda: runtime.calls)
            time.sleep(0.1)
        finally:
            server.stop()
        assert first["handle"] == again["handle"]

    def test_and_the_work_is_done_once(self, pki, db):
        runtime = FakeRuntime()
        runtime.delay = 0.2
        server, _ = an_agent(pki, db, runtime=runtime)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        body = {"runner_id": runner_id, "spec": {"image": "node:20"}}
        client = controller(pki, db)
        try:
            for _ in range(4):
                client.call("linux-1", "exec_unit.create", body,
                            idempotency_key="k-2")
            wait_for(lambda: runtime.calls)
            time.sleep(0.4)
        finally:
            server.stop()
        assert len([c for c in runtime.calls if c[0] == "create"]) == 1

    def test_a_key_reused_for_another_verb_is_refused(self, pki, db):
        server, _ = an_agent(pki, db)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        client = controller(pki, db)
        try:
            client.call("linux-1", "exec_unit.start",
                        {"runner_id": runner_id}, idempotency_key="k-3")
            with pytest.raises(AgentError) as caught:
                client.call("linux-1", "exec_unit.stop",
                            {"runner_id": runner_id}, idempotency_key="k-3")
        finally:
            server.stop()
        assert caught.value.status == 409


class TestWaitingForAnOutcome:
    def test_with_no_events_at_all_the_outcome_still_arrives(self, pki, db):
        """Events are a courtesy. Asking again with the same key is the
        record."""
        runtime = FakeRuntime()
        runtime.delay = 0.3
        server, _ = an_agent(pki, db, runtime=runtime)        # no receiver
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        try:
            result = controller(pki, db).call_and_wait(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                poll=0.05, deadline=10)
        finally:
            server.stop()
        assert result["handle"] == f"rnr-{runner_id}"

    def test_an_event_ends_the_wait_without_asking_again(self, pki, db,
                                                         receiver,
                                                         operations):
        server, _ = an_agent(pki, db, receiver=receiver)
        runner_id, operation_id = an_operation(db, operations)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            time.sleep(0.05)

        try:
            result = controller(pki, db).call_and_wait(
                "linux-1", "exec_unit.create",
                {"runner_id": runner_id, "spec": {"image": "node:20"}},
                operation_id=operation_id, operations=operations,
                poll=1.0, sleep=sleep, deadline=10)
        finally:
            server.stop()
        assert result["handle"] == f"rnr-{runner_id}"
        assert Counting.handled <= 1 + len(slept)

    def test_a_failure_is_raised_with_its_reason(self, pki, db):
        server, _ = an_agent(pki, db,
                             runtime=FakeRuntime(raise_with="disk full"))
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        try:
            with pytest.raises(AgentError, match="disk full"):
                controller(pki, db).call_and_wait(
                    "linux-1", "exec_unit.create",
                    {"runner_id": runner_id, "spec": {"image": "node:20"}},
                    poll=0.05, deadline=10)
        finally:
            server.stop()

    def test_work_that_never_finishes_is_given_up_on(self, pki, db):
        """Bounded by the deadline, not by any one request."""
        runtime = FakeRuntime()
        runtime.delay = threading.Event()               # never set
        server, _ = an_agent(pki, db, runtime=runtime)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        now = {"t": 0.0}
        try:
            with pytest.raises(TimeoutError):
                controller(pki, db).call_and_wait(
                    "linux-1", "exec_unit.create",
                    {"runner_id": runner_id, "spec": {"image": "node:20"}},
                    deadline=5, poll=1,
                    sleep=lambda s: now.__setitem__("t", now["t"] + s),
                    clock=lambda: now["t"])
        finally:
            runtime.delay.set()
            server.stop()


class TestAnEventIsBelievedOnlyFromTheRightWorker:
    def test_an_event_about_another_workers_runner_is_refused(
            self, db, operations):
        inv.Inventory(db).register_worker("linux-2", inv.HYPERV_LINUX)
        _, operation_id = an_operation(db, operations, host_id="linux-2")
        with pytest.raises(ValueError, match="not on this worker"):
            operations.apply_event("linux-1", {
                "operation_id": operation_id, "handle": "h",
                "state": "succeeded", "verb": "exec_unit.create"})

    def test_an_event_about_an_unknown_operation_is_refused(self,
                                                            operations):
        with pytest.raises(ValueError, match="no such operation"):
            operations.apply_event("linux-1", {
                "operation_id": "nope", "handle": "h", "state": "running"})

    def test_a_state_nobody_defined_is_refused(self, db, operations):
        inv.Inventory(db).register_worker("linux-1", inv.HYPERV_LINUX)
        _, operation_id = an_operation(db, operations)
        with pytest.raises(ValueError):
            operations.apply_event("linux-1", {
                "operation_id": operation_id, "handle": "h",
                "state": "finished-ish"})

    def test_through_the_receiver_it_is_refused_and_audited(
            self, pki, db, receiver, operations):
        inv.Inventory(db).register_worker("linux-2", inv.HYPERV_LINUX)
        _, operation_id = an_operation(db, operations, host_id="linux-2")
        cert, key, pem = pki.issue("linux-1", "agent", name="l1-events")
        inv.Inventory(db).register_worker(
            "linux-1", inv.HYPERV_LINUX,
            certificate_fingerprint=ca.fingerprint(pem))
        link = ControllerLink(f"https://127.0.0.1:{receiver.port}",
                              agent_tls.client_context(cert, key,
                                                       pki.ca_file))
        assert link.post(protocol.EVENT_PATH, {
            "operation_id": operation_id, "handle": "h",
            "state": "succeeded", "verb": "runner.register"}) is False
        assert "not on this worker" in audit.entries(db)[0]["decision"]


class TestProgressIsNeverLostToARace:
    def test_phases_and_call_states_written_together_all_survive(
            self, db, operations):
        """The reconciler records a restart's phases in the same progress
        that events write call states into. Replacing instead of merging
        would let each wipe the other."""
        inv.Inventory(db).register_worker("linux-1", inv.HYPERV_LINUX)
        _, operation_id = an_operation(db, operations)

        def phases():
            for i in range(25):
                operations.merge_progress(
                    operation_id, lambda p, i=i: dict(
                        p, done=list(p.get("done", [])) + [f"p{i}"]))

        def events():
            for i in range(25):
                operations.apply_event("linux-1", {
                    "operation_id": operation_id, "handle": f"h{i}",
                    "state": "running", "verb": "exec_unit.create"})

        threads = [threading.Thread(target=phases),
                   threading.Thread(target=events)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        progress = operations.progress(operation_id)
        assert len(progress["done"]) == 25
        assert len(progress["calls"]) == 25


class TestNoAgentCallBlocksLongerThanItsFastTimeout:
    """T-0405's definition of done."""

    READS = {"hello", "capabilities", "exec_unit.status",
             "exec_unit.telemetry", "exec_unit.logs", "exec_unit.probe"}

    def test_every_verb_is_either_a_read_or_asynchronous(self):
        assert ac.ASYNC_VERBS | self.READS == ac.VERB_NAMES
        assert not (ac.ASYNC_VERBS & self.READS)

    def test_even_a_verb_that_takes_minutes_answers_within_it(self, pki, db):
        runtime = FakeRuntime()
        runtime.delay = threading.Event()               # never finishes
        server, _ = an_agent(pki, db, runtime=runtime)
        runner_id = SpecStore(db).create(provider="github", platform="linux",
                                         host_id="linux-1")
        client = controller(pki, db, timeout=2)
        try:
            started = time.monotonic()
            client.call("linux-1", "exec_unit.create",
                        {"runner_id": runner_id,
                         "spec": {"image": "node:20"}})
            took = time.monotonic() - started
        finally:
            runtime.delay.set()
            server.stop()
        from control.retry import AGENT_FAST
        assert took < AGENT_FAST.timeout
