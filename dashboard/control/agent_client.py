"""The one way the controller reaches an agent.

Every call goes over mutual TLS, and the agent's identity is established three
ways before a byte of the request is sent:

1. its certificate chains to the control plane's own authority;
2. its subject is the `host_id` the call is addressed to;
3. its fingerprint is the one pinned for that worker in `workers`.

Each failure has its own reason, because they mean different things. A foreign
authority is someone else's certificate. A wrong subject is one of our own
workers answering at another's address. A changed fingerprint is the right
worker with a certificate nobody pinned - a re-issue the operator may not have
made. Folding these into one "TLS error" would hide which one happened, and
each refusal is recorded in the audit trail with the reason it was given.

Plain HTTP is refused before a connection is attempted, and a worker with no
pinned fingerprint is refused rather than trusted on first use: pinning happens
when the worker is registered, deliberately, not whenever it first answers.

The request body can carry a registration token, which is why nothing is sent
until all three checks have passed.
"""
import http.client
import json
import ssl
import uuid
from urllib.parse import urlsplit

from . import audit
from .ca import CONTROLLER_SUBJECT, fingerprint

#: Mirrors `agent.protocol`. A dashboard test reads that file and fails on any
#: difference; the two deployables cannot import each other.
PROTOCOL_MAJOR = 1
VERB_NAMES = frozenset({
    "hello", "capabilities",
    "exec_unit.create", "exec_unit.start", "exec_unit.stop",
    "exec_unit.restart", "exec_unit.remove", "exec_unit.status",
    "exec_unit.telemetry", "exec_unit.logs", "exec_unit.probe",
    "exec_unit.clear_cache", "exec_unit.drain", "exec_unit.cancel_drain",
    "runner.register", "runner.deregister",
})
OP_PATH = "/v1/op/"
#: Mirrors `agent.protocol.ASYNC_VERBS`, checked by test. These are answered
#: 202 with a handle; `call_and_wait` is how to get their outcome.
ASYNC_VERBS = frozenset({
    "exec_unit.create", "exec_unit.start", "exec_unit.stop",
    "exec_unit.restart", "exec_unit.remove", "exec_unit.clear_cache",
    "exec_unit.drain", "exec_unit.cancel_drain",
    "runner.register", "runner.deregister",
})

# The reasons a call is refused. Distinct on purpose; see the module docstring.
PLAIN_HTTP = "plain-http"
NOT_PINNED = "not-pinned"
UNKNOWN_WORKER = "unknown-worker"
UNKNOWN_VERB = "unknown-verb"
WRONG_CA = "wrong-ca"
WRONG_SUBJECT = "wrong-subject"
FINGERPRINT_CHANGED = "fingerprint-changed"
REFUSED_BY_AGENT = "refused-by-agent"
#: Per-verb authorization (T-0403), checked twice: the controller's policy
#: before anything is sent, then the worker's own on arrival. Two reasons, so
#: the audit trail says which side said no.
NOT_PERMITTED = "not-permitted"
NOT_PERMITTED_BY_AGENT = "not-permitted-by-agent"
#: Design 17.3: no destructive verb is dispatched to a worker that is not
#: healthy. Unreachable and absent look the same from here, and acting on that
#: evidence deletes capacity that was only out of touch. Enforced here as well
#: as in the reconciler, so it holds for every caller rather than one.
WORKER_NOT_HEALTHY = "worker-not-healthy"
#: T-0406. The agent does not speak the controller's protocol major. The worker
#: is marked degraded with both versions in the reason, and nothing is guessed:
#: an older agent and a newer one are refused the same way, visibly.
PROTOCOL_MISMATCH = "protocol-mismatch"
DESTRUCTIVE_VERBS = frozenset({"exec_unit.stop", "exec_unit.restart",
                               "exec_unit.remove", "exec_unit.clear_cache",
                               # Takes a runner out of service, if gently.
                               "exec_unit.drain",
                               "runner.deregister"})


