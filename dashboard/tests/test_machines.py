import json
import time

import pytest

import machines
import users


@pytest.fixture
def credential(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
    metadata, token = machines.create("Arcanum", ["fleet:read", "jobs:read"], 30, "owner")
    return metadata, token


def test_credential_is_hashed_and_revocation_is_immediate(credential):
    metadata, token = credential
    stored = open(users.PATH).read()
    assert token not in stored
    assert "digest" not in json.dumps(machines.listing())
    assert machines.authenticate(token) == metadata
    assert machines.authenticate(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    machines.revoke(metadata["id"])
    assert machines.authenticate(token) is None


def test_expired_or_unreadable_credentials_fail_closed(credential, monkeypatch):
    metadata, token = credential
    monkeypatch.setattr(time, "time", lambda: metadata["expires_at"])
    assert machines.authenticate(token) is None
    with open(users.PATH, "w") as stream:
        stream.write("broken")
    assert machines.authenticate(token) is None


@pytest.mark.parametrize("path,method", [
    ("/api/v2/secrets", "GET"), ("/api/v2/settings", "GET"), ("/api/users/machines", "GET"),
    ("/api/v1/users/approve", "POST"), ("/users", "GET"), ("/api/v2/fleets/github/runners", "POST"),
    ("/api/v2/runners/11111111-1111-4111-8111-111111111111/actions/remove", "POST"),
    ("/api/v2/runners/11111111-1111-4111-8111-111111111111/limits", "POST"),
])
def test_machine_cannot_reach_admin_or_unscoped_routes(credential, anon_client, path, method):
    _, token = credential
    response = anon_client.open(path, method=method, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403


def test_bearer_reads_fleet_without_browser_cookie(credential, anon_client):
    _, token = credential
    assert anon_client.get("/api/v2/fleet", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_invalid_bearer_never_inherits_admin_session(client):
    assert client.get("/api/v2/secrets", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_creation_requires_browser_admin_and_csrf(client):
    body = {"name": "Arcanum", "scopes": ["jobs:read"], "days": 30}
    assert client.post("/api/users/machines", json=body).status_code == 403
    assert client.get("/users").status_code == 200
    with client.session_transaction() as session:
        csrf = session["machine_csrf"]
    created = client.post("/api/users/machines", json=body, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    data = created.get_json()
    assert machines.authenticate(data["credential"])["scopes"] == ["jobs:read"]
    assert client.post("/api/users/machines/" + data["machine"]["id"] + "/revoke",
                       headers={"X-CSRF-Token": csrf}).status_code == 200
    assert machines.authenticate(data["credential"]) is None


@pytest.mark.parametrize("scopes,days", [(["admin"], 30), ([], 30), ("fleet:read", 30),
                                       (["fleet:read"], 0), (["fleet:read"], True)])
def test_invalid_permissions_or_expiry_cannot_create_access(credential, scopes, days):
    before = open(users.PATH).read()
    with pytest.raises(ValueError):
        machines.create("Bad", scopes, days, "owner")
    assert open(users.PATH).read() == before
