"""CPU and memory limits, as Settings and a runner's page set and show them.

The defaults are per platform cell - the fleet row - and bounded by the
hardware the cell's workers report. What a fleet does not set itself may
still come from the deployment (RUNNER_UNIT_MEMORY_*), which is a runtime
fallback the page shows as inherited and never copies into the database.
"""
import pytest

import api_v2
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
