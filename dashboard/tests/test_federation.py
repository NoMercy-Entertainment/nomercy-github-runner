import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from control.federation import Federation, PROFILE, Refused
from store import schema
from tests.test_controller_dashboard_integration import plane  # noqa: F401


class LocalProvisioner:
    def __init__(self):
        self.allocations = {}
        self.calls = []
        self.absent = True
        self.busy = False

    def available(self, profile):
        return profile["fleet_id"] == "github-linux-x64"

    def provision(self, request_id, profile, organization, count, expires, credential):
        self.calls.append((request_id, profile, organization, credential))
        return self.allocations.setdefault(request_id, [f"owned-{request_id}-{n}" for n in range(count)])

    def retire(self, request_id, runner_ids):
        return self.absent

    def running_job(self, runner_ids):
        return self.busy


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    now = [1800000000]
    adapter = LocalProvisioner()
    service = Federation(path, adapter, lambda: now[0])
    limits = {"max_runners": 3, "max_lease_minutes": 90,
              "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"],
              "automatic": False, "quiet_hours": []}
    peer, token = service.add_peer("Peer", "oidc-stable-peer", limits, "owner")
    return service, adapter, now, limits, peer, token


def request(service, peer, key="request-key-000001", **changes):
    intent = {"profile": "github-linux-x64", "organization": "Allowed-Org", "count": 2, "lease_minutes": 60}
    intent.update(changes)
    credential = {"registration_token": "A" * 32, "expires_at": service._now() + 1800,
                  "runner_group": "Lent-Restricted"}
    return service.submit(peer, key, intent, credential)


def test_credentials_never_reach_public_state_or_sql_plaintext(world):
    service, _, _, _, peer, token = world
    row = request(service, peer)
    assert service.authenticate(token)["id"] == peer
    public = json.dumps([service.peers(), service.requests(), service.activity(), service.capacity(peer)])
    assert token not in public and "A" * 32 not in public and "credential_digest" not in public
    with schema.connect(service.path) as c:
        sealed = c.execute("SELECT sealed_credential FROM federation_requests WHERE id=?", (row["id"],)).fetchone()[0]
    assert b"A" * 32 not in sealed
    assert service.authenticate(token + "x") is None


def test_active_leases_retire_on_current_quota_policy_without_releasing_busy_capacity(world):
    service, adapter, _, limits, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    assert service.get(row["id"])["state"] == "active"
    adapter.absent = False
    service.set_policy(peer, dict(limits, max_runners=1), "owner")
    service.tick()
    assert service.get(row["id"])["state"] == "retiring"
    assert service.capacity(peer)["reserved"] == 2


def test_active_lease_quiet_hours_have_exact_utc_event_deadline(world):
    service, adapter, now, limits, peer, _ = world
    next_hour = (time.gmtime(now[0]).tm_hour + 1) % 24
    service.set_policy(peer, dict(limits, quiet_hours=[next_hour]), "owner")
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    boundary = (now[0] // 3600 + 1) * 3600
    assert service.next_deadline() == boundary
    adapter.absent = False
    now[0] = boundary
    service.tick()
    assert service.get(row["id"])["state"] == "retiring"
    assert service.capacity(peer)["reserved"] == 2


@pytest.mark.parametrize("changes", [
    {"count": True}, {"count": 4}, {"lease_minutes": 91},
    {"organization": "Outside-Org"}, {"profile": "github-windows-x64"},
    {"commands": ["sudo"]}, {"template": "privileged"}, {"cpu_limit": 1},
    {"env": {"ROOT": "true"}}, {"path": "/host"},
])
def test_peer_cannot_expand_its_own_authority(world, changes):
    service, adapter, _, _, peer, _ = world
    with pytest.raises(Refused):
        request(service, peer, **changes)
    assert not service.requests() and not adapter.calls


def test_idempotency_collision_is_refused(world):
    service, _, _, _, peer, _ = world
    first = request(service, peer)
    assert request(service, peer)["id"] == first["id"]
    with pytest.raises(Refused, match="different intent"):
        request(service, peer, count=1)


def test_concurrent_approval_cannot_overbook(world):
    service, _, _, _, peer, _ = world
    first = request(service, peer)
    second = request(service, peer, "request-key-000002")
    def approve(row):
        try:
            return service.decide(row["id"], True, "owner")["state"]
        except Refused:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, [first, second])) == ["approved", "refused"]
    assert service.capacity(peer)["reserved"] == 2


