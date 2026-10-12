import base64
import hashlib
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from control.federation import Federation, Refused
from control.federation_broker import Broker, canonical
from store import schema
from tests.test_controller_dashboard_integration import plane  # noqa: F401


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    return key, {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": str(uuid.uuid4()),
                 "n": b64(numbers.n.to_bytes(256, "big")), "e": b64(numbers.e.to_bytes(3, "big"))}


@pytest.fixture
def broker_world(tmp_path, signing_key):
    path = str(tmp_path / "control.db")
    schema.init(path)
    now = [int(time.time())]
    service = Federation(path, clock=lambda: now[0])
    subject, audience = str(uuid.uuid4()), str(uuid.uuid4())
    peer, _ = service.add_peer("Existing person", subject, {"max_runners": 1, "max_lease_minutes": 90,
        "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"]}, "owner", "broker")
    calls = []
    def http(url, body=None, headers=None):
        calls.append((url, body, headers))
        return {"keys": [signing_key[1]]} if body is None else {"received": True}
    broker = Broker(path, clock=lambda: now[0], http=http)
    broker.configure({"enabled": True, "issuer": "https://api.nomercy.tv", "fleet_id": audience})
    envelope = {"request_key": str(uuid.uuid4()), "intent": {"profile": "github-linux-x64", "organization": "Allowed-Org",
        "count": 1, "lease_minutes": 90}, "credential": {"registration_token": "A" * 32,
        "expires_at": now[0] + 1000, "runner_group": "Lent-Restricted"}, "receipt": {
        "request_id": None, "credential": "B" * 64, "expires_at": now[0] + 7200 * 60 + 90 * 60 + 3600}}
    envelope["receipt"]["request_id"] = envelope["request_key"]
    return service, broker, peer, subject, audience, envelope, now, calls


def assertion(world, signing_key, changes=None, envelope=None, header_changes=None):
    _, _, _, subject, audience, original, now, _ = world
    payload = original if envelope is None else envelope
    claims = {"iss": "https://api.nomercy.tv", "aud": audience, "sub": subject, "purpose": "capacity",
        "request_id": payload["request_key"], "body_hash": hashlib.sha256(canonical(payload)).hexdigest(),
        "iat": now[0], "nbf": now[0] - 5, "exp": now[0] + 120, "jti": str(uuid.uuid4())}
    claims.update(changes or {})
    header = {"typ": "JWT", "alg": "RS256", "kid": signing_key[1]["kid"]}
    header.update(header_changes or {})
    signed = b64(canonical(header)) + "." + b64(canonical(claims))
    signature = signing_key[0].sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return "nmc_" + signed + "." + b64(signature)


def test_dedicated_assertion_resolves_only_an_enabled_local_person(broker_world, signing_key):
    service, broker, peer, _, _, envelope, _, calls = broker_world
    offered = assertion(broker_world, signing_key)
    assert broker.verify(offered, envelope)["id"] == peer
    assert calls[0][0] == "https://api.nomercy.tv/v1/runner-fleets/.well-known/jwks.json"
    service.revoke(peer, "owner")
    with pytest.raises(Refused, match="no enabled local peer"):
        broker.verify(offered, envelope)


def test_owner_initiated_registration_binds_nonce_actor_fleet_and_purpose(broker_world, signing_key):
    _, broker, _, subject, audience, _, _, _ = broker_world
    nonce = broker.begin_registration("https://api.nomercy.tv", subject)
    envelope = {"request_key": str(uuid.uuid4()), "fleet_id": audience, "ownership_nonce": nonce}
    offered = assertion(broker_world, signing_key, {"purpose": "registration"}, envelope)
    identity = broker.verify(offered, envelope, "registration")
    proof = broker.challenge(envelope, identity)
    assert proof == hashlib.sha256((nonce + "|" + audience).encode()).hexdigest()
    assert broker.settings()["fleet_id"] == audience
    assert broker.challenge(envelope, identity) == proof  # Lost ACK is idempotent.
    with pytest.raises(Refused):
        broker.verify(offered, envelope)  # Registration assertion cannot request capacity.
    wrong = dict(envelope, ownership_nonce="N" * 64)
    with pytest.raises(Refused, match="nonce"):
        broker.challenge(wrong, identity)


