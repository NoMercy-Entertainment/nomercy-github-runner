"""CPU and memory limits, as Settings and a runner's page set and show them.

The defaults are per platform cell - the fleet row - and bounded by the
hardware the cell's workers report. What a fleet does not set itself may
still come from the deployment (RUNNER_UNIT_MEMORY_*), which is a runtime
fallback the page shows as inherited and never copies into the database.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api_v2
from store import schema
from tests.test_controller_dashboard_integration import plane  # noqa: F401

GIB = 1024**3
LINUX_MEMORY = 84418977792          # rnr-linux-1, measured inside the VM


def live_workers(service):
    """The workers as they report today (2026-10-08)."""
    inventory = service.inventory
    inventory.register_worker("rnr-linux-1", "hyperv-linux", capabilities={
        "kind": "linux-container", "host_cores": 56,
        "memory_total_bytes": LINUX_MEMORY})
    inventory.register_worker("rnr-windows-1", "hyperv-windows", capabilities={
        "kind": "windows-process", "host_cores": 16})
    inventory.register_worker("windows-arm64-1", "hyperv-windows", capabilities={
        "kind": "windows-process", "host_cores": 8, "architecture": "arm64"})
    inventory.register_worker("macos-appliance-1", "hyperv-linux",
                              capabilities={"kind": "macos-appliance"})
    for host in ("rnr-linux-1", "rnr-windows-1", "windows-arm64-1",
                 "macos-appliance-1"):
        inventory.heartbeat(host)


def settings_row(client, fid):
    return next(f for f in client.get("/api/v2/settings").json["fleets"]
                if f["fleet_id"] == fid)


class TestSettingsShowsTheMaximum:
    def test_each_fleet_lists_its_hosts_and_the_largest(self, client, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        row = settings_row(client, "github-linux-x64")
        assert [h["host_id"] for h in row["hardware"]] == ["rnr-linux-1"]
        assert row["limits_max"]["cpus"] == 56
        assert row["limits_max"]["memory_bytes"] == LINUX_MEMORY
        arm = settings_row(client, "github-windows-arm64")
        assert arm["limits_max"]["cpus"] == 8
        assert arm["limits_max"]["memory_bytes"] is None

    def test_memory_the_deployment_supplies_is_shown_as_inherited(
            self, client, plane, monkeypatch):
        monkeypatch.setenv("RUNNER_UNIT_MEMORY_GITHUB_WINDOWS", "8g")
        monkeypatch.setenv("RUNNER_UNIT_MEMORY_GITHUB_WINDOWS_ARM64", "3g")
        service, _ = api_v2.control_plane()
        live_workers(service)
        x64 = settings_row(client, "github-windows-x64")
        arm = settings_row(client, "github-windows-arm64")
        assert x64["memory_inherited"] == {"bytes": 8 * GIB, "text": "8g"}
        assert arm["memory_inherited"] == {"bytes": 3 * GIB, "text": "3g"}
        assert x64["memory_limit"] is None, "never copied into the fleet row"
        assert settings_row(client, "github-linux-x64")["memory_inherited"] is None

    def test_unknown_hardware_is_flagged_not_refused(self, client, plane, monkeypatch):
        monkeypatch.setenv("RUNNER_UNIT_MEMORY_GITHUB_WINDOWS", "8g")
        service, _ = api_v2.control_plane()
        live_workers(service)
        saved = client.post("/api/v2/settings/github-windows-x64",
                            json={"cpu_limit": 16})
        assert saved.status_code == 200
        assert saved.json["hardware_unverified"] is True
        assert settings_row(client, "github-windows-x64")["hardware_unverified"] is True
        client.post("/api/v2/settings/github-linux-x64",
                    json={"cpu_limit": 16, "memory_limit": 32 * GIB})
        assert settings_row(client, "github-linux-x64")["hardware_unverified"] is False


class TestSettingsRefusesAboveTheMaximum:
    def test_more_cores_than_the_host_is_a_400_naming_it(self, client, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        refused = client.post("/api/v2/settings/github-linux-x64",
                              json={"cpu_limit": 64})
        assert refused.status_code == 400
        assert "rnr-linux-1" in refused.json["error"] and "56" in refused.json["error"]
        assert service.fleets.get("github-linux-x64")["cpu_limit"] is None

    def test_more_memory_than_the_host_is_a_400_naming_it(self, client, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        refused = client.post("/api/v2/settings/github-linux-x64",
                              json={"memory_limit": 96 * GIB})
        assert refused.status_code == 400
        assert "78.6 GiB" in refused.json["error"]

    def test_the_arm64_cell_is_bounded_by_its_own_host(self, client, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        assert client.post("/api/v2/settings/github-windows-arm64",
                           json={"cpu_limit": 8}).status_code == 200
        refused = client.post("/api/v2/settings/github-windows-arm64",
                              json={"cpu_limit": 16})
        assert refused.status_code == 400 and "windows-arm64-1" in refused.json["error"]


#: The deployment's memory seeds as they are set today (2026-10-08).
LIVE_ENV = {"RUNNER_UNIT_MEMORY_GITHUB_WINDOWS": "8g",
            "RUNNER_UNIT_MEMORY_FORGEJO_WINDOWS": "8g",
            "RUNNER_UNIT_MEMORY_GITHUB_WINDOWS_ARM64": "3g",
            "RUNNER_UNIT_MEMORY_FORGEJO_WINDOWS_ARM64": "3g"}


def live_fleets(service):
    """The fleet rows as they are today: every Linux and Windows fleet has a
    CPU limit, the Linux ones their own memory, the Windows ones none -
    their memory comes from the deployment - and macOS nothing at all."""
    service.fleets.set_defaults("github-linux-x64",
                                {"cpu_limit": 16, "memory_limit": 32 * GIB})
    service.fleets.set_defaults("forgejo-linux-x64",
                                {"cpu_limit": 16, "memory_limit": 6 * GIB})
    for fid in ("github-windows-x64", "forgejo-windows-x64"):
        service.fleets.set_defaults(fid, {"cpu_limit": 16})
    for fid in ("github-windows-arm64", "forgejo-windows-arm64"):
        service.fleets.set_defaults(fid, {"cpu_limit": 8})


def fleet_row(client, fid):
    return next(f for f in client.get("/api/v2/fleet").json["fleets"]
                if f["fleet_id"] == fid)


def add_action(row):
    return next(a for a in row["actions"] if a["verb"] == "add")


@pytest.mark.require_limits
class TestLinuxAndWindowsNeedLimits:
    def test_the_live_configuration_refuses_nothing(self, client, plane, monkeypatch):
        """The rule must not take a single serving fleet down: the Windows
        fleets set no memory of their own and get it from the deployment."""
        for key, value in LIVE_ENV.items():
            monkeypatch.setenv(key, value)
        service, _ = api_v2.control_plane()
        live_workers(service)
        live_fleets(service)
        for fleet in service.fleets.list():
            assert service.limits_problem(fleet) is None, fleet["fleet_id"]
        for row in client.get("/api/v2/fleet").json["fleets"]:
            assert row["limits_problem"] is None, row["fleet_id"]
            assert "limit" not in (add_action(row)["reason"] or ""), row["fleet_id"]
        for row in client.get("/api/v2/settings").json["fleets"]:
            assert row["limits_problem"] is None, row["fleet_id"]
        for fid in ("github-linux-x64", "forgejo-linux-x64", "github-windows-x64",
                    "github-windows-arm64"):
            assert service.create(fid, 1, idempotency_key=f"live-{fid}")

    def test_a_fleet_without_memory_cannot_add_a_runner(self, client, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        why = service.limits_problem(service.fleets.get("github-linux-x64"))
        assert why and "memory" in why and "Settings" in why
        refused = client.post("/api/v2/fleets/github-linux-x64/runners", json={},
                              headers={"Idempotency-Key": "add-1"})
        assert refused.status_code == 409 and "memory" in refused.json["error"]
        assert service.fleets.get("github-linux-x64")["desired_capacity"] == 0
        row = fleet_row(client, "github-linux-x64")
        assert row["limits_problem"] == why
        assert add_action(row)["enabled"] is False
        assert add_action(row)["reason"] == why

    def test_a_fleet_without_a_cpu_limit_says_so(self, client, plane):
        service, _ = api_v2.control_plane()
        service.fleets.set_defaults("github-windows-x64", {"memory_limit": 8 * GIB})
        why = service.limits_problem(service.fleets.get("github-windows-x64"))
        assert why and "CPU" in why and "memory" not in why

    def test_plan_refuses_before_any_runner_exists(self, plane):
        from control.service import Refused
        service, _ = api_v2.control_plane()
        live_workers(service)
        with pytest.raises(Refused, match="CPU"):
            service.plan("github-linux-x64", 1)
        assert service.specs.list() == []

    def test_a_scale_down_is_never_refused(self, plane):
        service, _ = api_v2.control_plane()
        with schema.connect(plane) as c:
            c.execute("UPDATE fleets SET desired_capacity=2 WHERE fleet_id='github-linux-x64'")
        assert service.scale_down("github-linux-x64", 1)

    def test_a_recreate_needs_a_limit_from_somewhere(self, plane):
        from control.service import Refused
        service, _ = api_v2.control_plane()
        live_workers(service)
        fleet = service.fleets.get("github-linux-x64")
        bare = {"platform": "linux", "fleet_id": "github-linux-x64"}
        assert service.limits_problem(fleet, bare)
        # What a recreate resolves again does not count: memory the runner
        # had is rebuilt from override, fleet or deployment (replacement_spec).
        own = dict(bare, cpu_limit="0-15", memory_limit=8 * GIB)
        assert "memory" in service.limits_problem(fleet, own)
        assert service.limits_problem(fleet, dict(own, memory_override=8 * GIB)) is None
        quota = dict(bare, cpu_limit="2.5", memory_override=8 * GIB)
        assert "CPU" in service.limits_problem(fleet, quota), "only a window is kept"
        rid = service.specs.create(provider="github", platform="linux",
                                   fleet_id="github-linux-x64", host_id="rnr-linux-1",
                                   actual_state="idle")
        with pytest.raises(Refused, match="limit"):
            service.validate_replacement(service.specs.get(rid))

    def test_macos_is_exempt(self, plane):
        service, _ = api_v2.control_plane()
        assert service.limits_problem(service.fleets.get("github-macos-x64")) is None

    def test_adopting_a_serving_runner_is_exempt(self, plane):
        service, _ = api_v2.control_plane()
        live_workers(service)
        rid = service.adopt("github-linux-x64", "github-linux-x64-1", "rnr-linux-1",
                            {"id": "42"}, {"label": "existing"})
        assert service.specs.get(rid)["fleet_id"] == "github-linux-x64"

    def test_which_platforms_need_limits_is_data(self, plane, monkeypatch):
        import providers
        from control import service as service_module
        monkeypatch.setattr(service_module, "REQUIRE_LIMITS",
                            frozenset({providers.LINUX}))
        service, _ = api_v2.control_plane()
        assert service.limits_problem(service.fleets.get("github-windows-x64")) is None
        assert service.limits_problem(service.fleets.get("github-linux-x64"))


def a_runner(service, state="idle", fid="github-linux-x64", host="rnr-linux-1",
             **fields):
    provider, platform, arch = fid.split("-")
    return service.specs.create(provider=provider, platform=platform,
                                architecture=arch, fleet_id=fid, host_id=host,
                                actual_state=state, exec_unit_ref="unit-1",
                                **fields)


def set_limits(client, rid, body, key="limits-1"):
    return client.post(f"/api/v2/runners/{rid}/limits", json=body,
                       headers={"Idempotency-Key": key})


@pytest.fixture
def linux(client, plane):
    service, _ = api_v2.control_plane()
    live_workers(service)
    service.fleets.set_defaults("github-linux-x64",
                                {"cpu_limit": 16, "memory_limit": 32 * GIB})
    return service


class TestARunnersOwnLimits:
    """An admin may give one runner its own CPU and memory. They outrank the
    fleet and the deployment, are checked against that runner's own host, and
    take effect through a recreate under the usual rules - or wait for one,
    saved and marked pending, when a recreate cannot be queued now."""

    def test_an_override_is_saved_and_a_recreate_queued(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        answer = set_limits(client, rid, {"cpu": 8, "memory": 8 * GIB})
        assert answer.status_code == 202, answer.json
        assert answer.json["pending"] is False
        spec = linux.specs.get(rid)
        assert spec["cpu_override"] == "8.0" and spec["memory_override"] == 8 * GIB
        assert spec["cpu_limit"] == "0-15", "the resolved value changes on recreate"
        recreate = linux.operations.get(answer.json["recreate_operation_id"])
        assert recreate["verb"] == "recreate"
        assert spec["current_operation"] == recreate["operation_id"]

    def test_a_busy_runner_is_recreated_the_usual_way(self, client, linux):
        """A recreate of a busy runner drains it first; the job finishes."""
        rid = a_runner(linux, state="busy", cpu_limit="0-15", memory_limit=32 * GIB)
        answer = set_limits(client, rid, {"memory": 8 * GIB})
        assert answer.status_code == 202 and answer.json["pending"] is False
        assert linux.specs.get(rid)["actual_state"] == "busy"

    @pytest.mark.parametrize("state", ["draining", "provisioning"])
    def test_when_no_recreate_can_be_queued_the_override_waits(self, client, linux, state):
        rid = a_runner(linux, state=state, cpu_limit="0-15", memory_limit=32 * GIB)
        answer = set_limits(client, rid, {"memory": 8 * GIB})
        assert answer.status_code == 202, answer.json
        assert answer.json["pending"] is True
        assert answer.json["recreate_operation_id"] is None
        assert "pending recreate" in answer.json["note"]
        assert linux.specs.get(rid)["memory_override"] == 8 * GIB
        assert client.get(f"/api/v2/runners/{rid}").json["limits"]["pending"] is True

    def test_an_operation_in_flight_leaves_it_pending(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        linux.act(rid, "stop", idempotency_key="stop-1")
        answer = set_limits(client, rid, {"cpu": 8})
        assert answer.status_code == 202 and answer.json["pending"] is True
        assert linux.specs.get(rid)["cpu_override"] == "8.0"

    def test_a_repeat_is_the_same_request(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        first = set_limits(client, rid, {"cpu": 8})
        again = set_limits(client, rid, {"cpu": 8})
        assert again.status_code == 202
        assert again.json["operation_id"] == first.json["operation_id"]
        assert len(linux.operations.list(runner_id=rid)) == 2   # set_limits, recreate

    def test_more_than_the_runners_host_has_is_a_400_naming_it(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        refused = set_limits(client, rid, {"memory": 96 * GIB})
        assert refused.status_code == 400
        assert "rnr-linux-1" in refused.json["error"] and "78.6 GiB" in refused.json["error"]
        refused = set_limits(client, rid, {"cpu": 64}, key="limits-2")
        assert refused.status_code == 400 and "56" in refused.json["error"]
        spec = linux.specs.get(rid)
        assert spec["cpu_override"] is None and spec["memory_override"] is None

    def test_it_is_the_runners_own_host_that_bounds_it(self, client, linux):
        linux.inventory.register_worker("rnr-linux-2", "hyperv-linux", capabilities={
            "kind": "linux-container", "host_cores": 8})
        linux.inventory.heartbeat("rnr-linux-2")
        rid = a_runner(linux, host="rnr-linux-2", cpu_limit="0-7", memory_limit=8 * GIB)
        refused = set_limits(client, rid, {"cpu": 16})
        assert refused.status_code == 400
        assert "rnr-linux-2" in refused.json["error"]
        assert "rnr-linux-1" not in refused.json["error"]

    def test_unknown_hardware_is_accepted_and_flagged(self, client, linux):
        rid = a_runner(linux, fid="github-windows-x64", host="rnr-windows-1",
                       cpu_limit="0-15", memory_limit=8 * GIB)
        answer = set_limits(client, rid, {"memory": 6 * GIB})
        assert answer.status_code == 202, answer.json
        assert answer.json["hardware_unverified"] is True

    def test_null_clears_an_override(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB,
                       cpu_override="8.0", memory_override=8 * GIB)
        answer = set_limits(client, rid, {"cpu": None})
        assert answer.status_code == 202
        spec = linux.specs.get(rid)
        assert spec["cpu_override"] is None and spec["memory_override"] == 8 * GIB

    @pytest.mark.parametrize("body", [{}, {"cpu": "abc"}, {"memory": "8g"},
                                      {"memory": 0}, {"cpu": True},
                                      {"disk": 1}, ["cpu"]])
    def test_nonsense_is_a_400(self, client, linux, body):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        assert set_limits(client, rid, body).status_code == 400
        assert linux.specs.get(rid)["cpu_override"] is None

    @pytest.mark.parametrize("close", [None, "fail"])
    def test_a_repeat_of_a_request_that_never_finished_is_a_409(self, client, linux, close):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        operation, _ = linux.operations.open("set_limits", runner_id=rid,
                                             idempotency_key="limits-1")
        if close:
            linux.operations.fail(operation["operation_id"], "it broke")
        answer = set_limits(client, rid, {"cpu": 8})
        assert answer.status_code == 409, answer.json
        assert operation["operation_id"] in answer.json["error"]
        assert linux.specs.get(rid)["cpu_override"] is None

    def test_a_key_used_for_something_else_is_a_409(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        linux.act(rid, "stop", idempotency_key="limits-1")
        assert set_limits(client, rid, {"cpu": 8}).status_code == 409

    def stale(self, monkeypatch, times):
        from store.specs import SpecStore, StaleSpec
        real = SpecStore.update
        left = {"n": times}

        def update(self, runner_id, version, **changes):
            if "cpu_override" in changes and left["n"]:
                left["n"] -= 1
                raise StaleSpec(runner_id, version, version + 1)
            return real(self, runner_id, version, **changes)
        monkeypatch.setattr(SpecStore, "update", update)

    def test_a_runner_that_moved_on_once_is_read_again(self, client, linux, monkeypatch):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        self.stale(monkeypatch, 1)
        answer = set_limits(client, rid, {"cpu": 8})
        assert answer.status_code == 202, answer.json
        assert linux.specs.get(rid)["cpu_override"] == "8.0"

    def test_one_that_keeps_moving_is_refused_closed_and_audited(self, client, linux, monkeypatch):
        from control import audit
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        self.stale(monkeypatch, 5)
        answer = set_limits(client, rid, {"cpu": 8})
        assert answer.status_code == 409 and "changed" in answer.json["error"]
        assert linux.specs.get(rid)["cpu_override"] is None
        operations = linux.operations.list(runner_id=rid)
        assert [o["state"] for o in operations] == ["failed"]
        rows = audit.entries(linux.operations.path, verb="set_limits", decision="refused")
        assert rows and "changed" in rows[0]["outcome"]
        again = set_limits(client, rid, {"cpu": 8})
        assert again.status_code == 409, "the key's failed operation is the answer"

    def test_an_unexpected_failure_still_closes_and_audits(self, linux, monkeypatch):
        from control import audit
        from store.specs import SpecStore
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)

        def broken(self, *args, **kwargs):
            raise RuntimeError("disk I/O error")
        monkeypatch.setattr(SpecStore, "update", broken)
        with pytest.raises(RuntimeError):
            linux.set_limits(rid, {"cpu": 8}, idempotency_key="k")
        assert [o["state"] for o in linux.operations.list(runner_id=rid)] == ["failed"]
        assert audit.entries(linux.operations.path, verb="set_limits", decision="refused")

    def test_a_key_is_required(self, client, linux):
        rid = a_runner(linux)
        assert client.post(f"/api/v2/runners/{rid}/limits",
                           json={"cpu": 8}).status_code == 400

    def test_an_unknown_runner_is_a_404(self, client, linux):
        assert set_limits(client, "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                          {"cpu": 8}).status_code == 404

    def test_every_request_is_audited(self, client, linux):
        from control import audit
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        set_limits(client, rid, {"cpu": 8})
        set_limits(client, rid, {"cpu": 64}, key="limits-2")
        rows = [r for r in audit.entries(linux.operations.path, verb="set_limits",
                                         runner_id=rid)
                if r["decision"] in ("accepted", "refused")]
        assert [r["decision"] for r in rows] == ["refused", "accepted"]
        assert "56" in rows[0]["outcome"]

    def test_an_unauthenticated_caller_is_refused(self, anon_client, plane):
        r = anon_client.post("/api/v2/runners/3f2504e0-4f89-41d3-9a0c-0305e82c3301/limits",
                             json={"cpu": 8}, headers={"Idempotency-Key": "k"})
        assert r.status_code == 401


class TestTheRunnerPageShowsItsLimits:
    def test_where_each_value_comes_from(self, client, linux, monkeypatch):
        rid = a_runner(linux, cpu_limit="0-15", memory_limit=32 * GIB)
        limits = client.get(f"/api/v2/runners/{rid}").json["limits"]
        assert limits["cpu"]["value"] == "0-15" and limits["cpu"]["cores"] == 16
        assert limits["cpu"]["source"] == "fleet setting"
        assert limits["memory"]["value"] == 32 * GIB
        assert limits["memory"]["source"] == "fleet setting"
        assert limits["max"]["cpus"] == 56 and limits["max"]["memory_bytes"] == LINUX_MEMORY
        assert limits["max"]["host_id"] == "rnr-linux-1"
        assert limits["pending"] is False

    def test_an_override_is_named_as_one(self, client, linux):
        rid = a_runner(linux, cpu_limit="0-7", memory_limit=8 * GIB,
                       cpu_override="8.0", memory_override=8 * GIB)
        limits = client.get(f"/api/v2/runners/{rid}").json["limits"]
        assert limits["cpu"]["source"] == "runner override"
        assert limits["memory"]["source"] == "runner override"
        assert limits["override"] == {"cpu": "8.0", "memory": 8 * GIB}
        assert limits["pending"] is False

    def test_memory_from_the_deployment_is_named_as_that(self, client, plane, monkeypatch):
        monkeypatch.setenv("RUNNER_UNIT_MEMORY_GITHUB_WINDOWS", "8g")
        service, _ = api_v2.control_plane()
        live_workers(service)
        rid = a_runner(service, fid="github-windows-x64", host="rnr-windows-1",
                       memory_limit=8 * GIB)
        limits = client.get(f"/api/v2/runners/{rid}").json["limits"]
        assert limits["memory"]["source"] == "deployment default"
        assert limits["memory"]["inherited"] == "8g"
        assert limits["max"]["memory_bytes"] is None


TEMPLATES = Path(__file__).parents[1] / "templates"


def run_page_js(page, start, end, call):
    node = shutil.which("node")
    if not node:
        pytest.skip("node runs the real page JavaScript")
    source = (TEMPLATES / page).read_text(encoding="utf-8")
    functions = source[source.index(start):source.index(end)]
    script = ("const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>'&#'+c.charCodeAt(0)+';');\n"
              + functions + "\nconsole.log(" + call + ");")
    return subprocess.run([node, "-e", script], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


class TestThePagesShowIt:
    def test_settings_shows_the_maximum_and_what_is_inherited(self):
        row = {"fleet_id": "github-windows-x64", "platform": "windows", "labels": [],
               "cpu_limit": "16.0", "memory_limit": None,
               "cpu_limit_supported": True, "memory_limit_supported": True,
               "disk_quota_supported": True,
               "limits_max": {"cpus": 16, "memory_bytes": None, "cpus_host": "rnr-windows-1",
                              "memory_host": None, "memory_source": None},
               "memory_inherited": {"bytes": 8 * GIB, "text": "8g"},
               "hardware_unverified": True, "limits_problem": None}
        html = run_page_js("settings_v2.html", "function field(", "async function load()",
                           "fleetForm(" + json.dumps(row) + ")")
        assert 'max="16"' in html
        assert 'placeholder="inherited from deployment: 8g"' in html
        assert "Max: 16 cores · memory unknown (rnr-windows-1)" in html
        assert "Not verified" in html
        assert "Empty limits inherit deployment defaults" not in html

    def test_settings_says_why_a_fleet_cannot_add_runners(self):
        row = {"fleet_id": "github-linux-x64", "platform": "linux", "labels": [],
               "cpu_limit": "16.0", "memory_limit": None,
               "cpu_limit_supported": True, "memory_limit_supported": True,
               "disk_quota_supported": True,
               "limits_max": {"cpus": 56, "memory_bytes": LINUX_MEMORY,
                              "cpus_host": "rnr-linux-1", "memory_host": "rnr-linux-1",
                              "memory_source": "measured"},
               "memory_inherited": None, "hardware_unverified": False,
               "limits_problem": "github-linux-x64 has no memory limit"}
        html = run_page_js("settings_v2.html", "function field(", "async function load()",
                           "fleetForm(" + json.dumps(row) + ")")
        assert "github-linux-x64 has no memory limit" in html
        assert "Max: 56 cores · 78.6 GiB (rnr-linux-1)" in html
        assert 'max="78.6"' in html

    def test_the_runner_page_shows_values_sources_max_and_pending(self):
        limits = {"cpu": {"value": "0-15", "cores": 16, "source": "fleet setting"},
                  "memory": {"value": 8 * GIB, "source": "deployment default",
                             "inherited": "8g"},
                  "override": {"cpu": None, "memory": 6 * GIB},
                  "max": {"host_id": "rnr-windows-1", "cpus": 16, "memory_bytes": None,
                          "memory_source": None},
                  "pending": True}
        html = run_page_js("runner_v2.html", "const GIB", "async function load()",
                           "limitsHTML(" + json.dumps(limits) + ")")
        assert "16 cores (cores 0-15)" in html
        assert "8 GiB" in html and "inherited from deployment 8g" in html
        assert "16 cores · memory unknown (rnr-windows-1)" in html
        assert "pending recreate" in html

    def test_only_an_admin_gets_the_runner_limits_form(self, client, plane):
        import users
        rid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert b'id="limits-form"' in client.get(f"/runners/{rid}").data
        users.approve("sub-test-admin", "operator")
        page = client.get(f"/runners/{rid}").data
        assert b'id="limits"' in page and b'id="limits-form"' not in page


def test_the_rule_is_lifted_for_tests_that_are_not_about_it(plane):
    service, _ = api_v2.control_plane()
    assert service.limits_problem(service.fleets.get("github-linux-x64")) is None
