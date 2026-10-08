"""Every runner gets one and the same card, with every meter on it.

CPU, Memory, Storage and Cache, in that order, on every card, each a used
figure and the total it is a share of. The total is the runner's own limit
where it has one - its cores, its memory limit, its own disk, its cache cap -
and otherwise the real boundary it shares, flagged `shared`: the machine's
cores and memory, the volume its directory or its cache is on. A figure that
is not known right now is None in its slot; a meter is never missing.

The telemetry below is the shape each runtime's agent really sends, with the
fleet's own numbers (2026-10-08): Linux runners on a 56-core, 84.4 GB host
with a 107.37 GB disk and a 40 GB cache cap each; Windows x64 with 16 cores,
8.59 GB and its own 107.37 GB VHD; forgejo-windows-x64-2, a plain directory
on that same storage-enabled worker; the ARM64 guest's 8 cores, 3.22 GB and
no owned storage; and the macOS appliance, 6 logical CPUs, no limits, its
runners on a 274 GB Data volume.
"""
from datetime import datetime, timedelta, timezone

import pytest

import cards

NOW = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
GB = 10 ** 9
GIB = 1024 ** 3


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def spec(platform="linux", architecture="x64", telemetry=None, **kw):
    base = {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            "provider": "forgejo", "platform": platform,
            "architecture": architecture, "host_id": f"{platform}-1",
            "actual_state": "idle", "unit_state": "running",
            "last_seen_at": at(5), "forge_state": "idle",
            "forge_seen_at": at(20), "telemetry": telemetry or {}}
    base.update(kw)
    return base


LINUX = spec("linux", disk_limit=107374182400,
             cache_policy={"max_bytes": 40 * GB},
             telemetry={"at": at(3), "cpu_percent": 812.5, "cpu_cores": 16,
                        "host_cores": 56, "mem_used_bytes": 11 * GB,
                        "mem_limit_bytes": 32 * GIB,
                        "storage_bytes": 20 * GB, "storage_at": at(100),
                        "cache_bytes": 12 * GB, "cache_cap_bytes": 40 * GB,
                        "cache_at": at(100)})

WINDOWS_VHD = spec("windows", telemetry={
    "at": at(3), "cpu_percent": 150.0, "cpu_cores": 16, "host_cores": 56,
    "mem_used_bytes": 2 * GB, "mem_limit_bytes": 8589934592,
    "storage_bytes": 12 * GB, "storage_total_bytes": 107374182400,
    "storage_at": at(100),
    "cache_bytes": 3 * GB, "cache_at": at(100),
    "cache_volume_used_bytes": 12 * GB,
    "cache_volume_total_bytes": 107374182400, "cache_volume_at": at(100)})

WINDOWS_PLAIN = spec("windows", display_name="forgejo-windows-x64-2",
                     telemetry={
    "at": at(3), "cpu_percent": 40.0, "cpu_cores": 16, "host_cores": 56,
    "mem_used_bytes": 1 * GB, "mem_limit_bytes": 8589934592,
    "storage_bytes": 40 * GB, "storage_at": at(100),
    "storage_volume_used_bytes": 900 * GB,
    "storage_volume_total_bytes": 2000 * GB, "storage_volume_at": at(100),
    "cache_bytes": 5 * GB, "cache_at": at(100),
    "cache_volume_used_bytes": 900 * GB,
    "cache_volume_total_bytes": 2000 * GB, "cache_volume_at": at(100)})

ARM64 = spec("windows", "arm64", telemetry={
    "at": at(3), "cpu_percent": 12.0, "cpu_cores": 8, "host_cores": 8,
    "mem_used_bytes": 1 * GB, "mem_limit_bytes": 3221225472})

MACOS = spec("macos", telemetry={
    "at": at(3), "cpu_percent": 30.0, "cpu_cores": None, "host_cores": 6,
    "host_mem_bytes": 16 * GIB, "mem_used_bytes": 2 * GB,
    "mem_limit_bytes": None,
    "storage_volume_used_bytes": 160 * GB,
    "storage_volume_total_bytes": 274 * GB, "storage_volume_at": at(3),
    "storage_bytes": 30 * GB, "storage_at": at(100),
    "cache_bytes": 1 * GB, "cache_at": at(100),
    "cache_volume_used_bytes": 160 * GB,
    "cache_volume_total_bytes": 274 * GB, "cache_volume_at": at(100)})

