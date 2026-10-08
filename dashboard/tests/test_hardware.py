"""What a worker's hardware is, and whether a CPU or memory limit fits on it.

Limits used to be accepted whatever their size. A fleet set to more cores than
any of its hosts has was found out by a runner that failed after it had been
placed, and a memory limit above a host's RAM was never found out at all. The
maximum is the hardware the worker reports about itself, so these tests hold
how that report is read - which key wins, what counts as measured and what is
only a budget someone declared - and the one rule that turns it into an
answer: accepted when some host can hold it, refused with a sentence naming
the hosts when none can, and accepted-but-unverified when nothing is known.
"""
import pytest

from control import hardware
from control import inventory as inv
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

GIB = 1024**3


def worker(host_id, **caps):
    return {"host_id": host_id, "capabilities": caps}


class TestOfWorker:
    def test_the_hardware_report_wins(self):
        got = hardware.of_worker(worker(
            "w", hardware={"logical_cpus": 56, "memory_bytes": 80 * GIB},
            host_cores=8, memory_total_bytes=4 * GIB, memory_bytes=2 * GIB))
        assert got["cpus"] == 56 and got["memory_bytes"] == 80 * GIB
        assert got["cpus_source"] == "measured"
        assert got["memory_source"] == "measured"

    def test_host_cores_and_measured_memory_without_a_hardware_report(self):
        got = hardware.of_worker(worker("rnr-linux-1", host_cores=56,
                                        memory_total_bytes=84418977792,
                                        memory_bytes=24 * GIB))
        assert got["cpus"] == 56 and got["memory_bytes"] == 84418977792
        assert got["memory_source"] == "measured"

    def test_a_declared_budget_is_used_and_says_so(self):
        got = hardware.of_worker(worker("w", host_cores=16, memory_bytes=12 * GIB))
        assert got["memory_bytes"] == 12 * GIB
        assert got["memory_source"] == "declared budget"

    def test_nested_runtime_capabilities_are_read(self):
        got = hardware.of_worker(worker("w", runtime={"host_cores": 8}))
        assert got["cpus"] == 8

    def test_nothing_reported_is_unknown_not_zero(self):
        got = hardware.of_worker(worker("macos-appliance-1"))
        assert got["cpus"] is None and got["memory_bytes"] is None
        assert got["cpus_source"] is None and got["memory_source"] is None

    @pytest.mark.parametrize("bad", [0, -4, True, "56", 5.5, None])
    def test_nonsense_is_unknown(self, bad):
        got = hardware.of_worker(worker("w", host_cores=bad,
                                        memory_total_bytes=bad))
        assert got["cpus"] is None and got["memory_bytes"] is None


class TestCheck:
    LINUX = [dict(hardware.of_worker(worker("rnr-linux-1", host_cores=56,
                                            memory_total_bytes=84418977792)),
                  healthy=True)]

    def test_a_limit_that_fits_is_accepted(self):
        assert hardware.check(16, 32 * GIB, self.LINUX) is None

    def test_nothing_asked_is_accepted(self):
        assert hardware.check(None, None, self.LINUX) is None

    def test_too_many_cores_is_refused_naming_host_and_max(self):
        why = hardware.check(64, None, self.LINUX)
        assert "rnr-linux-1" in why and "56" in why and "64" in why

    def test_too_much_memory_is_refused_naming_host_and_max(self):
        why = hardware.check(None, 96 * GIB, self.LINUX)
        assert "rnr-linux-1" in why and "78.6 GiB" in why and "96 GiB" in why

    def test_one_host_that_holds_it_is_enough(self):
        hosts = [dict(hardware.of_worker(worker("small", host_cores=8,
                                                memory_total_bytes=8 * GIB))),
                 dict(hardware.of_worker(worker("big", host_cores=56,
                                                memory_total_bytes=80 * GIB)))]
        assert hardware.check(16, 32 * GIB, hosts) is None

    def test_cores_and_memory_must_fit_the_same_host(self):
        """A host with the cores and another with the memory do not make
        one host that holds the runner."""
        hosts = [hardware.of_worker(worker("cores", host_cores=56,
                                           memory_total_bytes=8 * GIB)),
                 hardware.of_worker(worker("memory", host_cores=8,
                                           memory_total_bytes=80 * GIB))]
        why = hardware.check(16, 32 * GIB, hosts)
        assert why and "cores" in why and "memory" in why

    def test_unknown_hardware_is_accepted_but_unverified(self):
        hosts = [hardware.of_worker(worker("rnr-windows-1", host_cores=16))]
        assert hardware.check(8, 8 * GIB, hosts) == hardware.UNVERIFIED

    def test_no_host_at_all_is_unverified(self):
        assert hardware.check(8, 8 * GIB, []) == hardware.UNVERIFIED

    def test_a_known_dimension_too_small_is_refused_even_when_the_other_is_unknown(self):
        hosts = [hardware.of_worker(worker("rnr-windows-1", host_cores=16))]
        why = hardware.check(32, 8 * GIB, hosts)
        assert why and "rnr-windows-1" in why and "16" in why

    def test_limits_max_is_the_largest_host(self):
        hosts = [hardware.of_worker(worker("small", host_cores=8,
                                           memory_total_bytes=8 * GIB)),
                 hardware.of_worker(worker("big", host_cores=56,
                                           memory_total_bytes=80 * GIB))]
        top = hardware.limits_max(hosts)
        assert top["cpus"] == 56 and top["memory_bytes"] == 80 * GIB
        assert top["cpus_host"] == "big" and top["memory_host"] == "big"
        assert hardware.limits_max([]) == {"cpus": None, "memory_bytes": None,
                                           "cpus_host": None, "memory_host": None,
                                           "memory_source": None}


