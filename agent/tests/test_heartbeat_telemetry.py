"""T-1803, the agent's half: a heartbeat says what each unit uses.

CPU and memory on every beat, for the units that run, read in one call where
the runtime can; storage and cache on a deep beat, for every unit. A unit
that could not be read carries nothing - unknown - rather than zeros.
"""
from agent import heartbeat as hb
from agent.runtimes.linux_container import (LinuxContainerRuntime,
                                            LinuxRegistrar)
from agent.verbs import Agent

from .fake_docker import FakeDocker

RUNNING = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
STOPPED = "550e8400-e29b-41d4-a716-446655440000"


def agent_with_two_units():
    docker = FakeDocker()
    rt = LinuxContainerRuntime(run=docker)
    for rid in (RUNNING, STOPPED):
        rt.create(rid, {"image": "ghcr.io/x/runner:1"})
    rt.stop(STOPPED)
    return Agent("linux-1", rt, LinuxRegistrar(run=docker)), docker


def units(beat):
    return {u["runner_id"]: u for u in beat["instances"]}


class TestEveryBeat:
    def test_a_running_unit_carries_its_cpu_and_memory(self):
        agent, _ = agent_with_two_units()
        t = units(hb.build(agent))[RUNNING]["telemetry"]
        assert t["cpu_percent"] == 1.5
        assert t["mem_used_bytes"] == 100 * 1024 ** 2
        assert t["mem_limit_bytes"] == 32 * 1024 ** 3

    def test_a_stopped_one_carries_none(self):
        agent, _ = agent_with_two_units()
        assert "telemetry" not in units(hb.build(agent))[STOPPED]

    def test_all_units_are_read_in_one_call(self):
        """One `docker stats` per unit would outlast the interval on a full
        worker."""
        agent, docker = agent_with_two_units()
        docker.calls.clear()
        hb.build(agent)
        assert len([c for c in docker.calls if c[0] == "stats"]) == 1

    def test_storage_and_cache_wait_for_a_deep_beat(self):
        agent, _ = agent_with_two_units()
        assert "cache_bytes" not in units(hb.build(agent))[RUNNING][
            "telemetry"]
        deep = units(hb.build(agent, deep=True))[RUNNING]["telemetry"]
        assert "cache_bytes" in deep


class TestWhatAUnitMayUse:
    """"70%" means nothing without what it is 70% of. A unit pinned to a
    sixteen-core window and using eleven of them is nearly full, and against
    the host's sixty-four it reads as idle - so the card says "11.8 / 16
    cores", as the dashboard this replaces did. The heartbeat stopped
    carrying it and the card fell back to a bare percentage (2026-09-21).
    """

    def agent(self, **spec):
        docker = FakeDocker()
        rt = LinuxContainerRuntime(run=docker)
        rt.create(RUNNING, dict({"image": "ghcr.io/x/runner:1"}, **spec))
        return Agent("linux-1", rt, LinuxRegistrar(run=docker)), docker

    def test_a_pinned_unit_says_how_wide_its_window_is(self):
        agent, _ = self.agent(cpuset="0-15")
        t = units(hb.build(agent))[RUNNING]["telemetry"]
        assert t["cpu_cores"] == 16

    def test_a_quota_counts_when_it_is_the_lower_of_the_two(self):
        agent, _ = self.agent(cpuset="0-15", cpus="4")
        assert units(hb.build(agent))[RUNNING]["telemetry"]["cpu_cores"] == 4

    def test_a_unit_with_no_limit_says_none_and_the_host_is_the_scale(self):
        agent, _ = self.agent()
        t = units(hb.build(agent))[RUNNING]["telemetry"]
        assert t.get("cpu_cores") is None
        assert t["host_cores"] >= 1


class TestWhichJob:
    """A busy card that cannot say what it is busy with sends its reader to
    the forge to find out. The runner prints it; the old dashboard read it
    from there (2026-09-21)."""

    def test_a_running_unit_names_the_job_its_log_names(self):
        agent, docker = agent_with_two_units()
        from agent import naming
        docker.logs[naming.unit_name(RUNNING)] = (
            "2026-09-20 15:17:50Z: Running job: test / coverage")
        t = units(hb.build(agent))[RUNNING]["telemetry"]
        assert t["job"] == "test / coverage"

    def test_one_that_names_none_carries_none(self):
        agent, _ = agent_with_two_units()
        assert "job" not in units(hb.build(agent))[RUNNING]["telemetry"]

    def test_a_log_that_cannot_be_read_costs_the_name_not_the_beat(self):
        agent, docker = agent_with_two_units()
        docker.fail_once["logs"] = "the engine did not answer"
        t = units(hb.build(agent))[RUNNING]["telemetry"]
        assert "job" not in t and t["cpu_percent"] == 1.5


class TestWhatCannotBeRead:
    def test_a_runtime_that_fails_to_measure_reports_nothing(self):
        agent, _ = agent_with_two_units()

        def broken(ids):
            raise RuntimeError("the engine did not answer")
        agent.runtime.telemetry_all = broken
        beat = hb.build(agent)
        assert "telemetry" not in units(beat)[RUNNING]
        assert units(beat)[RUNNING]["state"] == "running"

    def test_every_thirtieth_beat_is_deep(self):
        assert hb.DEEP_EVERY == 30
