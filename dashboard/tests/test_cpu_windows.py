"""A pinned fleet's whole-number CPU limit is a window of cores per runner.

On Linux a CFS quota does not change what `nproc` reports, so a build that
runs `-j$(nproc)` inside a quota-limited runner still starts one compiler per
host core. Only a cpuset does (2026-09-17). The thirteen runners were pinned
by hand to staggered 16-core windows; a runner planned later got nothing and
saw all 56 cores. These tests hold the rule that closes that gap: a whole
number on a Linux fleet gives every new runner its own window, placed where
the existing windows overlap least, and a recreate keeps the window it had.

A Windows runner has the same gap: its CPU rate cap does not change what a
build sees as its processor count either, and only a Job Object affinity mask
does (`agent/jobhost.py:affinity_mask`) - so the same rule pins it, cut from
the same host of 56 (2026-09-23). Its host does not declare `host_cores` in
its capabilities the way a Linux worker does, so pinning it depends on the
fallback to a runner's last measured telemetry; a test below holds that path.
"""
import pytest

import providers as P
from control import cpusets
from control import inventory as inv
from control.service import Refused, RunnerService
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

    def test_a_window_wider_than_the_host_is_refused_naming_both(self):
        # Never silently narrowed (2026-09-23): a runner told to pin fewer
        # cores than its fleet asked for is a runner quietly running with
        # less than it was promised, and this is where both numbers - what
        # was asked and what the host actually has - are known together.
        with pytest.raises(ValueError) as excinfo:
            cpusets.allocate(80, 56, [])
        assert "80" in str(excinfo.value) and "56" in str(excinfo.value)


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


def _win_service(tmp_path, cores=56, declare_host_cores=True):
    """A deployment with one healthy Windows worker. Matching the live fleet
    (2026-09-23), it declares no `host_cores` in its capabilities unless
    asked to - only a Linux worker does that today."""
    path = str(tmp_path / "control.db")
    schema.init(path)
    env = {"GH_TOKEN": "x", "GITHUB_ORG": "o"}
    FleetStore(path).seed(env)
    cells = {(p.key, pl): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for pl in P.PLATFORMS}
    service = RunnerService(path, runtimes=cells, env=env)
    capabilities = {"kind": "windows-process"}
    if declare_host_cores:
        capabilities["host_cores"] = cores
    service.inventory.register_worker("windows-1", inv.HYPERV_WINDOWS,
                                      capabilities=capabilities)
    service.inventory.heartbeat("windows-1")
    return service


def _mixed_service(tmp_path, linux_cores=56, windows_cores=56):
    """One store with a healthy worker of each pinned platform - a Linux
    fleet and a Windows fleet side by side, the way the live deployment
    actually is. `_service` and `_win_service` above each build their own,
    separate database, which is exactly why a Linux fleet's windows leaking
    into a Windows placement (and the other way round) went unnoticed: two
    physically different hosts, each with its own independent processor
    numbering, never appeared in the same store together (2026-09-23)."""
    path = str(tmp_path / "control.db")
    schema.init(path)
    env = {"GH_TOKEN": "x", "GITHUB_ORG": "o"}
    FleetStore(path).seed(env)
    cells = {(p.key, pl): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for pl in P.PLATFORMS}
    service = RunnerService(path, runtimes=cells, env=env)
    service.inventory.register_worker("linux-1", inv.HYPERV_LINUX,
                                      capabilities={"kind": "linux-container",
                                                    "host_cores": linux_cores})
    service.inventory.register_worker("windows-1", inv.HYPERV_WINDOWS,
                                      capabilities={"kind": "windows-process",
                                                    "host_cores": windows_cores})
    service.inventory.heartbeat("linux-1")
    service.inventory.heartbeat("windows-1")
    return service


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


class TestWindowsPlanning:
    """The same rule, on the platform that gets it next: a Windows worker's
    quota (the Job Object's CPU rate) does not change what a build sees as
    its processor count either, so a whole-number fleet limit still means a
    window, cut from the same 56-core host the live fleet runs on."""

    def test_a_whole_number_on_a_windows_fleet_pins_each_new_runner(self, tmp_path):
        service = _win_service(tmp_path)
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        specs = _planned(service, "github-windows-x64", 3)
        windows = [s["cpu_limit"] for s in specs]
        assert all(cpusets.is_cpuset(w) for w in windows)
        assert all(len(cpusets.parse(w)) == 16 for w in windows)
        assert len(set(windows)) == 3

    def test_windows_on_the_same_worker_overlap_as_little_as_possible(self, tmp_path):
        service = _win_service(tmp_path)
        first = _planned(service, "github-windows-x64", 1)[0]
        service.specs.update(first["runner_id"], first["spec_version"],
                             cpu_limit="0-15")
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        new = _planned(service, "github-windows-x64", 1)[0]
        assert not cpusets.parse(new["cpu_limit"]) & set(range(16))

    def test_a_fraction_stays_a_quota_on_windows(self, tmp_path):
        service = _win_service(tmp_path)
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 2.5})
        spec = _planned(service, "github-windows-x64", 1)[0]
        assert spec["cpu_limit"] == "2.5"

    def test_no_fleet_limit_leaves_the_windows_runner_unpinned(self, tmp_path):
        service = _win_service(tmp_path)
        spec = _planned(service, "github-windows-x64", 1)[0]
        assert spec["cpu_limit"] is None