def _store(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed({})
    return path


class TestForFleet:
    def test_only_hosts_of_the_cell_count_with_their_health(self, tmp_path):
        path = _store(tmp_path)
        workers = inv.Inventory(path)
        workers.register_worker("rnr-linux-1", inv.HYPERV_LINUX, capabilities={
            "kind": "linux-container", "host_cores": 56,
            "memory_total_bytes": 84418977792})
        workers.heartbeat("rnr-linux-1")
        workers.register_worker("macos-appliance-1", inv.HYPERV_LINUX,
                                capabilities={"kind": "macos-appliance"})
        workers.register_worker("rnr-windows-1", inv.HYPERV_WINDOWS, capabilities={
            "kind": "windows-process", "host_cores": 16})
        workers.register_worker("windows-arm64-1", inv.HYPERV_WINDOWS, capabilities={
            "kind": "windows-process", "host_cores": 8, "architecture": "arm64"})
        fleets = FleetStore(path)

        linux = hardware.for_fleet(workers, fleets.get("github-linux-x64"))
        assert [(h["host_id"], h["cpus"], h["healthy"]) for h in linux] == [
            ("rnr-linux-1", 56, True)]
        x64 = hardware.for_fleet(workers, fleets.get("github-windows-x64"))
        assert [(h["host_id"], h["healthy"]) for h in x64] == [("rnr-windows-1", False)]
        arm = hardware.for_fleet(workers, fleets.get("github-windows-arm64"))
        assert [h["host_id"] for h in arm] == ["windows-arm64-1"]
        mac = hardware.for_fleet(workers, fleets.get("github-macos-x64"))
        assert [h["host_id"] for h in mac] == ["macos-appliance-1"]


class TestOfHost:
    def test_a_declaration_wins_and_telemetry_is_the_fallback(self, tmp_path):
        path = _store(tmp_path)
        service = RunnerService(path)
        service.inventory.register_worker("w1", inv.HYPERV_WINDOWS,
                                          capabilities={"kind": "windows-process"})
        service.inventory.heartbeat("w1")
        assert hardware.of_host(service.inventory, service.specs, "windows", "w1")["cpus"] is None
        rid = service.specs.create(provider="github", platform="windows",
                                   host_id="w1", fleet_id="github-windows-x64")
        with schema.connect(path) as c:
            c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                      ('{"host_cores": 16}', rid))
        got = hardware.of_host(service.inventory, service.specs, "windows", "w1")
        assert got["cpus"] == 16 and got["cpus_source"] == "runner telemetry"
        service.inventory.register_worker("w1", inv.HYPERV_WINDOWS, capabilities={
            "kind": "windows-process", "host_cores": 8})
        assert hardware.of_host(service.inventory, service.specs, "windows", "w1")["cpus"] == 8

    def test_another_hosts_telemetry_never_answers(self, tmp_path):
        path = _store(tmp_path)
        service = RunnerService(path)
        service.inventory.register_worker("w1", inv.HYPERV_WINDOWS)
        service.inventory.heartbeat("w1")
        rid = service.specs.create(provider="github", platform="windows",
                                   host_id="w1", fleet_id="github-windows-x64")
        with schema.connect(path) as c:
            c.execute("UPDATE runner_specs SET telemetry=? WHERE runner_id=?",
                      ('{"host_cores": 56}', rid))
        assert hardware.of_host(service.inventory, service.specs, "windows", "w2")["cpus"] is None