def test_limits_are_rechecked_on_approval(world):
    service, _, _, limits, peer, _ = world
    row = request(service, peer)
    service.set_policy(peer, {**limits, "organizations": ["Other-Org"]}, "owner")
    with pytest.raises(Refused, match="Organisation"):
        service.decide(row["id"], True, "owner")


def test_no_adapter_cannot_claim_capacity_is_provisioned(world):
    service, _, _, _, peer, _ = world
    row = request(service, peer)
    service.adapter = None
    with pytest.raises(Refused, match="not available"):
        service.decide(row["id"], True, "owner")
    assert service.get(row["id"])["state"] == "pending"
    assert service.capacity(peer)["reserved"] == 0


def test_lease_expiry_retires_only_owned_units_and_waits_for_absence(world):
    service, adapter, now, _, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    row = service.get(row["id"])
    assert row["state"] == "active"
    assert adapter.calls[0][1] == {"fleet_id": "github-linux-x64", **PROFILE}
    with schema.connect(service.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_requests WHERE id=?", (row["id"],)).fetchone()[0] is None
    now[0] = row["expires_at"]
    adapter.absent = False
    service.tick()
    assert service.get(row["id"])["state"] == "retiring"
    assert service.capacity(peer)["reserved"] == 2
    adapter.absent = True
    service.tick()
    assert service.get(row["id"])["state"] == "expired"
    assert service.capacity(peer)["reserved"] == 0


def test_revoke_during_allocation_cannot_revive_the_lease(world):
    service, adapter, _, _, peer, token = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    original = adapter.provision
    def revoke_during_call(*args):
        service.revoke(peer, "owner")
        return original(*args)
    adapter.provision = revoke_during_call
    service.tick()
    assert service.get(row["id"])["state"] == "retiring"
    assert service.get(row["id"])["runner_ids"]
    assert service.authenticate(token) is None
    service.tick()
    assert service.get(row["id"])["state"] == "expired"


def test_extensions_require_an_observed_busy_job(world):
    service, adapter, _, _, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    with pytest.raises(Refused, match="running job"):
        service.extend(row["id"], peer, 90)
    adapter.busy = True
    assert service.extend(row["id"], peer, 90)["expires_at"] == service._now() + 5400
    with pytest.raises(Refused):
        service.extend(row["id"], peer, 91)


def test_expired_pending_credentials_are_deleted_even_without_adapter(world):
    service, _, now, _, peer, _ = world
    row = request(service, peer)
    service.adapter = None
    now[0] += 1801
    service.tick()
    assert service.get(row["id"])["state"] == "denied"
    with schema.connect(service.path) as c:
        assert c.execute("SELECT sealed_credential FROM federation_requests WHERE id=?", (row["id"],)).fetchone()[0] is None


def test_peer_credentials_are_not_local_machine_credentials(client, plane):
    service = Federation(plane)
    limits = {"max_runners": 1, "max_lease_minutes": 90, "profiles": ["github-linux-x64"], "organizations": ["Org"]}
    peer, credential = service.add_peer("Peer", "peer-owner", limits, "owner")
    headers = {"Authorization": "Bearer " + credential}
    assert client.get("/api/v2/federation/peer/capacity", headers=headers).json["capacity"]["peer_id"] == peer
    for path in ("/api/v2/fleet", "/api/v2/secrets", "/api/v2/federation", "/api/users/machines"):
        assert client.get(path, headers=headers).status_code == 403
    assert client.post("/api/v2/federation/peers/" + peer + "/revoke", json={}, headers=headers).status_code == 403
    assert client.get("/api/v2/federation/peer/capacity", headers={"Authorization": "Basic " + credential}).status_code == 401


def test_owner_changes_require_csrf_and_admin(client, plane):
    assert client.get("/federation").status_code == 200
    body = {"name": "Peer", "subject": "peer-owner", "policy": {"max_runners": 1,
            "max_lease_minutes": 90, "profiles": ["github-linux-x64"], "organizations": ["Org"]}}
    assert client.post("/api/v2/federation/peers", json=body).status_code == 403
    with client.session_transaction() as s:
        csrf = s["federation_csrf"]
    assert client.post("/api/v2/federation/peers", json=body, headers={"X-CSRF-Token": csrf}).status_code == 201
    import users
    users.approve("sub-test-admin", "operator")
    assert client.get("/federation").status_code == 403


def test_personal_access_tokens_and_normal_groups_are_refused(world):
    service, _, _, _, peer, _ = world
    intent = {"profile": "github-linux-x64", "organization": "Allowed-Org", "count": 1, "lease_minutes": 60}
    for token, group in (("ghp_" + "A" * 32, "Lent-Restricted"), ("A" * 32, "Fillz"), ("A" * 32, "Default")):
        with pytest.raises(Refused):
            service.submit(peer, "request-key-000001", intent, {"registration_token": token,
                "expires_at": service._now() + 1000, "runner_group": group})


def test_normal_fleets_and_registration_data_are_untouched(world):
    service, _, _, _, peer, _ = world
    with schema.connect(service.path) as c:
        before = {table: [tuple(row) for row in c.execute("SELECT * FROM " + table)]
                  for table in ("fleets", "runner_specs", "secrets")}
    row = request(service, peer)
    service.decide(row["id"], False, "owner")
    service.revoke(peer, "owner")
    with schema.connect(service.path) as c:
        after = {table: [tuple(row) for row in c.execute("SELECT * FROM " + table)]
                 for table in before}
    assert before == after


def test_native_watch_does_not_read_database_on_idle_stop_timeouts(monkeypatch):
    from control.federation import run_events
    import threading
    stop = threading.Event()
    counts = {"tick": 0, "read": 0, "wait": 0}
    class Service:
        path = "unused"
        clock = staticmethod(lambda: 100)
        def tick(self):
            counts["tick"] += 1
        def next_deadline(self):
            counts["read"] += 1
            return None
    class Watch:
        def __init__(self, path):
            pass
        def wait(self, timeout):
            counts["wait"] += 1
            if counts["wait"] == 4:
                stop.set()
            return False
        def close(self):
            pass
    monkeypatch.setattr("dbwatch.DatabaseChanges", Watch)
    run_events(Service(), stop)
    assert counts == {"tick": 1, "read": 1, "wait": 4}


def test_changed_policy_before_activation_creates_no_units(world):
    service, adapter, _, limits, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.set_policy(peer, {**limits, "organizations": ["Other-Org"]}, "owner")
    adapter.absent = False
    service.tick()
    assert not adapter.calls
    assert service.get(row["id"])["state"] == "retiring"
    adapter.absent = True
    service.tick()
    assert service.get(row["id"])["state"] == "expired"


def test_allocation_failure_does_not_publish_credentials_or_release_unproved_capacity(world):
    service, adapter, _, _, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    def fail(*args):
        raise RuntimeError("credential=" + "A" * 32)
    adapter.provision = fail
    adapter.absent = False
    service.tick()
    assert service.get(row["id"])["state"] == "retiring"
    assert service.capacity(peer)["reserved"] == 2
    assert "A" * 32 not in json.dumps(service.activity())


def test_peer_cannot_inspect_another_peers_requests(client, plane):
    service = Federation(plane)
    limits = {"max_runners": 1, "max_lease_minutes": 90,
              "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"]}
    one, token_one = service.add_peer("One", "owner-one", limits, "owner")
    two, token_two = service.add_peer("Two", "owner-two", limits, "owner")
    row = request(service, one, count=1)
    path = "/api/v2/federation/peer/requests/" + row["id"]
    assert client.get(path, headers={"Authorization": "Bearer " + token_one}).status_code == 200
    assert client.get(path, headers={"Authorization": "Bearer " + token_two}).status_code == 404
    assert client.get("/api/v2/federation/peer/requests", headers={"Authorization": "Bearer " + token_two}).json["requests"] == []


