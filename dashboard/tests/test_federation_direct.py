import json
import multiprocessing
import queue
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path

import pytest

from control.federation import Federation, Refused, run_events
from control.federation_broker import Broker
from control.federation_direct import Direct, http_json, origin
from store import schema
from tests.test_federation import LocalProvisioner
from tests.test_federation_outbound import outgoing  # noqa: F401
from tests.test_controller_dashboard_integration import plane  # noqa: F401


def incremental_tls_exchange(endpoint, path, credential, body):
    """Spawn-importable test transport; production public/TLS policy is untouched."""
    import control.federation_direct as module
    from tests.federation_http_harness import RoutedTLS
    def connection(host, address):
        owned = RoutedTLS(host, body["port"], body["certificate"])
        owned.timeout = .2
        return owned
    module.PinnedHTTPS = connection
    module.public_addresses = lambda host: ["93.184.216.34"]
    return module._http_exchange(endpoint, path, credential, body)


def stalled_dns_exchange(endpoint, path, credential, body):
    import control.federation_direct as module
    def stalled(host):
        Path(body["phase_file"]).write_text("dns-entered", encoding="utf-8")
        threading.Event().wait(60)
        raise RuntimeError()
    module.public_addresses = stalled
    return module._http_exchange(endpoint, path, credential, body)


def large_private_exchange(endpoint, path, credential, body):
    assert len(json.dumps(body)) > 32000
    return b'{"pad":"' + b"x" * 32740 + b'"}'


def diagnostic_failure_exchange(endpoint, path, credential, body):
    assert body is None
    print(credential)
    raise RuntimeError(credential)


class IncrementalTLSServer:
    def __init__(self, certificate, private, phase):
        self.stop, self.entered, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private)
        def serve():
            try:
                raw, _ = self.listener.accept()
                raw.settimeout(3)
                with context.wrap_socket(raw, server_side=True) as connection:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        part = connection.recv(4096)
                        if not part:
                            return
                        request += part
                    self.entered.set()
                    body = b'{"pad":"' + b"x" * 128 + b'"}'
                    headers = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
                    for kind, data in (("headers", headers), ("body", body)):
                        if phase != kind:
                            connection.sendall(data)
                            continue
                        for byte in data:
                            connection.sendall(bytes([byte]))
                            if self.stop.wait(.04):
                                return
            except (OSError, ssl.SSLError):
                pass
            finally:
                self.finished.set()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(4)
        assert not self.thread.is_alive()


def audited_spawn(monkeypatch):
    """Observe only the exact test-owned process, never enumerate other PIDs."""
    import control.federation_direct as module
    real = multiprocessing.get_context("spawn")
    closed = []
    class Process:
        def __init__(self, **kwargs):
            assert len(kwargs["args"]) == 2  # No credential or payload in spawn arguments.
            self.owned = real.Process(**kwargs)
        def __getattr__(self, name):
            return getattr(self.owned, name)
        def close(self):
            assert self.owned.exitcode is not None
            closed.append((self.owned.pid, self.owned.exitcode))
            self.owned.close()
    class Context:
        Pipe = staticmethod(real.Pipe)
    Context.Process = Process
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda method: Context())
    return closed