class TestWindowsReplacement:
    def test_a_recreate_keeps_a_window_of_the_right_width(self, tmp_path):
        service = _win_service(tmp_path)
        spec = _planned(service, "github-windows-x64", 1)[0]
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             cpu_limit="38-53")
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        replacement = service.replacement_spec(service.specs.get(spec["runner_id"]))
        assert replacement["cpu_limit"] == "38-53"

    def test_a_recreate_after_a_width_change_gets_a_new_window(self, tmp_path):
        service = _win_service(tmp_path)
        spec = _planned(service, "github-windows-x64", 1)[0]
        service.specs.update(spec["runner_id"], spec["spec_version"],
                             cpu_limit="0-15")
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 8})
        replacement = service.replacement_spec(service.specs.get(spec["runner_id"]))
        assert len(cpusets.parse(replacement["cpu_limit"])) == 8


class TestWindowsHostCoresFallback:
    """The live Windows worker declares no `host_cores` in its capabilities -
    only a Linux worker does that. Pinning a Windows fleet therefore depends
    on the fallback to what a runner on it last reported in its telemetry
    (`WindowsProcessRuntime.telemetry`'s own `host_cores`, agent/runtimes/
    windows_process.py)."""

    def test_a_windows_worker_with_no_declared_host_cores_still_pins(
            self, tmp_path):
        service = _win_service(tmp_path, declare_host_cores=False)
        # Born unpinned - the fleet names no limit yet - then placed and
        # heard from, the way a runner already on a worker reports its
        # telemetry before this fleet is ever pinned.
        first = _planned(service, "github-windows-x64", 1)[0]
        service.specs.update(first["runner_id"], first["spec_version"],
                             host_id="windows-1", actual_state="idle")
        service.inventory.accept_heartbeat("windows-1", {
            "host_id": "windows-1",
            "instances": [{"runner_id": first["runner_id"],
                           "state": "running",
                           "telemetry": {"host_cores": 56}}]})

        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        second = _planned(service, "github-windows-x64", 1)[0]
        assert cpusets.is_cpuset(second["cpu_limit"])
        assert len(cpusets.parse(second["cpu_limit"])) == 16

    def test_with_no_worker_declaration_and_no_telemetry_yet_pinning_is_refused(
            self, tmp_path):
        service = _win_service(tmp_path, declare_host_cores=False)
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        with pytest.raises(Refused):
            service.plan("github-windows-x64", 1)


class TestCrossPlatformIsolation:
    """A Linux worker and a Windows worker are physically different hosts,
    each numbering its own processors from 0 - core 5 on one says nothing
    about core 5 on the other. Before `_host_cores` and `_cpu_window` took a
    platform (2026-09-23), both were computed by pooling every pinned
    platform's workers and specs together: a Linux fleet's windows counted
    as occupied cores when a Windows window was chosen, and the smaller of
    the two hosts' core counts silently became the ceiling for both. Each
    test here would have failed against that pooled code."""

    def test_a_windows_window_ignores_cores_a_linux_fleet_has_taken(
            self, tmp_path):
        service = _mixed_service(tmp_path)
        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        # Two Linux windows take cores 0-31 on the Linux host.
        _planned(service, "github-linux-x64", 2)

        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        first = _planned(service, "github-windows-x64", 1)[0]
        # Nothing is taken on the Windows host, so the first window there is
        # still 0-15 - pooled, it would have been pushed past 31 to dodge
        # cores the Linux fleet occupies on a different machine.
        assert first["cpu_limit"] == "0-15"

    def test_a_linux_window_ignores_cores_a_windows_fleet_has_taken(
            self, tmp_path):
        service = _mixed_service(tmp_path)
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        _planned(service, "github-windows-x64", 2)

        service.fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})
        first = _planned(service, "github-linux-x64", 1)[0]
        assert first["cpu_limit"] == "0-15"

    def test_host_core_counts_do_not_pool_across_platforms(self, tmp_path):
        # The Linux host has fewer cores than the Windows one; pooled, the
        # smaller number would wrongly cap the Windows window's width too.
        service = _mixed_service(tmp_path, linux_cores=32, windows_cores=56)
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 40})
        win = _planned(service, "github-windows-x64", 1)[0]
        assert len(cpusets.parse(win["cpu_limit"])) == 40