def test_federation_json_is_bounded_and_must_be_an_object(client, plane):
    client.get("/federation")
    with client.session_transaction() as s:
        csrf = s["federation_csrf"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    assert client.post("/api/v2/federation/peers", data='{"name":"' + "A" * 33000 + '"}', headers=headers).status_code == 413
    assert client.post("/api/v2/federation/peers", json=[], headers=headers).status_code == 400


def test_concurrent_ticks_cannot_retire_an_inflight_allocation(world):
    import threading
    service, adapter, now, _, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    entered, release = threading.Event(), threading.Event()
    original = adapter.provision
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    adapter.provision = blocked
    second_controller = Federation(service.path, adapter, service.clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(service.tick)
        assert entered.wait(5)
        now[0] = service.get(row["id"])["expires_at"] + 1
        # A separate service instance cannot retire or expire the in-flight
        # allocation, even while the first controller is outside SQLite.
        second_controller.tick()
        assert service.get(row["id"])["state"] == "activating"
        release.set()
        pending.result(5)
    assert len(adapter.calls) == 1
    assert service.get(row["id"])["state"] == "retiring"
    assert service.get(row["id"])["runner_ids"]
    second_controller.tick()
    assert service.get(row["id"])["state"] == "expired"


def test_transient_retire_error_persists_bounded_retry_without_losing_lease(world):
    service, adapter, now, _, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    service.revoke(peer, "owner")
    calls = []
    def retire(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("Temporary failure with credential " + "A" * 32)
        return True
    adapter.retire = retire
    service.tick()
    row = service.get(row["id"])
    assert row["state"] == "retiring" and row["retry_at"] == now[0] + 5
    assert service.next_deadline() == row["retry_at"]
    service.tick()
    assert len(calls) == 1
    now[0] = row["retry_at"]
    service.tick()
    assert len(calls) == 2 and service.get(row["id"])["state"] == "expired"
    assert "A" * 32 not in json.dumps(service.activity())


def test_aggregate_policy_reduction_is_rechecked_before_activation(world):
    service, adapter, _, limits, peer, _ = world
    rows = [request(service, peer, f"request-key-{n:06d}", count=1) for n in range(3)]
    for row in rows:
        service.decide(row["id"], True, "owner")
    service.set_policy(peer, {**limits, "max_runners": 2}, "owner")
    adapter.absent = False
    service.tick()
    assert service.get(rows[0]["id"])["state"] == "retiring"
    assert not adapter.calls  # retiring reservation is still held until absent
    adapter.absent = True
    service.tick()
    assert not adapter.calls
    assert all(service.get(row["id"])["state"] == "expired" for row in rows)
    assert service.capacity(peer)["reserved"] == 0


def test_no_mercy_permission_uses_known_identity_and_emits_no_peer_secret(client, plane):
    client.get("/federation")
    with client.session_transaction() as s:
        csrf = s["federation_csrf"]
    body = {"name": "Existing person", "subject": "sub-test-admin", "transport": "broker", "policy": {
        "max_runners": 0, "max_lease_minutes": 90, "profiles": ["github-linux-x64"], "organizations": ["Org"]}}
    headers = {"X-CSRF-Token": csrf}
    result = client.post("/api/v2/federation/peers", json=body, headers=headers)
    assert result.status_code == 201
    assert result.json["credential"] is None and result.headers["Cache-Control"] == "no-store"
    body["subject"] = "invented-person"
    assert client.post("/api/v2/federation/peers", json=body, headers=headers).status_code == 409
    peer = Federation(plane).peers()[0]
    assert peer["transport"] == "broker"
    assert client.get("/api/v2/federation").headers["Cache-Control"] == "no-store"


def test_provider_availability_failure_is_not_an_authority_grant(world):
    service, adapter, _, _, peer, _ = world
    row = request(service, peer)
    def unavailable(profile):
        raise RuntimeError("Credential " + "A" * 32)
    adapter.available = unavailable
    assert service.capacity(peer)["provisioning_available"] is False
    with pytest.raises(Refused, match="not available"):
        service.decide(row["id"], True, "owner")


def test_extension_rechecks_policy_after_slow_job_observation(world):
    service, adapter, _, limits, peer, _ = world
    row = request(service, peer)
    service.decide(row["id"], True, "owner")
    service.tick()
    def tighten_during_probe(ids):
        service.set_policy(peer, {**limits, "max_lease_minutes": 10}, "owner")
        return True
    adapter.running_job = tighten_during_probe
    before = service.get(row["id"])["expires_at"]
    with pytest.raises(Refused, match="lease_minutes"):
        service.extend(row["id"], peer, 90)
    assert service.get(row["id"])["expires_at"] == before


def test_peer_submission_requires_an_active_expiry_worker(client, plane):
    service = Federation(plane)
    peer, token = service.add_peer("Peer", "owner", {"max_runners": 1, "max_lease_minutes": 90,
        "profiles": ["github-linux-x64"], "organizations": ["Org"]}, "owner")
    body = {"request_key": "request-key-000001", "intent": {"profile": "github-linux-x64",
        "organization": "Org", "count": 1, "lease_minutes": 30}, "credential": {
        "registration_token": "A" * 32, "expires_at": service._now() + 1000, "runner_group": "Lent-Only"}}
    response = client.post("/api/v2/federation/peer/requests", json=body,
                           headers={"Authorization": "Bearer " + token})
    assert response.status_code == 409 and "expiry worker" in response.json["error"]
    assert not service.requests()


def test_controller_native_expiry_worker_clears_credentials_without_a_provisioner(tmp_path):
    import threading
    import time
    from control.main import Controller
    from dbwatch import DatabaseChanges
    path = str(tmp_path / "native-expiry.db")
    schema.init(path)
    service = Federation(path)
    peer, _ = service.add_peer("Peer", "owner", {"max_runners": 1, "max_lease_minutes": 90,
        "profiles": ["github-linux-x64"], "organizations": ["Allowed-Org"]}, "owner")
    credential = {"registration_token": "A" * 32, "expires_at": service._now() + 1,
                  "runner_group": "Lent-Only"}
    row = service.submit(peer, "request-key-000001", {"profile": "github-linux-x64",
        "organization": "Allowed-Org", "count": 1, "lease_minutes": 30}, credential)
    controller = Controller.__new__(Controller)
    controller.db, controller.federation = path, service
    controller._stop = threading.Event()
    messages = []
    watch = DatabaseChanges(path)
    worker = threading.Thread(target=controller._federation_events, args=(messages.append,), daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if service.get(row["id"])["state"] == "denied":
                break
            # inotify can consume an unrelated directory event (the
            # convergence lock's first creation) and return False before the
            # timeout. Keep waiting on native events until the actual deadline.
            watch.wait(max(0, deadline - time.monotonic()))
        assert service.get(row["id"])["state"] == "denied"
        with schema.connect(path) as c:
            assert c.execute("SELECT sealed_credential FROM federation_requests WHERE id=?", (row["id"],)).fetchone()[0] is None
        assert not messages
    finally:
        controller._stop.set()
        controller._federation_state("stopping")  # native WAL event releases wait
        worker.join(5)
        watch.close()
    assert not worker.is_alive()
