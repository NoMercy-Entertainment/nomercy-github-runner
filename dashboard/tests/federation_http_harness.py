"""Test-only HTTPS courier and real dashboard subprocesses; no host adapters.

All connections are routed to explicitly registered loopback ports, while TLS
still verifies the logical origin with a temporary test CA. Production's public
DNS transport is never weakened. GitHub/agent observations are simulations.
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from control.federation_broker import canonical

ISSUER = "https://api.nomercy.tv"
SOURCE = "https://source.dashboard.test"
RECEIVER = "https://receiver.dashboard.test"
PROFILE = "github-linux-x64"
ORG = "Isolated-Test-Org"
GROUP = "Lent-Test-Restricted"


def certificates(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated federation test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(urlsplit(url).hostname)
                for url in (ISSUER, SOURCE, RECEIVER)]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate, private = Path(directory) / "test-ca.pem", Path(directory) / "test-tls.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(certificate), str(private)


class RoutedTLS(http.client.HTTPSConnection):
    def __init__(self, hostname, port, certificate):
        super().__init__(hostname, timeout=10, context=ssl.create_default_context(cafile=certificate))
        self.local_port = port

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Transport:
    def __init__(self, certificate, routes):
        self.certificate, self.routes = certificate, routes

    def open(self, url, method="GET", body=None, headers=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.routes or parsed.port or parsed.username:
            raise AssertionError("The isolated test attempted an unregistered network destination")
        connection = RoutedTLS(parsed.hostname, self.routes[parsed.hostname], self.certificate)
        headers = dict(headers or {})
        if body is not None:
            body = canonical(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body, headers)
        return connection, connection.getresponse()

    def json(self, url, method="GET", body=None, headers=None):
        connection, response = self.open(url, method, body, headers)
        try:
            data = json.loads(response.read())
            return response.status, data
        finally:
            connection.close()

    def courier(self, url, body=None, headers=None):
        status, data = self.json(url, "POST" if body is not None else "GET", body, headers)
        if not 200 <= status < 300:
            raise AssertionError(f"Isolated courier HTTP {status}: {data}")
        return data


class TestCourier:
    """Wire-contract fixture, not the production Laravel implementation."""
    __test__ = False

    def __init__(self, certificate, private, actors):
        self.routes, self.actors = {}, actors
        self.transport = Transport(certificate, self.routes)
        self.fleets, self.grants, self.requests, self.deliveries = {}, set(), {}, []
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.kid = str(uuid.uuid4())
        self.jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                    "n": self.b64(numbers.n.to_bytes(256, "big")), "e": self.b64(numbers.e.to_bytes(3, "big"))}
        app = Flask("isolated-courier")
        app.add_url_rule("/v1/runner-fleets/.well-known/jwks.json", "keys", lambda: jsonify(keys=[self.jwk]))
        app.add_url_rule("/v1/runner-fleets/register", "register", self.register, methods=["POST"])
        app.add_url_rule("/v1/runner-fleets", "directory", self.directory)
        app.add_url_rule("/v1/runner-fleets/<fleet>/grants", "grant", self.grant, methods=["POST"])
        app.add_url_rule("/v1/runner-fleets/<fleet>/requests", "submit", self.submit, methods=["POST"])
        app.add_url_rule("/v1/runner-fleets/receipts/<identity>", "receipt", self.receipt, methods=["POST"])
        self.server = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=(certificate, private))
        self.routes["api.nomercy.tv"] = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    def signed(self, actor, fleet, body, purpose):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": fleet, "sub": actor, "purpose": purpose,
                  "request_id": body["request_key"], "body_hash": hashlib.sha256(canonical(body)).hexdigest(),
                  "iat": now, "nbf": now - 5, "exp": now + 120, "jti": str(uuid.uuid4())}
        head = {"typ": "JWT", "alg": "RS256", "kid": self.kid}
        data = self.b64(canonical(head)) + "." + self.b64(canonical(claims))
        signature = self.key.sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())
        return "nmc_" + data + "." + self.b64(signature)

    def deliver(self, actor, fleet, body, purpose):
        suffix = {"registration": "challenge", "capacity": "requests", "result": "results"}[purpose]
        assertion = self.signed(actor, fleet["id"], body, purpose)
        self.deliveries.append((purpose, assertion, body))
        return self.transport.json(fleet["callback_origin"] + "/api/v2/federation/broker/" + suffix,
                                   "POST", body, {"Authorization": "Bearer " + assertion})

    def actor(self):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return self.actors.get(token)

    def register(self):
        actor, body = self.actor(), request.get_json()
        if not actor:
            return jsonify(error="unauthenticated"), 401
        fleet = dict(body, id=str(uuid.uuid4()), owner=actor, verified=True, verified_at="test-proof")
        challenge = {"request_key": str(uuid.uuid4()), "fleet_id": fleet["id"], "ownership_nonce": body["ownership_nonce"]}
        status, proof = self.deliver(actor, fleet, challenge, "registration")
        expected = hashlib.sha256((body["ownership_nonce"] + "|" + fleet["id"]).encode()).hexdigest()
        if status != 200 or proof.get("proof") != expected:
            return jsonify(error="ownership proof failed", detail=proof), 409
        self.fleets[fleet["id"]] = fleet
        return jsonify(data={"id": fleet["id"], "verified_at": fleet["verified_at"]})

    def directory(self):
        actor = self.actor()
        if not actor:
            return jsonify(error="unauthenticated"), 401
        return jsonify(data=[row for row in self.fleets.values() if row["owner"] == actor or (row["id"], actor) in self.grants])

    def grant(self, fleet):
        actor, body = self.actor(), request.get_json()
        if fleet not in self.fleets or self.fleets[fleet]["owner"] != actor:
            return jsonify(error="not owner"), 403
        self.grants.add((fleet, body["user_id"]))
        return jsonify(saved=True)

    def submit(self, fleet):
        actor, body = self.actor(), request.get_json()
        source = self.fleets.get(body["source_fleet_id"])
        if not source or source["owner"] != actor or (fleet, actor) not in self.grants:
            return jsonify(error="no routing permission"), 403
        identity, credential = str(uuid.uuid4()), "R" * 64
        envelope = {"request_key": identity, "intent": body["intent"], "credential": body["credential"],
                    "receipt": {"request_id": identity, "credential": credential, "expires_at": int(time.time()) + 7200}}
        record = dict(body, id=identity, actor=actor, source=source, target=self.fleets[fleet],
                      receipt=credential, envelope=envelope, receiver_request_id=None)
        self.requests[identity] = record
        status, reply = self.deliver(actor, record["target"], envelope, "capacity")
        if status != 202:
            return jsonify(error="receiver refused", detail=reply), 409
        record["receiver_request_id"] = reply["request"]["id"]
        return jsonify(request={"id": identity}), 202

    def receipt(self, identity):
        record, body = self.requests.get(identity), request.get_json()
        if not record or request.headers.get("X-Fleet-Receipt") != record["receipt"]:
            return jsonify(error="invalid receipt"), 403
        result = {"request_key": record["request_key"], "broker_request_id": identity,
                  "receiver_request_id": body["receiver_request_id"], "state": body["state"], "sequence": body["sequence"]}
        status, reply = self.deliver(record["actor"], record["source"], result, "result")
        if status != 200:
            return jsonify(error="result refused", detail=reply), 409
        return jsonify(received=True)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def dashboard(config):
    """Run an actual Flask app/native expiry worker with simulated forge I/O."""
    # The subprocess receives an allowlisted environment, not deployment secrets.
    from store import schema
    from store.fleets import FleetStore
    from control.federation import Federation, Refused, run_events
    from control.federation_provision import LocalProvisioner, registration_for, registration_consumed, retirement_safe
    from control.service import RunnerService
    import providers
    import api_v2
    import users
    import app as dashboard_app
    import federation_api
    import control.federation_broker as broker_module
    import control.federation_outbound as outbound_module
    from dbwatch import DatabaseChanges

    path = schema.DB_PATH
    schema.init(path)
    env = {"GITHUB_ORG": ORG, "GITHUB_DRAIN_GROUP": "Test-Closed-No-Jobs", "RUNNER_UNIT_IMAGE_GITHUB_LINUX": "test:guarded"}
    FleetStore(path).seed(env)
    service = RunnerService(path, env=env)
    service.inventory.register_worker("simulated-worker", "hyperv-linux")
    service.inventory.heartbeat("simulated-worker", capabilities={"kind": "linux-container", "builds_from": "image",
        "host_cores": 32, "memory_bytes": 64 * 1024**3, "architecture": "x64", "disk_quota": True,
        "disk_limit_enforced": True, "federation_fork_guard": True, "federation_templates": ["test:guarded"]})
    ordinary = service.specs.create(provider="github", platform="linux", fleet_id=PROFILE,
        host_id="simulated-worker", desired_state="running", actual_state="idle", runner_group="Fillz",
        cpu_limit="0-7", memory_limit=16 * 1024**3, disk_limit=100 * 1024**3)
    normal_before = service.specs.get(ordinary)
    fleets_before = service.fleets.list()

    class SimulatedForge:
        org = ORG
        members, busy, known, mint_count = [], False, True, 0

        def runner_groups(self):
            return [{"id": 3, "name": GROUP, "visibility": "selected", "allows_public_repositories": False,
                     "inherited": False, "default": False}]

        def _get(self, *args, **kwargs):
            return {"total_count": 1}

        def _pages(self, *args, **kwargs):
            return self.members

        def all_runners(self):
            return [dict(row, busy=self.busy, status="online") for row in self.members] if self.known else None

        def drain_group_closed(self, group, rid=None):
            return group == "Test-Closed-No-Jobs"

        def move_runner_to_group(self, group, rid):
            return self.drain_group_closed(group)

        def registration_token(self):
            assert config["origin"] == SOURCE, "Receiver attempted to mint its own registration credential"
            self.mint_count += 1
            return "A" * 32

    forge = SimulatedForge()
    adapter = LocalProvisioner(service, forge)
    plane = Federation(path, adapter)
    transport = Transport(config["certificate"], {"api.nomercy.tv": config["broker_port"]})
    original_broker, original_outbound = broker_module.Broker, outbound_module.Outbound

    class IsolatedBroker(original_broker):
        def __init__(self, path, clock=time.time, http=None):
            super().__init__(path, clock, http or transport.courier)

    class IsolatedOutbound(original_outbound):
        def __init__(self, path, service=None, clock=time.time, http=None):
            super().__init__(path, service, clock, http or transport.courier)

    broker_module.Broker = outbound_module.Broker = IsolatedBroker
    outbound_module.Outbound = IsolatedOutbound
    providers.by_key("github").forge_client = lambda env: forge
    api_v2.control_plane = lambda: (service, None)
    federation_api.adapter_factory = lambda path: adapter
    federation_api.public_origin_provider = lambda: config["origin"]
    users.sign_in(config["actor"], "test-owner", "Test owner")
    users.sign_in(config["peer_actor"], "known-person", "Known person")
    users.approve(config["peer_actor"], "viewer")
    IsolatedOutbound(path, service).retain_identity({"sub": config["actor"]},
        {"access_token": config["access"], "expires_in": 300})
    with schema.connect(path) as c:
        c.execute("INSERT INTO controller_status VALUES(1,?,'running',NULL)", (datetime.now(timezone.utc).isoformat(),))

    stop, ready = threading.Event(), threading.Event()

    def worker_ready():
        with schema.connect(path) as c:
            c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_runtime','running')")
        ready.set()

    worker = threading.Thread(target=run_events, args=(plane, stop, worker_ready), daemon=True)
    worker.start()
    assert ready.wait(5)
    server = make_server("127.0.0.1", 0, dashboard_app.app, threaded=True,
                         ssl_context=(config["certificate"], config["private"]))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    csrf = "isolated-csrf"
    cookie = dashboard_app.app.session_interface.get_signing_serializer(dashboard_app.app).dumps(
        {"sub": config["actor"], "federation_csrf": csrf})
    print(json.dumps({"port": server.server_port, "cookie": cookie, "csrf": csrf, "python_prefix": sys.prefix}), flush=True)

    def owned():
        with schema.connect(path) as c:
            return [service.specs.get(row[0]) for row in c.execute("SELECT runner_id FROM federation_allocations")]

    def wait_claim():
        watch = DatabaseChanges(path)
        try:
            deadline = time.monotonic() + 8
            while not owned():
                assert time.monotonic() < deadline, "Native convergence did not create a claim"
                watch.wait(max(0, deadline - time.monotonic()))
        finally:
            watch.close()

    def secret_counts():
        with schema.connect(path) as c:
            return {table: c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL").fetchone()[0]
                for table, column in (("federation_requests", "sealed_credential"), ("federation_allocations", "sealed_registration"),
                                      ("federation_receipts", "sealed_credential"))}

    try:
        for line in sys.stdin:
            command = json.loads(line)
            try:
                action = command["action"]
                if action == "stop":
                    break
                if action == "materialise":
                    wait_claim()
                    rows = owned()
                    for index, row in enumerate(rows):
                        if row["actual_state"] != "planned":
                            continue
                        plan = registration_for(service, row)
                        assert plan.token == "A" * 32 and plan.runner_group == GROUP
                        registration_consumed(service, row)
                        forge.members.append({"id": 100 + index, "name": providers._forge_name(row),
                                              "labels": [{"name": label} for label in row["labels"]]})
                        service.specs.update(row["runner_id"], row["spec_version"], actual_state="idle", registration_id=str(100 + index))
                        with schema.connect(path) as c:
                            c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?", (json.dumps({"origin_guard": {
                                "version": 1, "federation_guard": True, "at": datetime.now(timezone.utc).isoformat()}}), row["runner_id"]))
                elif action in ("busy", "idle", "unknown", "known"):
                    forge.busy = action == "busy" if action in ("busy", "idle") else forge.busy
                    forge.known = action == "known" if action in ("unknown", "known") else forge.known
                    for row in owned():
                        if row["actual_state"] != "absent":
                            service.specs.update(row["runner_id"], row["spec_version"], actual_state="busy" if forge.busy else "idle",
                                                 forge_state="busy" if forge.busy else "idle")
                elif action == "retire":
                    for row in owned():
                        if row["actual_state"] == "absent":
                            continue
                        retirement_safe(service, row)
                        forge.members = [member for member in forge.members if member["name"] != providers._forge_name(row)]
                        retirement_safe(service, row, require_absent=True)
                        service.specs.update(row["runner_id"], row["spec_version"], actual_state="absent")
                elif action == "expire":
                    with schema.connect(path) as c:
                        c.execute("UPDATE federation_requests SET expires_at=? WHERE id=?", (int(time.time()) + 2, command["request"]))
                elif action == "settled":
                    # The requester event arrives before the receiver's HTTP
                    # acknowledgement has returned and erased its receipt key.
                    watch = DatabaseChanges(path)
                    try:
                        deadline = time.monotonic() + 8
                        while any(secret_counts().values()):
                            assert time.monotonic() < deadline, "Acknowledged terminal credentials were retained"
                            watch.wait(max(0, deadline - time.monotonic()))
                    finally:
                        watch.close()
                elif action != "inspect":
                    raise AssertionError("Unknown test-only IPC command")
                result = {"owned": owned(), "normal_unchanged": service.specs.get(ordinary) == normal_before,
                          "fleets_unchanged": service.fleets.list() == fleets_before, "mint_count": forge.mint_count,
                          "worker_alive": worker.is_alive(), "secrets": secret_counts()}
                print(json.dumps(result), flush=True)
            except Exception as error:
                print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
    finally:
        stop.set()
        # A real DB event wakes the native worker; no polling shutdown loop.
        with schema.connect(path) as c:
            c.execute("UPDATE platform_settings SET value='stopped' WHERE key='federation_runtime'")
        worker.join(5)
        server.shutdown()
        server.server_close()
        server_thread.join(5)


if __name__ == "__main__":
    dashboard(json.loads(Path(sys.argv[1]).read_text()))