@pytest.fixture
def direct_world(outgoing, tmp_path):
    source, actor, _, _, _, broker_calls, intent = outgoing
    now = [int(time.time())]
    path = str(tmp_path / "receiver.db")
    schema.init(path)
    adapter = LocalProvisioner()
    receiver = Federation(path, adapter, clock=lambda: now[0])
    limits = {"max_runners": 3, "max_lease_minutes": 90, "profiles": ["github-linux-x64"],
              "organizations": ["Allowed-Org"], "runner_groups": ["Lent-Restricted"], "automatic": False, "quiet_hours": []}
    peer, token = receiver.add_peer("Source", "independent-owner", limits, "receiving-owner")
    Broker(source.path).configure({"enabled": False, "issuer": "https://api.nomercy.tv", "fleet_id": str(uuid.uuid4())})
    calls = []
    def wire(endpoint, operation, offered, body=None):
        calls.append((endpoint, operation, offered, body))
        assert offered != "owner-account-secret"
        if endpoint == "https://receiver.example.com":
            assert receiver.authenticate(offered)["id"] == peer
            if operation == "capacity":
                return {"capacity": receiver.capacity(peer)}
            assert operation == "requests"
            results.validate_receipt(body["request_key"], body["receipt"])
            row = receiver.submit(peer, body["request_key"], body["intent"], body["credential"], receipt=body["receipt"])
            return {"request": row}
        assert endpoint == "https://source.example.com" and operation == "results"
        identity = direct.authenticate(offered)
        assert identity
        direct.receive(identity, body)
        return {"received": True}
    direct = Direct(source.path, source.service, clock=lambda: now[0], http=wire)
    results = Direct(path, clock=lambda: now[0], http=wire)
    target = direct.add(actor, "Receiver", "https://receiver.example.com", token, now[0] + 3600)
    return direct, results, receiver, adapter, now, actor, target, token, intent, calls, broker_calls


def submit(world):
    direct, _, _, _, _, actor, target, _, intent, _, _ = world
    return direct.submit(actor, target, intent, "Lent-Restricted", "https://source.example.com")


