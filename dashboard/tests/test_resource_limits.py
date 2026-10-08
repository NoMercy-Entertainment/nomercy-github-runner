"""CPU and memory limits, as Settings and a runner's page set and show them.

The defaults are per platform cell - the fleet row - and bounded by the
hardware the cell's workers report. What a fleet does not set itself may
still come from the deployment (RUNNER_UNIT_MEMORY_*), which is a runtime
fallback the page shows as inherited and never copies into the database.
"""
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
        own = dict(bare, cpu_limit="0-15", memory_limit=8 * GIB)
        assert service.limits_problem(fleet, own) is None
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


def test_the_rule_is_lifted_for_tests_that_are_not_about_it(plane):
    service, _ = api_v2.control_plane()
    assert service.limits_problem(service.fleets.get("github-linux-x64")) is None