def test_registration_refuses_other_actor_and_expired_or_reassigned_nonce(broker_world, signing_key):
    _, broker, _, subject, audience, _, now, _ = broker_world
    nonce = broker.begin_registration("https://api.nomercy.tv", subject)
    envelope = {"request_key": str(uuid.uuid4()), "fleet_id": audience, "ownership_nonce": nonce}
    with pytest.raises(Refused, match="initiating local owner"):
        broker.verify(assertion(broker_world, signing_key, {"purpose": "registration", "sub": str(uuid.uuid4())}, envelope), envelope, "registration")
    broker.begin_registration("https://api.nomercy.tv", subject)
    identity = {"subject": subject, "verified_issuer": "https://api.nomercy.tv"}
    with pytest.raises(Refused):
        broker.challenge(envelope, identity)
    now[0] += 301
    with pytest.raises(Refused, match="owner-initiated"):
        broker.challenge(envelope, identity)


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.2.60", "169.254.169.254", "100.64.0.1", "2001:db8::1", "::1"])
def test_broker_transport_refuses_nonpublic_dns_before_connection(monkeypatch, address):
    from control import federation_broker as module
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (address, 443))])
    with pytest.raises(Refused, match="public"):
        module.http_json("https://api.nomercy.tv/v1/runner-fleets")


def test_broker_connection_uses_checked_address_and_tls_original_identity(monkeypatch):
    from control import federation_broker as module
    connected, wrapped = [], []
    sock = object()
    monkeypatch.setattr(module.socket, "create_connection", lambda endpoint, **kw: connected.append(endpoint) or sock)
    class Context:
        def wrap_socket(self, value, server_hostname):
            wrapped.append((value, server_hostname))
            return "verified"
    connection = module.PinnedHTTPS("api.nomercy.tv", "93.184.216.34")
    connection._context = Context()
    connection.connect()
    assert connected == [("93.184.216.34", 443)]
    assert wrapped == [(sock, "api.nomercy.tv")]


@pytest.mark.parametrize("url", ["http://api.nomercy.tv/v1/runner-fleets", "https://evil.invalid/v1/runner-fleets", "https://api.nomercy.tv/v1/media", "https://api.nomercy.tv/v1/runner-fleets?redirect=x"])
def test_broker_transport_never_sends_account_credential_to_another_origin(url):
    from control.federation_broker import http_json
    with pytest.raises(Refused):
        http_json(url, headers={"Authorization": "Bearer private"})


@pytest.mark.parametrize("changes", [{"iss": "https://attacker.invalid"}, {"aud": str(uuid.uuid4())},
    {"sub": "super-admin-not-a-local-peer"}, {"exp": 0}, {"iat": True},
    {"exp": 9999999999}, {"body_hash": "0" * 64}, {"request_id": str(uuid.uuid4())}, {"purpose": "registration"}, {"roles": ["super-admin"]}])
def test_roles_wrong_issuer_audience_lifetime_and_body_never_expand_authority(broker_world, signing_key, changes):
    with pytest.raises(Refused):
        broker_world[1].verify(assertion(broker_world, signing_key, changes), broker_world[5])


def test_alternative_algorithms_and_remote_key_urls_are_refused(broker_world, signing_key):
    for changes in ({"alg": "HS256"}, {"jku": "https://attacker.invalid/keys"}, {"alg": "none"}):
        with pytest.raises(Refused):
            broker_world[1].verify(assertion(broker_world, signing_key, header_changes=changes), broker_world[5])


def test_assertion_cannot_be_reused_with_tampered_registration_or_command_body(broker_world, signing_key):
    offered = assertion(broker_world, signing_key)
    envelope = json.loads(json.dumps(broker_world[5]))
    envelope["intent"]["commands"] = ["sudo"]
    with pytest.raises(Refused, match="different request envelope"):
        broker_world[1].verify(offered, envelope)
    envelope = json.loads(json.dumps(broker_world[5]))
    envelope["credential"]["registration_token"] = "C" * 32
    with pytest.raises(Refused, match="different request envelope"):
        broker_world[1].verify(offered, envelope)


def test_duplicate_delivery_has_one_local_request_and_no_credentials_in_json(broker_world, signing_key):
    service, broker, peer, _, _, envelope, _, _ = broker_world
    offered = assertion(broker_world, signing_key)
    for _ in range(2):
        assert broker.verify(offered, envelope)["id"] == peer
        row = service.submit(peer, envelope["request_key"], envelope["intent"], envelope["credential"])
        broker.receipt(row, envelope["receipt"])
    assert len(service.requests()) == 1
    assert "A" * 32 not in json.dumps(service.requests())
    assert "B" * 64 not in json.dumps(service.requests())


