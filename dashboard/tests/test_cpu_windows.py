"""A Linux fleet's whole-number CPU limit is a pinned window per runner.

On Linux a CFS quota does not change what `nproc` reports, so a build that
runs `-j$(nproc)` inside a quota-limited runner still starts one compiler per
host core. Only a cpuset does (2026-09-17). The thirteen runners were pinned
by hand to staggered 16-core windows; a runner planned later got nothing and
saw all 56 cores. These tests hold the rule that closes that gap: a whole
number on a Linux fleet gives every new runner its own window, placed where
the existing windows overlap least, and a recreate keeps the window it had.
"""
import pytest

import providers as P
from control import cpusets
from control import inventory as inv
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

G = 1024**3


class TestParseAndFormat:
    def test_ranges_and_lists(self):
        assert cpusets.parse("0-3,8,10-11") == {0, 1, 2, 3, 8, 10, 11}

    def test_a_window_that_wraps_is_two_ranges(self):
        assert cpusets.window(43, 16, 56) == "43-55,0-2"
        assert cpusets.parse("43-55,0-2") == set(range(43, 56)) | {0, 1, 2}

    def test_a_window_that_fits_is_one_range(self):
        assert cpusets.window(4, 16, 56) == "4-19"

    def test_is_cpuset(self):
        # The same rule the agent runtime applies: a range or a list is a
        # cpuset, a plain number is a quota.
        assert cpusets.is_cpuset("0-15") and cpusets.is_cpuset("3,5")
        assert not cpusets.is_cpuset("16") and not cpusets.is_cpuset("1.5")
        assert not cpusets.is_cpuset("") and not cpusets.is_cpuset(None)

    def test_rejects_garbage(self):
        for bad in ("a-b", "3-1", "-1", "1,,2"):
            with pytest.raises(ValueError):
                cpusets.parse(bad)


class TestAllocate:
    def test_the_first_window_starts_at_zero(self):
        assert cpusets.allocate(16, 56, []) == "0-15"

    def test_a_new_window_goes_where_overlap_is_least(self):
        taken = [cpusets.parse("0-15")]
        got = cpusets.parse(cpusets.allocate(16, 56, taken))
        assert not got & set(range(16))

    def test_thirteen_windows_cover_every_core_evenly(self):
        taken = []
        for _ in range(13):
            taken.append(cpusets.parse(cpusets.allocate(16, 56, taken)))
        cover = [sum(c in t for t in taken) for c in range(56)]
        assert min(cover) >= 3 and max(cover) <= 4

    def test_a_window_wider_than_the_host_is_the_whole_host(self):
        assert cpusets.allocate(80, 56, []) == "0-55"


def _service(tmp_path, cores=56):
    path = str(tmp_path / "control.db")
    schema.init(path)
    env = {"GH_TOKEN": "x", "GITHUB_ORG": "o"}
    FleetStore(path).seed(env)
    cells = {(p.key, pl): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for pl in P.PLATFORMS}
    service = RunnerService(path, runtimes=cells, env=env)
    service.inventory.register_worker("linux-1", inv.HYPERV_LINUX,
                                      capabilities={"kind": "linux-container",
                                                    "host_cores": cores})
    service.inventory.heartbeat("linux-1")
    return service


def _planned(service, fid, n):
    return [service.specs.get(r) for r in service.planned_ids(service.plan(fid, n))]


class TestPlanning:
    def test_a_whole_number_on_a_linux_fleet_pins_each_new_runner(self, tmp_path):
        service = _service(tmp_path)
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        specs = _planned(service, "github-linux-x64", 3)
        windows = [s["cpu_limit"] for s in specs]
        assert all(cpusets.is_cpuset(w) for w in windows)
        assert all(len(cpusets.parse(w)) == 16 for w in windows)
        assert len(set(windows)) == 3

    def test_new_windows_avoid_the_ones_already_in_use(self, tmp_path):
        service = _service(tmp_path)
        first = _planned(service, "github-linux-x64", 1)[0]
        service.specs.update(first["runner_id"], first["spec_version"],
                             cpu_limit="0-15")
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        new = _planned(service, "github-linux-x64", 1)[0]
        assert not cpusets.parse(new["cpu_limit"]) & set(range(16))

    def test_a_fraction_stays_a_quota(self, tmp_path):
        service = _service(tmp_path)
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 2.5})
        spec = _planned(service, "github-linux-x64", 1)[0]
        assert spec["cpu_limit"] == "2.5"

    def test_no_fleet_limit_leaves_the_runner_unpinned(self, tmp_path):
        service = _service(tmp_path)
        spec = _planned(service, "github-linux-x64", 1)[0]
        assert spec["cpu_limit"] is None


class TestReplacement:
    def test_a_recreate_keeps_a_window_of_the_right_width(self, tmp_path):
        service = _service(tmp_path)
        spec = _planned(service, "github-linux-x64", 1)[0]
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             cpu_limit="38-53")
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        replacement = service.replacement_spec(service.specs.get(spec["runner_id"]))
        assert replacement["cpu_limit"] == "38-53"

    def test_a_recreate_after_a_width_change_gets_a_new_window(self, tmp_path):
        service = _service(tmp_path)
        spec = _planned(service, "github-linux-x64", 1)[0]
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             cpu_limit="0-15")
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 8})
        replacement = service.replacement_spec(service.specs.get(spec["runner_id"]))
        assert len(cpusets.parse(replacement["cpu_limit"])) == 8

    def test_a_recreate_without_a_fleet_limit_keeps_its_own(self, tmp_path):
        service = _service(tmp_path)
        spec = _planned(service, "github-linux-x64", 1)[0]
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             cpu_limit="4-19")
        replacement = service.replacement_spec(service.specs.get(spec["runner_id"]))
        assert replacement["cpu_limit"] == "4-19"
