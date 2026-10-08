"""Mac defaults need fresh appliance enforcement, and placement must honor it."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import api_v2
from control import placement
from store import schema
from tests.test_controller_dashboard_integration import plane  # noqa: F401

FID = "github-macos-x64"
GIB = 1024**3
CAPS = {"kind": "macos-appliance", "architecture": "x64", "appliance_per_runner": True,
        "cpu_enforcement": True, "memory_enforcement": True,
        "per_runner_memory_overhead_bytes": 2 * GIB, "guest_disk_virtual_bytes": 256 * GIB,
        "disk_quota": False, "memory_bytes": 32 * GIB}


def worker(service, name="pool", caps=None, kind="hyperv-linux", state="healthy"):
    caps = dict(CAPS if caps is None else caps)
    service.inventory.register_worker(name, kind, capabilities=caps)
    if state != "unknown":
        service.inventory.heartbeat(name, capabilities=caps)
    if state == "degraded":
        service.inventory.mark_degraded(name, "test degradation")
    if state == "stale":
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with schema.connect(service.fleets.path) as connection:
            connection.execute("UPDATE workers SET last_seen_at=? WHERE host_id=?", (old, name))
    return {"host_id": name, "capabilities": caps}


def fleet(client):
    return next(row for row in client.get("/api/v2/settings").json["fleets"] if row["fleet_id"] == FID)


def test_healthy_pool_enables_cpu_and_guest_ram_but_fixed_disk_is_read_only(client, plane):
    service, _ = api_v2.control_plane()
    worker(service)
    row = fleet(client)
    assert row["cpu_limit_supported"] and row["memory_limit_supported"]
    assert not row["disk_quota_supported"]
    assert row["guest_disk_virtual_bytes"] == 256 * GIB
    assert "2 GiB" in row["resource_notice"] and "fixed at 256 GiB" in row["resource_notice"]
    saved = client.post(f"/api/v2/settings/{FID}", json={"cpu_limit": "4.0", "memory_limit": 8 * GIB})
    assert saved.status_code == 200, saved.json
    assert saved.json["fleet"]["cpu_limit"] == "4"
    assert client.post(f"/api/v2/settings/{FID}", json={"disk_limit": 128 * GIB}).status_code == 400
    assert service.fleets.get(FID)["disk_limit"] is None


@pytest.mark.parametrize("state,caps,kind", [
    ("unknown", CAPS, "hyperv-linux"), ("stale", CAPS, "hyperv-linux"),
    ("degraded", CAPS, "hyperv-linux"),
    ("healthy", dict(CAPS, appliance_per_runner=False), "hyperv-linux"),
    ("healthy", dict(CAPS, cpu_enforcement=1), "hyperv-linux"),
    ("healthy", dict(CAPS, memory_enforcement="true"), "hyperv-linux"),
    ("healthy", dict(CAPS, architecture="arm64"), "hyperv-linux"),
    ("healthy", CAPS, "hyperv-windows"),
    ("healthy", dict(CAPS, capacity_valid=False), "hyperv-linux"),
    ("healthy", {"kind": "macos-appliance", "runtime": "invalid"}, "hyperv-linux"),
])
def test_unverified_workers_cannot_enable_or_change_mac_limits(client, plane, state, caps, kind):
    service, _ = api_v2.control_plane()
    worker(service, caps=caps, state=state, kind=kind)
    row = fleet(client)
    assert not row["cpu_limit_supported"] and not row["memory_limit_supported"]
    for values in ({"cpu_limit": "4"}, {"memory_limit": 8 * GIB}, {"memory_limit": None}):
        assert client.post(f"/api/v2/settings/{FID}", json=values).status_code == 400
    assert client.post(f"/api/v2/settings/{FID}", json={"labels": ["macos"]}).status_code == 200


def test_flags_from_different_workers_do_not_invent_one_enforcing_worker(client, plane):
    service, _ = api_v2.control_plane()
    worker(service, "cpu-only", dict(CAPS, memory_enforcement=False))
    worker(service, "ram-only", dict(CAPS, cpu_enforcement=False))
    assert not fleet(client)["cpu_limit_supported"]


def test_capabilities_can_be_nested_and_are_rechecked_when_saving(client, plane):
    service, _ = api_v2.control_plane()
    worker(service, caps={"runtime": CAPS})
    assert fleet(client)["cpu_limit_supported"]
    service.inventory.mark_degraded("pool", "lost contact after opening settings")
    assert client.post(f"/api/v2/settings/{FID}", json={"cpu_limit": "4"}).status_code == 400
    assert service.fleets.get(FID)["cpu_limit"] is None


@pytest.mark.parametrize("values", [{"cpu_limit": "1.5"}, {"cpu_limit": "65"},
                                    {"memory_limit": 2 * GIB}, {"memory_limit": 129 * GIB},
                                    {"memory_limit": 4 * GIB + 1}])
def test_defaults_refuse_values_that_the_appliance_runtime_cannot_apply(client, plane, values):
    service, _ = api_v2.control_plane()
    worker(service)
    assert client.post(f"/api/v2/settings/{FID}", json=values).status_code == 400


def test_placement_does_not_choose_legacy_guest_even_when_less_loaded():
    spec = {"runner_id": "new", "platform": "macos", "architecture": "x64",
            "cpu_limit": "4", "memory_limit": 8 * GIB}
    legacy = {"host_id": "legacy", "capabilities": dict(CAPS, appliance_per_runner=False)}
    pool = {"host_id": "pool", "capabilities": CAPS}
    placed = [{"runner_id": "existing", "host_id": "pool", "memory_limit": 8 * GIB}]
    assert placement.choose(spec, [legacy, pool], placed) == ("pool", None)
    host, reason = placement.choose(spec, [legacy], [])
    assert host is None and "enforcement is not supported" in reason
    pool["capabilities"] = dict(CAPS, memory_bytes=19 * GIB)
    host, reason = placement.choose(spec, [legacy, pool], placed)
    assert host is None and "not enough memory" in reason


def test_fleet_notice_and_runner_notice_do_not_confuse_new_defaults_with_current_enforcement(client, plane):
    service, _ = api_v2.control_plane()
    worker(service)
    row = next(row for row in client.get("/api/v2/fleet").json["fleets"] if row["fleet_id"] == FID)
    assert "New runners" in row["resource_notice"]
    rid = service.specs.create(provider="github", platform="macos", fleet_id=FID,
                               host_id="pool", capabilities={"kind": "macos-appliance"})
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]
    current = service.specs.get(rid)
    service.specs.update(rid, current["spec_version"], capabilities=CAPS)
    # Even matching saved capabilities cannot prove an existing instance.
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]
    service.inventory.mark_degraded("pool", "offline")
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]


def _limits(client, rid, body, key):
    return client.post(f"/api/v2/runners/{rid}/limits", json=body,
                       headers={"Idempotency-Key": key})


def test_a_runner_override_needs_its_own_worker_to_enforce_it(client, plane):
    """The same rule as a fleet default (store/fleets.py): a macOS runner's
    own CPU or RAM is refused unless its worker confirms per-runner
    appliance enforcement. Saved anyway, every later recreate of that
    runner - fleet-wide ones included - would be refused by placement."""
    service, _ = api_v2.control_plane()
    worker(service)
    worker(service, "legacy", dict(CAPS, appliance_per_runner=False))
    legacy = service.specs.create(provider="github", platform="macos", fleet_id=FID,
                                  host_id="legacy", actual_state="idle")
    for n, body in enumerate(({"cpu": 4}, {"memory": 8 * GIB})):
        refused = _limits(client, legacy, body, f"legacy-{n}")
        assert refused.status_code == 400, refused.json
        assert "per-runner appliance enforcement" in refused.json["error"]
    spec = service.specs.get(legacy)
    assert spec["cpu_override"] is None and spec["memory_override"] is None
    # Clearing one is always allowed: it asks for nothing to be enforced.
    assert _limits(client, legacy, {"cpu": None}, "legacy-clear").status_code == 202
    pooled = service.specs.create(provider="github", platform="macos", fleet_id=FID,
                                  host_id="pool", actual_state="idle")
    answer = _limits(client, pooled, {"cpu": 4, "memory": 8 * GIB}, "pool")
    assert answer.status_code == 202, answer.json
    assert service.specs.get(pooled)["cpu_override"] == "4"


def test_an_available_pool_does_not_prove_limits_on_a_legacy_runner(client, plane):
    service, _ = api_v2.control_plane()
    worker(service)
    worker(service, "legacy", dict(CAPS, appliance_per_runner=False))
    rid = service.specs.create(provider="github", platform="macos", fleet_id=FID,
                               host_id="legacy", capabilities=CAPS)
    assert fleet(client)["cpu_limit_supported"]
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]


@pytest.fixture
def provisioned_pool(plane, tmp_path):
    """Real provision, verb validation and appliance runtime; fake only OS/transport."""
    # The agent package sits beside dashboard/; importing the harness puts the
    # repository root on the path, so this runs on its own and not only after
    # a test that happened to import it first.
    import tests.agent_harness  # noqa: F401
    from agent import heartbeat, verbs
    from agent.runtimes.macos_pool import MacPoolRegistrar
    from agent.tests.test_macos_pool import Host, TEMPLATE, pool as pool_fixture
    from control.agent_runtime import AgentWiring, FlowAgent
    from control.provision import ProvisioningFlow

    host = Host()
    pool = pool_fixture.__wrapped__(tmp_path, host)
    agent = verbs.Agent("pool", pool, MacPoolRegistrar(pool))
    service, _ = api_v2.control_plane()
    worker(service, caps=dict(pool.capabilities(), architecture="x64", memory_bytes=32 * GIB))
    calls = []

    class Transport:
        def call_and_wait(self, host_id, verb, body, **kwargs):
            assert host_id == agent.host_id
            calls.append(verb)
            return verbs.dispatch(agent, verb, body)

    service.agents = AgentWiring(Transport(), images={("github", "macos"): TEMPLATE})
    flow = ProvisioningFlow(service, FlowAgent(service.agents), None)
    rid = service.specs.create(provider="github", platform="macos", architecture="x64",
                               fleet_id=FID, runtime_template=TEMPLATE,
                               cpu_limit="4", memory_limit=8 * GIB)

    def placed(host_id):
        spec = service.specs.get(rid)
        service.specs.update(rid, spec["spec_version"], host_id=host_id)

    result = flow.provision(service.specs.get(rid), on_placed=placed)
    spec = service.specs.get(rid)
    service.specs.update(rid, spec["spec_version"], actual_state="provisioned", **result)
    assert calls == ["exec_unit.status", "exec_unit.create"]
    assert not service.specs.get(rid).get("capabilities")

    def beat():
        payload = heartbeat.build(agent)
        service.inventory.accept_heartbeat("pool", payload)
        return payload

    return service, agent, host, rid, beat


def test_real_provision_heartbeat_detail_proves_instance_limits(client, provisioned_pool):
    service, agent, host, rid, beat = provisioned_pool
    detail = lambda: client.get(f"/api/v2/runners/{rid}").json
    assert "unverified" in detail()["resource_notice"]
    version = service.specs.get(rid)["spec_version"]
    payload = beat()
    proof = payload["instances"][0]["resource_enforcement"]
    assert proof["cpu_cores"] == 4 and proof["memory_limit_bytes"] == 8 * GIB
    assert service.specs.get(rid)["spec_version"] == version
    assert not service.specs.get(rid).get("capabilities")
    assert "This runner uses a separate appliance" in detail()["resource_notice"]
    assert "4 CPUs and 8 GiB" in detail()["resource_notice"]
    assert "fixed at" not in detail()["resource_notice"]
    # Editing desired limits cannot relabel observed current limits.
    service.specs.update(rid, version, cpu_limit="6", memory_limit=12 * GIB)
    assert "4 CPUs and 8 GiB" in detail()["resource_notice"]
    agent.runtime.stop(rid)
    beat()
    assert service.specs.get(rid)["unit_state"] == "stopped"
    assert "4 CPUs and 8 GiB" in detail()["resource_notice"]
    service.inventory.mark_degraded("pool", "offline")
    assert "unverified" in detail()["resource_notice"]


@pytest.mark.parametrize("failure", ["cpu", "memory", "ownership", "absent", "unreachable"])
def test_actual_appliance_drift_clears_previous_proof(client, provisioned_pool, failure):
    from agent import naming
    service, agent, host, rid, beat = provisioned_pool
    beat()
    unit = host.units[naming.unit_name(rid)]
    if failure == "cpu":
        unit["HostConfig"]["NanoCpus"] *= 2
    elif failure == "memory":
        unit["HostConfig"]["Memory"] *= 2
    elif failure == "ownership":
        unit["Config"]["Labels"]["nomercy.runner_id"] = "another-runner"
    elif failure == "absent":
        del host.units[naming.unit_name(rid)]
    else:
        host.unreachable = True
    payload = beat()
    assert "resource_enforcement" not in payload["instances"][0]
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]
    assert not service.specs.get(rid)["telemetry"].get("resource_enforcement")


def test_minimal_heartbeat_cannot_renew_old_instance_proof(client, provisioned_pool, monkeypatch):
    from agent import heartbeat
    from control import inventory
    service, agent, host, rid, beat = provisioned_pool
    payload = beat()
    measured = inventory._parse(payload["measured_at"])
    monkeypatch.setattr(inventory, "_now", lambda: measured + timedelta(seconds=31))
    service.inventory.accept_heartbeat("pool", heartbeat.minimal(agent))
    assert service.inventory.healthy()
    assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]


def test_foreign_worker_and_malformed_or_missing_proof_cannot_confirm_instance(client, provisioned_pool):
    from copy import deepcopy
    service, agent, host, rid, beat = provisioned_pool
    payload = beat()
    worker(service, "foreign")
    foreign = deepcopy(payload)
    foreign["host_id"] = "foreign"
    foreign["instances"][0]["resource_enforcement"]["cpu_cores"] = 64
    assert service.inventory.accept_heartbeat("foreign", foreign) == {}
    assert "4 CPUs" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]
    proof = payload["instances"][0]["resource_enforcement"]
    for value in (None, {"kind": "macos-appliance"}, dict(proof, cpu_cores=True),
                  dict(proof, memory_limit_bytes=2 ** 64)):
        changed = deepcopy(payload)
        changed["instances"][0]["resource_enforcement"] = value
        service.inventory.accept_heartbeat("pool", changed)
        assert "unverified" in client.get(f"/api/v2/runners/{rid}").json["resource_notice"]


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input":
            self.inputs[attributes["name"]] = attributes


@pytest.mark.parametrize("enforced", [False, True])
def test_settings_renderer_and_submission_follow_reported_capabilities(enforced):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runs the real settings JavaScript renderer")
    source = (Path(__file__).parents[1] / "templates/settings_v2.html").read_text(encoding="utf-8")
    functions = source[source.index("function field("):source.index("async function load()")]
    row = {"fleet_id": FID, "platform": "macos", "labels": [], "cpu_limit": "4", "memory_limit": 8 * GIB,
           "cpu_limit_supported": enforced, "memory_limit_supported": enforced,
           "disk_quota_supported": False, "guest_disk_virtual_bytes": 256 * GIB}
    script = "const esc=v=>String(v??'');\n" + functions + "\nconsole.log(fleetForm(" + json.dumps(row) + "));"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    inputs = Inputs(result.stdout).inputs
    for key in ("cpu_limit", "memory_limit"):
        assert ("disabled" not in inputs[key]) is enforced
        assert inputs[key]["step"] == "1"
    assert "disabled" in inputs["disk_limit"] and inputs["disk_limit"]["value"] == "256"
    entries = [[key, value.get("value", "")] for key, value in inputs.items()
               if "disabled" not in value and value.get("type") != "checkbox"]
    stub = ("class FormData {constructor(entries){this.entries=entries} [Symbol.iterator](){return this.entries[Symbol.iterator]()} "
            "has(k){return this.entries.some(e=>e[0]===k)} getAll(k){return this.entries.filter(e=>e[0]===k).map(e=>e[1])}}\n")
    script = stub + functions + "\nconsole.log(JSON.stringify(fleetDefaults(" + json.dumps(entries) + ")));"
    posted = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert "disk_limit" not in posted
    assert ("memory_limit" in posted) is enforced
    assert ("cpu_limit" in posted) is enforced