def test_two_independent_fleets_push_approval_completion_without_broker_or_status_polling(direct_world):
    direct, results, receiver, adapter, now, actor, _, token, intent, calls, broker_calls = direct_world
    key = submit(direct_world)
    row = receiver.requests()[0]
    results.publish()
    assert direct.results(actor)[0]["state"] == "pending"
    count = len(calls)
    results.publish()
    assert len(calls) == count  # Unchanged state causes no network request.
    receiver.decide(row["id"], True, "receiving-owner")
    receiver._tick()
    results.publish()
    assert direct.results(actor)[0]["state"] == "active"
    assert adapter.calls[0][1] == {"fleet_id": intent["profile"], "cpus": 8, "memory_bytes": 16 * 1024 ** 3,
                                  "disk_bytes": 100 * 1024 ** 3, "cache_bytes": 40 * 1024 ** 3}
    now[0] += intent["lease_minutes"] * 60
    receiver._tick()
    results.publish()
    assert direct.results(actor)[0]["state"] == "expired"
    assert direct.results(actor)[0]["request_key"] == key and not broker_calls
    assert all(operation in {"capacity", "requests", "results"} for _, operation, _, _ in calls)
    public = json.dumps([direct.targets(actor), direct.results(actor), receiver.peers(), receiver.requests(), receiver.activity()])
    receipt = calls[1][3]["receipt"]["credential"]
    assert token not in public and receipt not in public and "A" * 32 not in public and "owner-account-secret" not in public
    with schema.connect(direct.path) as c:
        assert token.encode() not in c.execute("SELECT sealed_credential FROM federation_direct_targets").fetchone()[0]
    with schema.connect(results.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_direct_receipts").fetchone()[0] is None


@pytest.mark.parametrize("change", [{"count": True}, {"count": 101}, {"lease_minutes": 7201}, {"organization": "Other"}, {"commands": ["sudo"]}, {"profile": "github-macos-x64"}])
def test_direct_requests_reuse_restricted_registration_rules(direct_world, change):
    direct, _, receiver, _, _, actor, target, _, intent, calls, _ = direct_world
    with pytest.raises(Refused):
        direct.submit(actor, target, dict(intent, **change), "Lent-Restricted", "https://source.example.com")
    assert not receiver.requests() and not direct.results(actor)
    assert all(operation == "capacity" for _, operation, _, _ in calls)


def test_connection_owner_expiry_and_disconnect_do_not_grant_remote_admin(direct_world):
    direct, _, _, _, now, actor, target, token, _, _, _ = direct_world
    with pytest.raises(Refused):
        direct._target("another-owner", target)
    with pytest.raises(Refused):
        direct.disconnect("another-owner", target)
    assert not direct.authenticate(token)
    direct.disconnect(actor, target)
    with pytest.raises(Refused):
        submit(direct_world)
    assert not direct.targets(actor)[0]["enabled"]
    target = direct.add(actor, "Expiring", "https://receiver.example.com", token, now[0] + 7)
    assert Federation(direct.path, clock=lambda: now[0]).next_deadline() == now[0] + 7
    now[0] += 7
    direct.publish()
    with schema.connect(direct.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_direct_targets WHERE id=?", (target,)).fetchone()[0] is None


def test_results_are_one_request_only_ordered_and_terminal_fenced(direct_world):
    direct, results, receiver, _, _, actor, _, _, _, calls, _ = direct_world
    key = submit(direct_world)
    token = calls[-1][3]["receipt"]["credential"]
    identity = direct.authenticate(token)
    body = {"request_key": key, "receiver_request_id": receiver.requests()[0]["id"], "state": "active", "sequence": 2}
    direct.receive(identity, body)
    direct.receive(identity, dict(body, state="pending", sequence=1))
    assert direct.results(actor)[0]["state"] == "active"
    with pytest.raises(Refused):
        direct.receive(identity, dict(body, request_key=str(uuid.uuid4())))
    with pytest.raises(Refused):
        direct.receive(identity, dict(body, receiver_request_id=str(uuid.uuid4()), sequence=3))
    direct.receive(identity, dict(body, state="expired", sequence=3))
    with pytest.raises(Refused, match="terminal"):
        direct.receive(identity, dict(body, sequence=4))
    assert direct.authenticate(token + "x") is None
    assert not Broker(direct.path).settings()["enabled"]


def test_accepted_receipt_cannot_be_retargeted_and_duplicate_is_idempotent(direct_world):
    _, results, receiver, _, _, _, _, _, _, calls, _ = direct_world
    submit(direct_world)
    row, receipt = receiver.requests()[0], calls[-1][3]["receipt"]
    results.receipt(row, receipt)
    for change in ({"origin": "https://another.example.com"}, {"credential": receipt["credential"][:-1] + "X"}, {"expires_at": receipt["expires_at"] - 1}):
        with pytest.raises(Refused):
            results.receipt(row, dict(receipt, **change))
    with schema.connect(results.path) as c:
        assert c.execute("SELECT COUNT(*) FROM federation_direct_receipts").fetchone()[0] == 1


def test_receipt_failure_rolls_back_intent_and_activity_before_automatic_approval(direct_world, monkeypatch):
    _, _, receiver, _, now, _, _, token, intent, _, _ = direct_world
    peer = receiver.authenticate(token)["id"]
    key = str(uuid.uuid4())
    receipt = {"origin": "https://source.example.com", "credential": "nrr_" + key + "." + "a" * 43, "expires_at": now[0] + 300}
    events = receiver.activity()
    def fail(*args, **kwargs):
        raise Refused("Unable to persist a private result receipt")
    monkeypatch.setattr(Direct, "receipt", fail)
    with pytest.raises(Refused, match="persist"):
        receiver.submit(peer, key, intent, {"registration_token": "A" * 32, "expires_at": now[0] + 300,
                        "runner_group": "Lent-Restricted"}, receipt=receipt)
    assert receiver.requests() == [] and receiver.activity() == events
    with schema.connect(receiver.path) as c:
        assert c.execute("SELECT COUNT(*) FROM federation_direct_receipts").fetchone()[0] == 0


def test_automatic_receiver_approval_ack_remains_pending_until_allocation_event(direct_world):
    direct, results, receiver, _, _, actor, _, token, _, _, _ = direct_world
    peer = receiver.authenticate(token)
    receiver.set_policy(peer["id"], dict(peer["policy"], automatic=True), "receiving-owner")
    submit(direct_world)
    assert receiver.requests()[0]["state"] == "approved"
    assert direct.results(actor)[0]["state"] == "pending"
    receiver._tick()
    results.publish()
    assert direct.results(actor)[0]["state"] == "active"


def test_disconnect_during_preflight_prevents_recording_and_sending_new_intent(direct_world):
    direct, _, receiver, _, _, actor, target, _, _, calls, _ = direct_world
    real = direct.http
    def disconnect(endpoint, operation, credential, body=None):
        reply = real(endpoint, operation, credential, body)
        if operation == "capacity":
            direct.disconnect(actor, target)
        return reply
    direct.http = disconnect
    with pytest.raises(Refused, match="changed"):
        submit(direct_world)
    assert direct.results(actor) == [] and receiver.requests() == []
    assert [operation for _, operation, _, _ in calls] == ["capacity"]


def test_ready_delivery_batch_yields_to_lease_convergence_with_exact_continuation(direct_world):
    direct, results, receiver, _, now, actor, _, _, _, calls, _ = direct_world
    submit(direct_world)
    submit(direct_world)
    assert len(calls) == 4
    assert results.next_deadline() == now[0]
    results.publish()
    assert len(calls) == 5 and results.next_deadline() == now[0]
    results.publish()
    assert len(calls) == 6 and results.next_deadline() > now[0]
    results.publish()
    assert len(calls) == 6  # No ready work means no transport request.
    assert all(row["state"] == "pending" and row["sequence"] == 1 for row in direct.results(actor))


def test_controller_native_event_loop_drains_ready_results_without_status_polling(direct_world, monkeypatch):
    direct, results, receiver, _, now, actor, _, _, _, calls, _ = direct_world
    keys = {submit(direct_world), submit(direct_world)}
    delivered, ready, stop = queue.Queue(), threading.Event(), threading.Event()
    failures = []
    real_publish, real_http = Direct.publish, results.http
    def http(endpoint, operation, token, body=None):
        reply = real_http(endpoint, operation, token, body)
        if operation == "results":
            delivered.put(body["request_key"])
        return reply
    def publish(self):
        self.http = http
        real_publish(self)
    monkeypatch.setattr(Direct, "publish", publish)
    def run():
        try:
            run_events(receiver, stop, ready.set)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        assert {delivered.get(timeout=10), delivered.get(timeout=10)} == keys
        assert all(row["state"] == "pending" for row in direct.results(actor))
    finally:
        stop.set()
        # A database write wakes the OS watcher; shutdown needs no timer poll.
        with schema.connect(receiver.path) as c:
            c.execute("INSERT INTO platform_settings(key,value) VALUES('direct-test-shutdown','true')")
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert [operation for _, operation, _, _ in calls].count("results") == 2


def test_delivery_retry_uses_exact_deadline_and_expiry_purges_private_receipt(direct_world):
    direct, results, receiver, _, now, _, _, _, _, calls, _ = direct_world
    submit(direct_world)
    def fail(*args):
        raise RuntimeError("sensitive transport error")
    real = results.http
    results.http = fail
    results.publish()
    assert results.next_deadline() == now[0] + 5
    results.publish()
    assert len(calls) == 2
    now[0] += 5
    results.http = real
    results.publish()
    assert len(calls) == 3
    with schema.connect(results.path) as c:
        expires = c.execute("SELECT expires_at FROM federation_direct_receipts").fetchone()[0]
    now[0] = expires
    results.publish()
    with schema.connect(results.path) as c:
        row = c.execute("SELECT sealed_credential,next_retry_at FROM federation_direct_receipts").fetchone()
        assert tuple(row) == (None, None)
    assert direct.authenticate(calls[-1][2]) is None


def test_failed_slow_delivery_schedules_retry_after_completion_not_request_start(direct_world):
    _, results, _, _, now, _, _, _, _, _, _ = direct_world
    submit(direct_world)
    attempts = []
    def slow_failure(*args):
        attempts.append(now[0])
        now[0] += 10
        raise TimeoutError()
    results.http = slow_failure
    results.publish()
    assert results.next_deadline() == now[0] + 5
    results.publish()
    assert len(attempts) == 1


@pytest.mark.parametrize("result_before_ack", [False, True])
def test_lost_intent_ack_preserves_accepted_request_and_reconciles_from_result(direct_world, result_before_ack):
    direct, results, receiver, _, _, actor, _, _, _, _, _ = direct_world
    real = direct.http
    def lost_ack(endpoint, operation, offered, body=None):
        reply = real(endpoint, operation, offered, body)
        if operation == "requests":
            if result_before_ack:
                results.publish()
            raise TimeoutError("private token and transport details must not be exposed")
        return reply
    direct.http = lost_ack
    with pytest.raises(Refused, match="not acknowledged") as error:
        submit(direct_world)
    assert "private token" not in str(error.value) and len(receiver.requests()) == 1
    assert direct.results(actor)[0]["state"] == ("pending" if result_before_ack else "delivery-unknown")
    results.publish()
    assert direct.results(actor)[0]["state"] == "pending"


def test_receipt_expiry_auth_is_rechecked_before_result_projection(direct_world):
    direct, _, receiver, _, now, _, _, _, _, calls, _ = direct_world
    key = submit(direct_world)
    token = calls[-1][3]["receipt"]["credential"]
    identity = direct.authenticate(token)
    now[0] = calls[-1][3]["receipt"]["expires_at"]
    assert direct.authenticate(token) is None
    with pytest.raises(Refused, match="expired"):
        direct.receive(identity, {"request_key": key, "receiver_request_id": receiver.requests()[0]["id"], "state": "active", "sequence": 1})


@pytest.mark.parametrize("endpoint", ["http://peer.example.com", "https://u:p@peer.example.com", "https://peer.example.com/path", "https://peer.example.com:444", "https://127.0.0.1", "https://[::1]", "https://peer.example.com?token=secret"])
def test_endpoint_contract_rejects_credentials_paths_ports_and_literal_addresses(endpoint):
    with pytest.raises(Refused):
        origin(endpoint)


def test_transport_pins_public_dns_and_refuses_redirect_or_oversized_response(monkeypatch):
    import control.federation_direct as module
    import control.federation_broker as broker
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    calls = []
    class Connection:
        status = 302
        def __init__(self, host, address):
            calls.append((host, address))
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
        def getresponse(self):
            return self
        def read(self, bound):
            return b"x" * bound
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(module, "PinnedHTTPS", Connection)
    monkeypatch.setattr(broker.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(Refused, match="public"):
        module._http_exchange("https://peer.example.com", "capacity", token)
    assert not calls
    monkeypatch.setattr(broker.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443))])
    with pytest.raises(Refused):
        module._http_exchange("https://peer.example.com", "capacity", token)
    assert calls[0] == ("peer.example.com", "8.8.8.8") and calls[-1] == "closed"
    Connection.status = 200
    with pytest.raises(Refused, match="32 KiB"):
        module._http_exchange("https://peer.example.com", "capacity", token)
    for credential, operation in (("owner-account-secret", "capacity"), (token, "results"), (token, "../../runners")):
        with pytest.raises(Refused):
            http_json("https://peer.example.com", operation, credential)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_exchange_deadline_stops_incremental_tls_and_reaps_owned_child(tmp_path, monkeypatch, phase):
    import control.federation_direct as module
    from tests.federation_http_harness import SOURCE, certificates
    certificate, private = certificates(tmp_path)
    server = IncrementalTLSServer(certificate, private, phase)
    closed = audited_spawn(monkeypatch)
    monkeypatch.setattr(module, "_http_exchange", incremental_tls_exchange)
    monkeypatch.setattr(module, "EXCHANGE_TIMEOUT", 2)
    token = "nrr_" + str(uuid.uuid4()) + "." + "a" * 43
    started = time.monotonic()
    try:
        with pytest.raises(Refused, match="total deadline"):
            module.http_json(SOURCE, "results", token, {"port": server.port, "certificate": certificate})
        assert time.monotonic() - started < 3.5
        assert server.entered.is_set()  # The request reached real verified TLS.
        assert len(closed) == 1 and closed[0][0] > 0  # Native child exit proved before return.
        assert server.finished.wait(1)  # Termination closed its exact socket too.
        assert module._unreaped_process is None
    finally:
        server.close()


