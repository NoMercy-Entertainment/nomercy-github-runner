import json
import time
from datetime import datetime, timezone

import pytest
import providers

from control.federation import Federation, PROFILE, Refused
from control.federation_provision import LocalProvisioner, registration_for, registration_consumed, retirement_safe
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore


class Forge:
    org = "Allowed-Org"
    def __init__(self):
        self.members = []
        self.after_read = None
    def runner_groups(self):
        return [{"id": 3, "name": "Lent-Restricted", "visibility": "selected",
                 "allows_public_repositories": False, "inherited": False, "default": False}]
    def _get(self, path, **kwargs):
        return {"total_count": 1}
    def _pages(self, path, key):
        if self.after_read:
            call, self.after_read = self.after_read, None
            call()
        return self.members
    def registration_token(self):
        raise AssertionError("The receiver must never mint a PAT-backed registration token for a lent spec")
    def all_runners(self):
        return [dict(member, busy=False, status="online") for member in self.members]
    def move_runner_to_group(self, group, rid):
        return group == "No-Jobs"
    def drain_group_closed(self, group, rid=None):
        return group == "No-Jobs"


@pytest.fixture
def allocation(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    env = {"GITHUB_ORG": "Allowed-Org", "GITHUB_DRAIN_GROUP": "No-Jobs",
           "RUNNER_UNIT_IMAGE_GITHUB_LINUX": "curated:guard"}
    FleetStore(path).seed(env)
    service = RunnerService(path, env=env)
    service.inventory.register_worker("linux-one", "hyperv-linux")
    service.inventory.heartbeat("linux-one", capabilities={"kind": "linux-container", "builds_from": "image",
        "host_cores": 32, "memory_bytes": 64 * 1024**3, "architecture": "x64",
        "disk_quota": True, "disk_limit_enforced": True, "federation_fork_guard": True,
        "federation_templates": ["curated:guard"]})
    forge = Forge()
    adapter = LocalProvisioner(service, forge)
    federation = Federation(path, adapter)
    limits = {"max_runners": 3, "max_lease_minutes": 90, "profiles": ["github-linux-x64"],
              "organizations": ["Allowed-Org"], "runner_groups": ["Lent-Restricted"]}
    peer, _ = federation.add_peer("Owner-selected person", "peer-sub", limits, "owner")
    with schema.connect(path) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_lending','true')")
    return service, forge, adapter, federation, peer, limits


def request(world, count=2):
    _, _, _, federation, peer, _ = world
    row = federation.submit(peer, "request-key-000001", {"profile": "github-linux-x64", "organization": "Allowed-Org",
        "count": count, "lease_minutes": 60}, {"registration_token": "A" * 32,
        "expires_at": int(time.time()) + 1800, "runner_group": "Lent-Restricted"})
    federation.decide(row["id"], True, "owner")
    federation.tick()
    return federation.get(row["id"])


def ready(world, row, guarded=True):
    service, forge, _, _, _, _ = world
    ids = [spec["runner_id"] for spec in service.specs.list()]
    for index, rid in enumerate(ids):
        spec = service.specs.get(rid)
        service.specs.update(rid, spec["spec_version"], actual_state="idle", registration_id=str(100 + index))
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?", (json.dumps({"origin_guard": {
                "version": 1, "federation_guard": guarded, "at": datetime.now(timezone.utc).isoformat()}}), rid))
        forge.members.append({"id": 100 + index, "name": providers._forge_name(spec),
                              "labels": [{"name": value} for value in spec["labels"]]})
    return ids


def test_owned_specs_are_atomic_fixed_resources_without_normal_fleet_changes(allocation):
    service, _, _, federation, _, _ = allocation
    before = service.fleets.list()
    row = request(allocation)
    assert row["state"] == "activating" and row["runner_ids"] == []
    specs = service.specs.list()
    assert len(specs) == 2 and all(spec["fleet_id"] is None for spec in specs)
    assert service.fleets.list() == before
    assert all(spec["memory_limit"] == PROFILE["memory_bytes"] and spec["disk_limit"] == PROFILE["disk_bytes"] and
               spec["cache_policy"]["max_bytes"] == PROFILE["cache_bytes"] and spec["runner_group"] == "Lent-Restricted" for spec in specs)
    assert specs[0]["cpu_limit"] != specs[1]["cpu_limit"]
    ids = ready(allocation, row)
    federation.tick()
    assert federation.get(row["id"])["state"] == "active"
    assert set(federation.get(row["id"])["runner_ids"]) == set(ids)


def test_registration_uses_requester_token_then_consumes_it_without_local_pat(allocation):
    service, _, _, _, _, _ = allocation
    request(allocation, 1)
    spec = service.specs.list()[0]
    plan = registration_for(service, spec)
    assert plan.token == "A" * 32 and plan.url == "https://github.com/Allowed-Org" and plan.runner_group == "Lent-Restricted"
    registration_consumed(service, spec)
    with pytest.raises(Refused, match="consumed"):
        registration_for(service, spec)
    ordinary = service.specs.create(provider="github", platform="linux", fleet_id="github-linux-x64")
    assert registration_for(service, service.specs.get(ordinary)) is None