class AgentRefused(Exception):
    """The call was not made, or was not let through, and this is why."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class AgentUnreachable(Exception):
    """Nothing answered. Distinct from a refusal: this proves nothing about
    who is at the address, only that nobody was."""


class AgentError(Exception):
    """The right agent answered, and said no."""

    def __init__(self, status, error):
        super().__init__(f"{status}: {error}")
        self.status = status
        self.error = error


def _common_name(peer_cert):
    for rdn in (peer_cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class AgentClient:
    def __init__(self, inventory, cert_file, key_file, ca_file,
                 audit_path=None, timeout=10):
        self.inventory = inventory
        self.timeout = timeout
        self.audit_path = audit_path or inventory.path
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # The agent is identified by subject and pinned fingerprint below; a
        # hostname match would prove only where DNS pointed.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(cert_file, key_file)
        context.load_verify_locations(ca_file)
        self._context = context

    # ---- refusing ----------------------------------------------------------

    def _refuse(self, host_id, verb, reason, detail=""):
        audit.record(self.audit_path, verb=verb,
                     decision=f"refused: {reason}",
                     parameters={"host_id": host_id, "detail": detail})
        raise AgentRefused(reason, detail)

    # ---- connecting --------------------------------------------------------

    def _worker(self, host_id, verb):
        worker = self.inventory.get(host_id)
        if worker is None:
            self._refuse(host_id, verb, UNKNOWN_WORKER, "not in the inventory")
        endpoint = urlsplit(worker.get("endpoint") or "")
        if endpoint.scheme != "https":
            self._refuse(host_id, verb, PLAIN_HTTP,
                         f"endpoint scheme is {endpoint.scheme or 'missing'}")
        if not worker.get("certificate_fingerprint"):
            self._refuse(host_id, verb, NOT_PINNED,
                         "no certificate fingerprint is pinned for it")
        return worker, endpoint

    def _connect(self, host_id, verb, worker, endpoint, timeout):
        conn = http.client.HTTPSConnection(
            endpoint.hostname, endpoint.port or 443, context=self._context,
            timeout=timeout)
        try:
            conn.connect()
        except ssl.SSLCertVerificationError as e:
            conn.close()
            self._refuse(host_id, verb, WRONG_CA,
                         e.verify_message or "certificate did not verify")
        except ssl.SSLError as e:
            conn.close()
            self._refuse(host_id, verb, REFUSED_BY_AGENT, type(e).__name__)
        except OSError as e:
            conn.close()
            raise AgentUnreachable(f"{host_id}: {type(e).__name__}") from None

        subject = _common_name(conn.sock.getpeercert())
        if subject != host_id:
            conn.close()
            self._refuse(host_id, verb, WRONG_SUBJECT,
                         f"certificate names {subject!r}")
        seen = fingerprint(conn.sock.getpeercert(binary_form=True))
        if seen != worker["certificate_fingerprint"]:
            conn.close()
            self._refuse(host_id, verb, FINGERPRINT_CHANGED,
                         f"presented {seen[:23]}...")
        return conn

    # ---- calling -----------------------------------------------------------

    def call(self, host_id, verb, body=None, idempotency_key=None,
             operation_id=None, timeout=None):
        """Ask one agent for one verb. Returns the verb's result.

        Raises AgentRefused when identity or policy stops the call - recorded
        in the audit trail - AgentUnreachable when nothing answers, and
        AgentError when the right agent answered with a refusal of its own.
        """
        if verb not in VERB_NAMES:
            self._refuse(host_id, verb, UNKNOWN_VERB, "not a protocol verb")
        worker, endpoint = self._worker(host_id, verb)
        # The controller's own policy for this worker, before a connection is
        # opened. The worker checks its own again when the request arrives.
        if verb not in self.inventory.permitted_verbs(host_id):
            self._refuse(host_id, verb, NOT_PERMITTED,
                         "not in this worker's permitted verbs")
        if verb in DESTRUCTIVE_VERBS:
            health = self.inventory.health(host_id)
            if health != "healthy":
                self._refuse(host_id, verb, WORKER_NOT_HEALTHY,
                             f"the worker is {health}")
        conn = self._connect(host_id, verb, worker, endpoint,
                             timeout or self.timeout)
        payload = json.dumps(body or {}).encode()
        headers = {"Content-Type": "application/json",
                   "X-Protocol-Version": str(PROTOCOL_MAJOR),
                   "Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        if operation_id:
            headers["X-Operation-Id"] = operation_id
        try:
            conn.request("POST", OP_PATH + verb, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
        except (ssl.SSLError, ConnectionError, http.client.HTTPException) as e:
            # Under TLS 1.3 an agent that rejects our certificate does so
            # after we consider the handshake done, so it surfaces here.
            self._refuse(host_id, verb, REFUSED_BY_AGENT, type(e).__name__)
        finally:
            conn.close()
        try:
            answer = json.loads(data or b"{}")
        except ValueError:
            raise AgentError(response.status, "the answer was not JSON")
        if response.status == 403:
            self._refuse(host_id, verb, NOT_PERMITTED_BY_AGENT,
                         answer.get("error", ""))
        if response.status == 426:
            self._mismatch(host_id, verb, answer.get("error", ""))
        if response.status >= 300:
            raise AgentError(response.status, answer.get("error", ""))
        if response.status == 202:
            # Accepted, not done: the handle and the state so far.
            return answer
        result = answer.get("result", answer)
        if verb == "hello" and isinstance(result, dict) and \
                result.get("protocol_major") != PROTOCOL_MAJOR:
            # An agent that let the request through but says it speaks
            # another major is believed about the major, not about letting
            # it through.
            self._mismatch(host_id, verb,
                           f"agent reports protocol "
                           f"{result.get('protocol_major')}")
        return result

    def _mismatch(self, host_id, verb, detail):
        reason = (f"protocol mismatch: controller speaks {PROTOCOL_MAJOR}; "
                  f"{detail}")
        try:
            self.inventory.mark_degraded(host_id, reason)
        except Exception:                   # noqa: BLE001
            pass
        self._refuse(host_id, verb, PROTOCOL_MISMATCH, detail)

    def call_and_wait(self, host_id, verb, body=None, operation_id=None,
                      idempotency_key=None, deadline=300, poll=1.0,
                      operations=None, sleep=None, clock=None):
        """Ask for a verb and return its outcome, however long it takes.

        A read comes back at once. An asynchronous verb comes back 202, and
        this then waits: for the agent's event, when `operations` is given and
        the event arrives, or by asking again with the same Idempotency-Key -
        which returns the state so far and never starts the work twice. So a
        lost event delays the answer and cannot lose it.

        No single request stays open longer than the fast timeout; the
        waiting happens here, between requests, bounded by `deadline`.
        """
        import time
        sleep = sleep or time.sleep
        clock = clock or time.monotonic
        key = idempotency_key or str(uuid.uuid4())
        answer = self.call(host_id, verb, body, idempotency_key=key,
                           operation_id=operation_id)
        if not (isinstance(answer, dict) and answer.get("accepted")):
            return answer
        handle = answer["handle"]
        give_up = clock() + deadline
        while True:
            state, outcome = answer.get("state"), answer
            if operations is not None and operation_id:
                heard = operations.call_state(operation_id, handle)
                if heard and heard["state"] != "running":
                    state, outcome = heard["state"], heard
            if state == "succeeded":
                return outcome.get("result") or {}
            if state == "failed":
                raise AgentError(500, outcome.get("error", "failed"))
            if clock() >= give_up:
                raise TimeoutError(
                    f"{verb} on {host_id} not finished within {deadline}s")
            sleep(poll)
            try:
                answer = self.call(host_id, verb, body, idempotency_key=key,
                                   operation_id=operation_id)
            except (TimeoutError, AgentUnreachable) as not_now:
                # Asking again is not the work - the agent is doing that -
                # so a poll that did not answer says nothing about the verb.
                # On a worker whose disk was saturated by its own runners a
                # poll timed out after ten seconds, and a create that went
                # on to finish was reported as a failure two minutes in
                # (2026-09-20). The deadline above still ends it.
                answer = {"accepted": True, "handle": handle,
                          "state": "running", "unanswered": str(not_now)}