def _two_host_windows_service(tmp_path, small_cores=8, big_cores=56,
                              declare_small=True, declare_big=True):
    """Two Windows workers of the same platform, physically different
    machines with their own, independent processor numbering: a small
    guest beside the physical host it does not share cores with - the
    live `rnr-windows-1` beside `beast-unit` (2026-09-23). `_mixed_service`
    above proved platform isolation with one worker per platform; this is
    the gap it could not see, because it never put two hosts of the *same*
    platform in one store together."""
    path = str(tmp_path / "control.db")
    schema.init(path)
    env = {"GH_TOKEN": "x", "GITHUB_ORG": "o"}
    FleetStore(path).seed(env)
    cells = {(p.key, pl): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for pl in P.PLATFORMS}
    service = RunnerService(path, runtimes=cells, env=env)
    big_caps = {"kind": "windows-process"}
    if declare_big:
        big_caps["host_cores"] = big_cores
    small_caps = {"kind": "windows-process"}
    if declare_small:
        small_caps["host_cores"] = small_cores
    service.inventory.register_worker("windows-big", inv.HYPERV_WINDOWS,
                                      capabilities=big_caps)
    service.inventory.register_worker("windows-small", inv.HYPERV_WINDOWS,
                                      capabilities=small_caps)
    service.inventory.heartbeat("windows-big")
    service.inventory.heartbeat("windows-small")
    return service


def _placed(service, fid, host_id, cpu_limit):
    """A runner already on a host, with the window it already has -
    standing in for one of the runners that was there before this fleet's
    width, or this host, ever changed."""
    spec = _planned(service, fid, 1)[0]
    service.specs.update(spec["runner_id"], spec["spec_version"],
                         host_id=host_id, cpu_limit=cpu_limit,
                         actual_state="idle")
    return service.specs.get(spec["runner_id"])


class TestWindowsHostIsolation:
    """Two Windows workers of the same platform - a small guest and the
    physical host behind it - number their own processors independently.
    Before `_host_cores` took a `host_id` (2026-09-23), the two were pooled
    with `min()` across the whole platform: the big host's windows could be
    cut to the small guest's size, or - since neither worker declared
    `host_cores` at all until this task - the small guest could be handed a
    window off the big host's numbering. That second failure is exactly how
    a runner placed on an eight-processor guest was pinned to cores 32-47
    and died on start (task 25's live run, 2026-09-23)."""

    def test_the_big_hosts_window_keeps_its_own_numbering(self, tmp_path):
        service = _two_host_windows_service(tmp_path, small_cores=8, big_cores=56)
        # Planned and placed before the fleet is pinned at all - like every
        # runner already on a host before its fleet's width, in this
        # deployment, ever became a window (`_service`/`_win_service`'s own
        # replacement tests do the same). Only the recreate below asks
        # `_cpu_window` to compute anything.
        _placed(service, "github-windows-x64", "windows-small", "0-7")
        big = _placed(service, "github-windows-x64", "windows-big", "0-31")
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        replacement = service.replacement_spec(service.specs.get(big["runner_id"]))
        # Pooled with the small host's 8 cores, this would have been cut
        # to an 8-wide window instead of the 16 the fleet actually asks
        # for.
        assert len(cpusets.parse(replacement["cpu_limit"])) == 16
        assert max(cpusets.parse(replacement["cpu_limit"])) > 7

    def test_the_small_hosts_window_falls_inside_its_own_cores(self, tmp_path):
        service = _two_host_windows_service(tmp_path, small_cores=8, big_cores=56)
        _placed(service, "github-windows-x64", "windows-big", "0-15")
        small = _placed(service, "github-windows-x64", "windows-small", "0-3")
        service.fleets.set_defaults("github-windows-x64", {"cpu_limit": 8})
        replacement = service.replacement_spec(service.specs.get(small["runner_id"]))
        assert cpusets.parse(replacement["cpu_limit"]) <= set(range(8))

    def test_host_core_counts_do_not_pool_within_one_platform(self, tmp_path):
        service = _two_host_windows_service(tmp_path, small_cores=8, big_cores=56)
        assert service._host_cores(P.WINDOWS, "windows-small") == 8
        assert service._host_cores(P.WINDOWS, "windows-big") == 56

    def test_a_window_wider_than_its_host_is_refused_naming_both_numbers(
            self, tmp_path):
        service = _two_host_windows_service(tmp_path, small_cores=8, big_cores=56)
        with pytest.raises(Refused) as excinfo:
            service._cpu_window(P.WINDOWS, 16, host_id="windows-small")
        message = str(excinfo.value)
        assert "16" in message and "8" in message