def test_group_or_labels_drift_never_publishes_active_capacity(allocation):
    _, forge, _, federation, _, _ = allocation
    row = request(allocation, 1)
    ready(allocation, row)
    forge.members[0]["labels"] = [{"name": "wrong"}]
    federation.tick()
    assert federation.get(row["id"])["state"] == "retiring"
    assert federation.get(row["id"])["runner_ids"] == []


def test_missing_strict_hook_proof_keeps_activation_pending(allocation):
    _, _, _, federation, _, _ = allocation
    row = request(allocation, 1)
    ready(allocation, row, guarded=False)
    federation.tick()
    assert federation.get(row["id"])["state"] == "activating"


def test_policy_tightening_during_group_probe_blocks_registration(allocation):
    service, forge, _, federation, peer, limits = allocation
    request(allocation, 1)
    forge.after_read = lambda: federation.set_policy(peer, dict(limits, runner_groups=[]), "owner")
    with pytest.raises(Refused, match="owner limits"):
        registration_for(service, service.specs.list()[0])


def test_revocation_retires_only_private_claims_and_clears_even_absent_tokens(allocation):
    service, _, adapter, federation, peer, _ = allocation
    row = request(allocation, 1)
    spec = service.specs.list()[0]
    ordinary = service.specs.create(provider="github", platform="linux", desired_state="running", fleet_id="github-linux-x64")
    service.specs.update(spec["runner_id"], spec["spec_version"], actual_state="absent")
    federation.revoke(peer, "owner")
    assert adapter.retire(row["id"], [ordinary]) is True
    with schema.connect(service.specs.path) as c:
        assert c.execute("SELECT sealed_registration FROM federation_allocations").fetchone()[0] is None
    assert service.specs.get(ordinary)["desired_state"] == "running"
    federation.tick()
    assert federation.get(row["id"])["state"] == "expired"


def test_foreign_organization_and_non_owned_group_members_fail_closed(allocation):
    _, forge, adapter, federation, _, _ = allocation
    forge.members = [{"id": 7, "name": "normal-runner", "labels": []}]
    with pytest.raises(Refused, match="privately claimed"):
        adapter.restricted_group("Lent-Restricted")
    with pytest.raises(Refused, match="own safely observed"):
        adapter.provision("unknown", dict(PROFILE, fleet_id="github-linux-x64"), "Foreign-Org", 1, int(time.time()) + 100,
                          {"runner_group": "Lent-Restricted", "registration_token": "A" * 32, "expires_at": int(time.time()) + 100})
    assert federation.requests() == []


def test_claimed_token_has_exact_native_expiry_even_without_adapter(allocation):
    service, _, _, federation, _, _ = allocation
    request(allocation, 1)
    now = int(time.time())
    with schema.connect(service.specs.path) as c:
        c.execute("UPDATE federation_allocations SET credential_expires_at=?", (now + 5,))
    assert federation.next_deadline() == now + 5
    federation.clock = lambda: now + 6
    federation.adapter = None
    federation.tick()
    with schema.connect(service.specs.path) as c:
        assert c.execute("SELECT sealed_registration FROM federation_allocations").fetchone()[0] is None


def test_unknown_or_busy_forge_blocks_stopped_lent_retirement(allocation, monkeypatch):
    service, forge, _, _, _, _ = allocation
    request(allocation, 1)
    spec = service.specs.list()[0]
    monkeypatch.setattr(forge, "all_runners", lambda: None)
    with pytest.raises(Refused, match="unknown"):
        retirement_safe(service, spec)
    ordinary = service.specs.create(provider="github", platform="linux")
    retirement_safe(service, service.specs.get(ordinary))  # Existing fleet paths unchanged.
    monkeypatch.setattr(forge, "all_runners", lambda: [{"id": 7, "name": providers._forge_name(spec), "status": "online", "busy": True}])
    with pytest.raises(Refused, match="running or unknown"):
        retirement_safe(service, spec)


def test_unit_removal_requires_owned_forge_record_absent_even_after_stopping(allocation, monkeypatch):
    service, forge, _, _, _, _ = allocation
    request(allocation, 1)
    spec = service.specs.list()[0]
    monkeypatch.setattr(forge, "all_runners", lambda: [{"id": 7, "name": providers._forge_name(spec), "status": "offline", "busy": False}])
    retirement_safe(service, spec)  # Closed group and fresh idle proof permits deregistration.
    with pytest.raises(Refused, match="record must be absent"):
        retirement_safe(service, spec, require_absent=True)
    monkeypatch.setattr(forge, "all_runners", lambda: [])
    retirement_safe(service, spec, require_absent=True)