def test_absolute_exchange_deadline_includes_stalled_dns_and_closes_worker(tmp_path, monkeypatch):
    import control.federation_direct as module
    closed = audited_spawn(monkeypatch)
    monkeypatch.setattr(module, "_http_exchange", stalled_dns_exchange)
    monkeypatch.setattr(module, "EXCHANGE_TIMEOUT", 2)
    token = "nrr_" + str(uuid.uuid4()) + "." + "a" * 43
    phase_file = tmp_path / "owned-dns-phase"
    started = time.monotonic()
    with pytest.raises(Refused, match="total deadline"):
        module.http_json("https://peer.example.com", "results", token, {"phase_file": str(phase_file)})
    assert time.monotonic() - started < 3.5
    assert phase_file.read_text() == "dns-entered" and len(closed) == 1
    assert module._unreaped_process is None


def test_maximum_private_ipc_completes_and_reaps_before_next_exchange(monkeypatch):
    import control.federation_direct as module
    closed = audited_spawn(monkeypatch)
    monkeypatch.setattr(module, "_http_exchange", large_private_exchange)
    token = "nrr_" + str(uuid.uuid4()) + "." + "a" * 43
    for _ in range(2):
        reply = module.http_json("https://peer.example.com", "results", token, {"pad": "x" * 32700})
        assert len(reply["pad"]) == 32740
    assert len(closed) == 2 and closed[0][0] != closed[1][0]
    assert all(exitcode == 0 for _, exitcode in closed)