EVERY = {"linux": LINUX, "windows-vhd": WINDOWS_VHD,
         "windows-plain": WINDOWS_PLAIN, "arm64": ARM64, "macos": MACOS}

METERS = ("cpu", "memory", "storage", "cache")


def card(s, **kw):
    return cards.from_spec(s, worker_reachable=True, now=NOW, **kw)


class TestOneShape:
    @pytest.mark.parametrize("name", EVERY)
    def test_every_card_has_all_four_meters(self, name):
        c = card(EVERY[name])
        for meter in METERS:
            assert isinstance(c[meter], dict), (name, meter)

    def test_each_meter_has_the_same_keys_on_every_card(self):
        shapes = {meter: {tuple(sorted(card(s)[meter])) for s in EVERY.values()}
                  for meter in METERS}
        assert all(len(keys) == 1 for keys in shapes.values()), shapes

    def test_a_runner_nobody_has_measured_yet_still_has_four_meters(self):
        c = card(spec("windows", telemetry={}))
        for meter in METERS:
            assert isinstance(c[meter], dict)
            assert c[meter]["total_bytes" if meter != "cpu"
                            else "total_cores"] is None
        assert c["storage"]["used_bytes"] is None


class TestOwnLimits:
    def test_a_linux_runner_is_bounded_by_its_own_everything(self):
        c = card(LINUX)
        assert c["cpu"] == {"percent": 812.5, "cores": 16, "host_cores": 56,
                            "total_cores": 16, "shared": False}
        assert c["memory"] == {"used_bytes": 11 * GB,
                               "limit_bytes": 32 * GIB, "host_bytes": None,
                               "total_bytes": 32 * GIB, "shared": False}
        assert c["storage"] == {"used_bytes": 20 * GB,
                                "total_bytes": 107374182400, "shared": False,
                                "volume_used_bytes": None,
                                "volume_total_bytes": None}
        assert c["cache"] == {"used_bytes": 12 * GB, "cap_bytes": 40 * GB,
                              "total_bytes": 40 * GB, "shared": False,
                              "volume_used_bytes": None,
                              "volume_total_bytes": None}

    def test_the_fleets_cache_policy_is_a_cap_when_the_unit_did_not_say(self):
        t = dict(LINUX["telemetry"])
        t.pop("cache_cap_bytes")
        c = card(dict(LINUX, telemetry=t,
                      cache_policy={"max_bytes": 20 * GB}))
        assert c["cache"]["total_bytes"] == 20 * GB
        assert c["cache"]["shared"] is False

    def test_a_windows_vhd_is_its_storage_and_the_volume_its_cache_shares(
            self):
        c = card(WINDOWS_VHD)
        assert c["cpu"]["total_cores"] == 16 and c["cpu"]["shared"] is False
        assert c["memory"]["total_bytes"] == 8589934592
        assert c["storage"]["total_bytes"] == 107374182400
        assert c["storage"]["shared"] is False
        assert c["cache"] == {"used_bytes": 3 * GB, "cap_bytes": None,
                              "total_bytes": 107374182400, "shared": True,
                              "volume_used_bytes": 12 * GB,
                              "volume_total_bytes": 107374182400}

    def test_the_measured_disk_size_wins_over_the_configured_one(self):
        c = card(dict(WINDOWS_VHD, disk_limit=100 * GIB))
        assert c["storage"]["total_bytes"] == 107374182400