def test_receipts_push_on_state_changes_only_and_never_forward_an_account_token(broker_world):
    service, broker, peer, _, _, envelope, _, calls = broker_world
    row = service.submit(peer, envelope["request_key"], envelope["intent"], envelope["credential"])
    broker.receipt(row, envelope["receipt"])
    broker.publish()
    assert len(calls) == 1
    url, payload, headers = calls[0]
    assert url == "https://api.nomercy.tv/v1/runner-fleets/receipts/" + envelope["request_key"]
    assert payload == {"sequence": 1, "state": "pending", "receiver_request_id": row["id"]}
    assert headers == {"X-Fleet-Receipt": "B" * 64}
    broker.publish()
    assert len(calls) == 1
    service.decide(row["id"], False, "owner")
    broker.publish()
    assert calls[-1][1]["state"] == "denied" and calls[-1][1]["sequence"] == 2
    with schema.connect(service.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_receipts").fetchone()[0] is None


def test_disabled_broker_still_purges_expired_secrets_at_exact_deadline(broker_world):
    service, broker, peer, _, audience, envelope, now, calls = broker_world
    row = service.submit(peer, envelope["request_key"], envelope["intent"], envelope["credential"])
    envelope["receipt"]["expires_at"] = now[0] + 10
    broker.receipt(row, envelope["receipt"])
    broker.configure({"enabled": False, "issuer": "https://api.nomercy.tv", "fleet_id": audience})
    assert service.next_deadline() == now[0] + 10
    now[0] += 10
    broker.publish()
    assert not calls
    with schema.connect(service.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_receipts").fetchone()[0] is None


def test_existing_receipt_cannot_be_retargeted_by_broker_configuration_change(broker_world):
    service, broker, peer, _, audience, envelope, _, calls = broker_world
    row = service.submit(peer, envelope["request_key"], envelope["intent"], envelope["credential"])
    broker.receipt(row, envelope["receipt"], "https://api.nomercy.tv")
    broker.configure({"enabled": True, "issuer": "https://api-dev.nomercy.tv", "fleet_id": audience})
    broker.publish()
    assert calls[-1][0].startswith("https://api.nomercy.tv/")


def test_authenticated_courier_cannot_operate_normal_dashboard_or_override_peer_limits(client, plane, signing_key, monkeypatch):
    service = Federation(plane)
    subject, audience = str(uuid.uuid4()), str(uuid.uuid4())
    peer, _ = service.add_peer("Known person", subject, {"max_runners": 1, "max_lease_minutes": 90,
        "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"]}, "owner", "broker")
    broker = Broker(plane)
    broker.configure({"enabled": True, "issuer": "https://api.nomercy.tv", "fleet_id": audience})
    monkeypatch.setattr(Broker, "_jwks", lambda self, issuer: [signing_key[1]])
    with schema.connect(plane) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_runtime','running')")
    now = [int(time.time())]
    envelope = {"request_key": str(uuid.uuid4()), "intent": {"profile": "github-linux-x64", "organization": "Allowed-Org",
        "count": 1, "lease_minutes": 90}, "credential": {"registration_token": "A" * 32,
        "expires_at": now[0] + 1000, "runner_group": "Lent-Restricted"}, "receipt": {"request_id": None,
        "credential": "B" * 64, "expires_at": now[0] + 10000}}
    envelope["receipt"]["request_id"] = envelope["request_key"]
    world = service, broker, peer, subject, audience, envelope, now, []
    offered = assertion(world, signing_key)
    headers = {"Authorization": "Bearer " + offered}
    response = client.post("/api/v2/federation/broker/requests", json=envelope, headers=headers)
    assert response.status_code == 202
    assert client.get("/api/v2/fleet", headers=headers).status_code == 403
    assert client.post("/api/v2/federation/peers/" + peer + "/revoke", json={}, headers=headers).status_code == 403
    changed = json.loads(json.dumps(envelope))
    changed["intent"]["count"] = 2
    token = assertion(world, signing_key, envelope=changed)
    assert client.post("/api/v2/federation/broker/requests", json=changed,
                       headers={"Authorization": "Bearer " + token}).status_code == 409
    assert len(service.requests()) == 1


def test_signed_malformed_receipt_is_refused_before_accepting_capacity(client, plane, signing_key, monkeypatch):
    service = Federation(plane)
    subject, audience = str(uuid.uuid4()), str(uuid.uuid4())
    peer, _ = service.add_peer("Known person", subject, {"max_runners": 1, "max_lease_minutes": 90,
        "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"]}, "owner", "broker")
    broker = Broker(plane)
    broker.configure({"enabled": True, "issuer": "https://api.nomercy.tv", "fleet_id": audience})
    monkeypatch.setattr(Broker, "_jwks", lambda self, issuer: [signing_key[1]])
    with schema.connect(plane) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_runtime','running')")
    now = [int(time.time())]
    envelope = {"request_key": str(uuid.uuid4()), "intent": {}, "credential": {}, "receipt": None}
    world = service, broker, peer, subject, audience, envelope, now, []
    response = client.post("/api/v2/federation/broker/requests", json=envelope,
                           headers={"Authorization": "Bearer " + assertion(world, signing_key)})
    assert response.status_code == 409 and not service.requests()