def test_body_none_worker_failure_keeps_credentials_out_of_parent_errors_and_logs(monkeypatch, capsys):
    import control.federation_direct as module
    closed = audited_spawn(monkeypatch)
    monkeypatch.setattr(module, "_http_exchange", diagnostic_failure_exchange)
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    with pytest.raises(Refused, match="not acknowledged") as error:
        module.http_json("https://peer.example.com", "capacity", token)
    assert token not in str(error.value) and len(closed) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_queued_exchange_wait_has_the_same_absolute_deadline(monkeypatch):
    import control.federation_direct as module
    monkeypatch.setattr(module, "EXCHANGE_TIMEOUT", .05)
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    module._exchange_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(Refused, match="total deadline"):
            module.http_json("https://peer.example.com", "capacity", token)
        assert time.monotonic() - started < .3
    finally:
        module._exchange_lock.release()


def test_unreaped_owned_transport_fences_progress_and_all_later_exchanges(monkeypatch):
    import control.federation_direct as module
    monkeypatch.setattr(module, "_unreaped_process", None)
    class Owned:
        pid, exitcode = 123, None
        def __init__(self):
            self.actions = []
        def join(self, timeout):
            self.actions.append(("join", timeout))
        def terminate(self):
            self.actions.append("terminate")
        def kill(self):
            self.actions.append("kill")
        def close(self):
            raise AssertionError("Must retain the unreaped owned handle")
    owned = Owned()
    with pytest.raises(module.TransportCleanupFailure):
        module._reap_exchange(owned)
    assert module._unreaped_process is owned and "terminate" in owned.actions and "kill" in owned.actions
    with pytest.raises(Refused, match="prior owned"):
        module._bounded_exchange(b"{}", large_private_exchange, 1)