class TestSharedBoundaries:
    def test_a_plain_directory_shares_the_volume_it_is_on(self):
        """forgejo-windows-x64-2 had no storage meter at all."""
        c = card(WINDOWS_PLAIN)
        assert c["storage"] == {"used_bytes": 40 * GB,
                                "total_bytes": 2000 * GB, "shared": True,
                                "volume_used_bytes": 900 * GB,
                                "volume_total_bytes": 2000 * GB}
        assert c["cache"]["shared"] is True
        assert c["cache"]["total_bytes"] == 2000 * GB

    def test_arm64_before_its_volume_is_measured_is_unknown_not_missing(self):
        c = card(ARM64)
        assert c["cpu"]["total_cores"] == 8
        assert c["memory"]["total_bytes"] == 3221225472
        assert c["storage"] == {"used_bytes": None, "total_bytes": None,
                                "shared": True, "volume_used_bytes": None,
                                "volume_total_bytes": None}
        assert c["cache"]["used_bytes"] is None

    def test_the_appliance_shares_the_guest_and_its_data_volume(self):
        c = card(MACOS)
        assert c["cpu"] == {"percent": 30.0, "cores": None, "host_cores": 6,
                            "total_cores": 6, "shared": True}
        assert c["memory"] == {"used_bytes": 2 * GB, "limit_bytes": None,
                               "host_bytes": 16 * GIB,
                               "total_bytes": 16 * GIB, "shared": True}
        assert c["storage"] == {"used_bytes": 30 * GB,
                                "total_bytes": 274 * GB, "shared": True,
                                "volume_used_bytes": 160 * GB,
                                "volume_total_bytes": 274 * GB}

    def test_the_appliances_system_volume_is_never_its_storage(self):
        """`/` on APFS is the sealed system volume. Its figures, still sent
        by an older agent, are not the runner's storage."""
        t = dict(MACOS["telemetry"], root_disk_used_bytes=11 * GB,
                 root_disk_total_bytes=100 * GIB)
        c = card(dict(MACOS, telemetry=t))
        assert c["storage"]["total_bytes"] == 274 * GB
        assert c["storage"]["used_bytes"] == 30 * GB

    def test_the_hosts_memory_comes_from_the_worker_when_beats_lack_it(self):
        t = dict(MACOS["telemetry"])
        t.pop("host_mem_bytes")
        c = card(dict(MACOS, telemetry=t),
                 host={"cpus": 56, "memory_bytes": 84400000000})
        assert c["memory"]["total_bytes"] == 84400000000
        assert c["memory"]["host_bytes"] == 84400000000
        assert c["memory"]["shared"] is True

    def test_the_hosts_cores_come_from_the_worker_when_beats_lack_them(self):
        t = dict(MACOS["telemetry"], host_cores=None)
        c = card(dict(MACOS, telemetry=t), host={"cpus": 6})
        assert c["cpu"]["total_cores"] == 6

    def test_a_beats_own_figure_wins_over_the_workers(self):
        c = card(MACOS, host={"cpus": 56, "memory_bytes": 84400000000})
        assert c["cpu"]["total_cores"] == 6
        assert c["memory"]["total_bytes"] == 16 * GIB


class TestFreshness:
    def test_an_old_volume_reading_is_unknown(self):
        t = dict(WINDOWS_PLAIN["telemetry"], storage_volume_at=at(3600),
                 storage_at=at(3600))
        c = card(dict(WINDOWS_PLAIN, telemetry=t))
        assert c["storage"]["used_bytes"] is None
        assert c["storage"]["volume_total_bytes"] is None
        assert c["storage"]["total_bytes"] is None
        assert c["storage"]["shared"] is True

    def test_an_own_disk_keeps_its_size_when_its_usage_is_old(self):
        t = dict(LINUX["telemetry"], storage_at=at(3600))
        c = card(dict(LINUX, telemetry=t))
        assert c["storage"]["used_bytes"] is None
        assert c["storage"]["total_bytes"] == 107374182400


def test_the_page_gives_each_card_its_workers_hardware(tmp_path):
    """A runner whose beats name no limit and no machine is a share of the
    worker it is placed on, as that worker declared its hardware."""
    import api_v2
    from control import inventory as inv
    from control.service import RunnerService
    from store import schema
    from store.fleets import FleetStore
    from tests.fake_runtime import ALL_CELLS
    from tests.test_partial_failure import GH

    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed({})
    service = RunnerService(path, runtimes=dict(ALL_CELLS))
    service.inventory.register_worker("linux-1", inv.HYPERV_LINUX, capabilities={
        "hardware": {"logical_cpus": 56, "memory_bytes": 84400000000}})
    rid = service.planned_ids(service.plan(GH, 1))[0]
    s = service.specs.get(rid)
    service.specs.update(rid, s["spec_version"], host_id="linux-1",
                         actual_state="idle")
    service.inventory.accept_heartbeat("linux-1", {
        "host_id": "linux-1",
        "instances": [{"runner_id": rid, "state": "running",
                       "telemetry": {"cpu_percent": 1.0,
                                     "mem_used_bytes": 10}}]})
    card = next(c for c in api_v2._controller_cards(service)
                if c["runner_id"] == rid)
    assert card["memory"]["total_bytes"] == 84400000000
    assert card["memory"]["shared"] is True
    assert card["cpu"]["total_cores"] == 56
    assert card["cpu"]["shared"] is True
