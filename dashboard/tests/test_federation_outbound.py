import json
import time
import uuid
from types import SimpleNamespace

import pytest
import providers

from control.federation import Federation, Refused
from control.federation_broker import Broker
from control.federation_outbound import Outbound
from store import schema
from store.fleets import FleetStore
from tests.test_federation_broker import assertion, broker_world, signing_key  # noqa: F401
from tests.test_controller_dashboard_integration import plane  # noqa: F401


@pytest.fixture
def outgoing(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    Federation(path)
    fleets = FleetStore(path)
    fleets.seed({})
    subject, audience, target, remote = (str(uuid.uuid4()) for _ in range(4))
    Broker(path).configure({"enabled": True, "issuer": "https://api.nomercy.tv", "fleet_id": audience})
    calls = []
    def http(url, body=None, headers=None):
        calls.append((url, body, headers))
        if body is None:
            return {"data": [{"id": target, "name": "Permitted fleet", "verified": True,
                             "capabilities": {"profiles": ["github-linux-x64"]}}]}
        if url.endswith("/register"):
            return {"data": {"id": audience}}
        return {"request": {"id": remote}}
    class Requester:
        org = "Allowed-Org"
        def runner_groups(self):
            return [{"id": 3, "name": "Lent-Restricted", "visibility": "selected",
                     "allows_public_repositories": False, "inherited": False, "default": False}]
        def _get(self, path, **kwargs):
            return {"total_count": 1}
        def registration_token(self):
            return "A" * 32
    monkeypatch.setattr(providers.by_key("github"), "forge_client", lambda env: Requester())
    service = Outbound(path, SimpleNamespace(env={}, fleets=fleets), http=http)
    service.retain_identity({"sub": subject}, {"access_token": "owner-account-secret", "expires_in": 300})
    intent = {"profile": "github-linux-x64", "organization": "Allowed-Org", "count": 1, "lease_minutes": 90}
    return service, subject, audience, target, remote, calls, intent


def test_owner_token_is_encrypted_expiring_and_sent_only_to_fixed_broker(outgoing):
    service, subject, _, target, _, calls, intent = outgoing
    key = service.submit(subject, target, intent, "Lent-Restricted")
    assert str(uuid.UUID(key)) == key
    assert all(url.startswith("https://api.nomercy.tv/v1/runner-fleets") for url, _, _ in calls)
    assert all(headers["Authorization"] == "Bearer owner-account-secret" for _, _, headers in calls)
    credential = calls[-1][1]["credential"]
    assert credential["registration_token"] == "A" * 32 and credential["expires_at"] <= int(time.time()) + 1800
    public = json.dumps(service.results(subject))
    assert "owner-account-secret" not in public and "A" * 32 not in public
    with schema.connect(service.path) as c:
        assert b"owner-account-secret" not in c.execute("SELECT sealed_access FROM federation_identities").fetchone()[0]
    federation = Federation(service.path)
    assert federation.next_deadline() <= int(time.time()) + 300
    federation.clock = lambda: time.time() + 301
    federation.tick()
    with schema.connect(service.path) as c:
        assert c.execute("SELECT sealed_access FROM federation_identities").fetchone()[0] is None


@pytest.mark.parametrize("changes", [{"organization": "Foreign-Org"}, {"count": True}, {"lease_minutes": 7201}, {"commands": ["sudo"]}, {"profile": "github-macos-x64"}])
def test_outgoing_bounded_intents_and_requester_organisation_cannot_expand(outgoing, changes):
    service, subject, _, target, _, calls, intent = outgoing
    with pytest.raises(Refused):
        service.submit(subject, target, dict(intent, **changes), "Lent-Restricted")
    assert all(body is None for _, body, _ in calls)
    assert service.results(subject) == []


def test_registration_uses_owner_nonce_configured_origin_and_no_pairing_secret(outgoing):
    service, subject, _, _, _, calls, _ = outgoing
    service.register(subject, "https://api.nomercy.tv", "My fleet", "https://dashboard.example.com")
    payload = calls[-1][1]
    assert payload["callback_origin"] == "https://dashboard.example.com"
    assert len(payload["ownership_nonce"]) == 64 and payload["capabilities"]["protocol"] == 1
    for origin in ("http://dashboard.example.com", "https://evil.example.com/path", "https://user:secret@dashboard.example.com"):
        with pytest.raises(Refused):
            service.register(subject, "https://api.nomercy.tv", "My fleet", origin)
    with pytest.raises(Refused):
        service.call(subject, "/register", {}, "https://evil.invalid")


def test_pushed_signed_result_targets_only_local_owner_intent_and_terminal_state(broker_world, signing_key):
    federation, broker, _, subject, _, _, now, _ = broker_world
    service = Outbound(federation.path, clock=lambda: now[0])
    key, remote, receiver = (str(uuid.uuid4()) for _ in range(3))
    with schema.connect(service.path) as c:
        c.execute("INSERT INTO federation_outgoing(request_key,actor,target_fleet_id,intent,created_at) VALUES(?,?,?,?,?)",
                  (key, subject, str(uuid.uuid4()), '{}', now[0]))
    body = {"request_key": key, "broker_request_id": remote, "receiver_request_id": receiver, "state": "active", "sequence": 1}
    offered = assertion(broker_world, signing_key, {"purpose": "result"}, body)
    identity = broker.verify(offered, body, "result")
    service.receive(identity, body)
    service.receive(identity, body)  # Duplicate ACK is harmless.
    assert service.results(subject)[0]["state"] == "active"
    assert federation.activity()[-1]["action"] == "peer-result"
    body.update(state="expired", sequence=2)
    service.receive(broker.verify(assertion(broker_world, signing_key, {"purpose": "result"}, body), body, "result"), body)
    with pytest.raises(Refused, match="terminal"):
        service.receive(identity, dict(body, sequence=3, state="active"))
    with pytest.raises(Refused):
        broker.verify(assertion(broker_world, signing_key, {"purpose": "result", "sub": str(uuid.uuid4())}, body), body, "result")
    with pytest.raises(Refused):
        broker.verify(offered, body, "capacity")


def test_sharing_sign_in_is_explicit_csrf_and_does_not_accept_supplied_account_token(client, plane):
    client.get("/federation")
    with client.session_transaction() as session:
        csrf = session["federation_csrf"]
        assert "federation_login" not in session
    assert client.post("/api/v2/federation/broker/sign-in", json={}).status_code == 403
    response = client.post("/api/v2/federation/broker/sign-in", json={}, headers={"X-CSRF-Token": csrf})
    assert response.json == {"redirect": "/auth/start"}
    assert client.post("/api/v2/federation/broker/sign-in", json={"access_token": "private"}, headers={"X-CSRF-Token": csrf}).status_code == 409
