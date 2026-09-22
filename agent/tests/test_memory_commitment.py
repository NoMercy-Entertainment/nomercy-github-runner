import pytest

from agent.__main__ import Declared
from agent.runtimes.linux_container import LinuxContainerRuntime
from agent.verbs import _spec, Refused
from .fake_docker import FakeDocker

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
GIB = 2 ** 30


@pytest.mark.parametrize("swap", ["-1", "0", "1g"])
def test_swap_is_bounded_and_not_less_than_physical_limit(swap):
    with pytest.raises((Refused, ValueError)):
        _spec({"image": "unit:1", "memory": "2g", "memory_swap": swap})


def test_explicit_memory_swap_is_the_total_and_telemetry_reads_it_back():
    docker = FakeDocker()
    runtime = LinuxContainerRuntime(run=docker)
    spec = _spec({"image": "unit:1", "memory": "32g", "memory_swap": "64g"})
    runtime.create(RID, spec)
    run = next(call for call in docker.calls if call[0] == "run")
    assert run[run.index("--memory-swap") + 1] == "64g"
    assert runtime.telemetry_all([RID])[RID]["mem_swap_limit_bytes"] == 64 * GIB


def test_invalid_direct_runtime_request_creates_no_storage():
    docker = FakeDocker()
    runtime = LinuxContainerRuntime(run=docker)
    with pytest.raises(ValueError):
        runtime.create(RID, {"image": "unit:1", "memory": "32g", "memory_swap": "1g"})
    assert not docker.volumes and not docker.containers


def test_worker_memory_is_measured_from_meminfo(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 1000 kB\nMemFree: 300 kB\nSwapTotal: 2000 kB\n")
    assert LinuxContainerRuntime().memory_capacity(str(path)) == {
        "memory_bytes": 1000 * 1024, "swap_bytes": 2000 * 1024}


def test_commitment_requires_actual_swap_and_preserves_reserve():
    class Runtime:
        def capabilities(self):
            return {"kind": "linux-container"}
        def memory_capacity(self):
            return {"memory_bytes": 94 * GIB, "swap_bytes": 640 * GIB}
    declared = Declared(Runtime(), {"memory_bytes": 88 * GIB,
        "swap_bytes": 640 * GIB, "memory_commit_bytes": 728 * GIB,
        "memory_admission": "bounded-overcommit"})
    assert declared.capabilities()["capacity_valid"] is True
    assert declared.capabilities()["memory_bytes"] == 88 * GIB
    declared._runtime.memory_capacity = lambda: {"memory_bytes": 94 * GIB, "swap_bytes": 0}
    caps = declared.capabilities()
    assert caps["capacity_valid"] is False and caps["swap_bytes"] == 0


def test_without_explicit_commitment_only_physical_budget_is_declared():
    runtime = LinuxContainerRuntime()
    runtime.memory_capacity = lambda: {"memory_bytes": 94 * GIB, "swap_bytes": 640 * GIB}
    caps = Declared(runtime, {"memory_bytes": 88 * GIB}).capabilities()
    assert "memory_commit_bytes" not in caps
    assert caps["memory_bytes"] == 88 * GIB