def test_unreaped_delivery_exits_expiry_worker_instead_of_scheduling_overlap(direct_world):
    import control.federation_direct as module
    _, results, _, _, _, _, _, _, _, _, _ = direct_world
    submit(direct_world)
    def unreaped(*args):
        raise module.TransportCleanupFailure("Owned transport is not stopped")
    results.http = unreaped
    with pytest.raises(module.TransportCleanupFailure):
        results.publish()
    with schema.connect(results.path) as c:
        row = c.execute("SELECT attempts,next_retry_at,last_state FROM federation_direct_receipts").fetchone()
        assert tuple(row) == (0, None, None)


def test_owner_screen_csrf_and_result_credential_cannot_inspect_or_operate_runners(client, plane):
    client.get("/federation")
    with client.session_transaction() as session:
        csrf, actor = session["federation_csrf"], session["sub"]
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    body = {"name": "Receiver", "origin": "https://receiver.example.com", "credential": token, "expires_at": int(time.time()) + 60}
    assert client.post("/api/v2/federation/direct/targets", json=body).status_code == 403
    response = client.post("/api/v2/federation/direct/targets", json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201 and token not in response.get_data(as_text=True)
    assert token not in client.get("/api/v2/federation").get_data(as_text=True)
    credential = "nrr_" + str(uuid.uuid4()) + "." + "a" * 43
    for route, method in (("/api/v2/runners", "GET"), ("/api/v2/federation/peer/capacity", "GET"), ("/api/v2/federation/peer/results", "GET")):
        response = client.open(route, method=method, headers={"Authorization": "Bearer " + credential})
        assert response.status_code == 403
    assert client.post("/api/v2/federation/peer/results", json={}, headers={"Authorization": "Bearer " + token}).status_code == 401


def test_two_dashboard_http_contract_accepts_only_scoped_bearers_and_valid_receipts(direct_world, monkeypatch):
    import app
    import api_v2
    import federation_api
    direct, results, receiver, _, now, actor, _, token, intent, _, _ = direct_world
    with schema.connect(receiver.path) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_runtime','running')")
    monkeypatch.setattr(api_v2, "controller_health", lambda path: {"running": True})
    monkeypatch.setattr(federation_api, "adapter_factory", lambda path: receiver.adapter if path == receiver.path else None)
    client = app.app.test_client(use_cookies=False)
    calls = []
    def wire(endpoint, operation, offered, body=None):
        path = receiver.path if endpoint == "https://receiver.example.com" else direct.path
        with monkeypatch.context() as scoped:
            scoped.setattr(api_v2, "_db_path", lambda: path)
            response = client.open("/api/v2/federation/peer/" + operation, method="GET" if body is None else "POST",
                                   json=body, headers={"Authorization": "Bearer " + offered})
        calls.append((operation, response.status_code))
        assert response.status_code in (200, 202), response.get_json()
        return response.get_json()
    direct.http = results.http = wire
    key = submit(direct_world)
    results.publish()
    assert direct.results(actor)[0]["request_key"] == key and direct.results(actor)[0]["state"] == "pending"
    receiver.decide(receiver.requests()[0]["id"], False, "receiving-owner")
    results.publish()
    assert direct.results(actor)[0]["state"] == "denied"
    assert calls == [("capacity", 200), ("requests", 202), ("results", 200), ("results", 200)]
    with monkeypatch.context() as scoped:
        scoped.setattr(api_v2, "_db_path", lambda: receiver.path)
        body = {"request_key": str(uuid.uuid4()), "intent": intent,
                "credential": {"registration_token": "A" * 32, "expires_at": now[0] + 1800, "runner_group": "Lent-Restricted"}, "receipt": {}}
        response = client.post("/api/v2/federation/peer/requests", json=body, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 409
        assert len(receiver.requests()) == 1  # Invalid receipt failed before accepting another intent.
