"""Adopting a container that is already a runner (T-0802 for Linux).

The fleet on the WSL engine was made by hand and by compose, long before the
controller existed: `github-runner-1` and its nine siblings, each with its
own volume and its own registration, most of them with a job in flight at
any moment. Bringing them under the controller must cost nothing - no
rebuild, no re-registration, no restart - so the same adoption the macOS
appliance got applies here.

The difference is where the memory of it lives. A macOS instance keeps its
record in the guest; a container cannot be told anything after it is made,
so the worker keeps a small map of its own: which container on this engine
a runner_id already is. Every verb reads it, and removing a runner forgets
it.
"""
import json

import pytest

from agent import naming
from agent.runtimes.adopted import Adopted
from agent.runtimes.linux_container import LinuxContainerRuntime

from .fake_docker import FakeDocker

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER = "550e8400-e29b-41d4-a716-446655440000"
LEGACY = "github-runner-1"


@pytest.fixture
def engine():
    return FakeDocker()


@pytest.fixture
def memory(tmp_path):
    return Adopted(str(tmp_path / "adopted.json"))


@pytest.fixture
def runtime(engine, memory):
    return LinuxContainerRuntime(run=engine, adopted=memory)


@pytest.fixture
def legacy(engine):
    """A container that was there before the controller was."""
    engine.containers[LEGACY] = {"state": "running", "restarts": 0,
                                 "tmp": {}, "draining": False,
                                 "image": "runner:1",
                                 "restart": "unless-stopped"}
    return engine


class TestTakingOverAContainer:
    def test_it_makes_nothing_and_touches_nothing(self, runtime, legacy):
        before = json.dumps(legacy.containers[LEGACY], sort_keys=True)
        runtime.create(RID, {"adopt": {"label": LEGACY}})
        assert json.dumps(legacy.containers[LEGACY], sort_keys=True) == before
        assert list(legacy.containers) == [LEGACY], "no second container"
        assert legacy.volumes == {}, "no volumes were made for it"

    def test_the_handle_is_the_container_it_already_is(self, runtime, legacy):
        assert runtime.create(RID, {"adopt": {"label": LEGACY}}) == LEGACY

    def test_afterwards_every_verb_means_that_container(self, runtime,
                                                        legacy):
        runtime.create(RID, {"adopt": {"label": LEGACY}})
        assert runtime.status(RID)["exists"] is True
        assert runtime.status(RID)["running"] is True
        runtime.stop(RID)
        assert legacy.containers[LEGACY]["state"] != "running"
        runtime.start(RID)
        assert legacy.containers[LEGACY]["state"] == "running"

    def test_a_runner_that_was_not_adopted_is_still_its_own_name(
            self, runtime, engine):
        runtime.create(OTHER, {"image": "runner:1"})
        assert naming.unit_name(OTHER) in engine.containers

    def test_adopting_twice_is_the_same_as_adopting_once(self, runtime,
                                                         legacy):
        first = runtime.create(RID, {"adopt": {"label": LEGACY}})
        assert runtime.create(RID, {"adopt": {"label": LEGACY}}) == first
        assert list(legacy.containers) == [LEGACY]

    def test_adopting_what_is_not_there_is_refused(self, runtime, engine):
        with pytest.raises(RuntimeError, match="no container"):
            runtime.create(RID, {"adopt": {"label": "ghost-runner"}})

    def test_the_memory_of_it_survives_a_restart_of_the_agent(
            self, runtime, legacy, memory):
        runtime.create(RID, {"adopt": {"label": LEGACY}})
        fresh = LinuxContainerRuntime(run=legacy,
                                      adopted=Adopted(memory.path))
        assert fresh.status(RID)["running"] is True

    def test_removing_it_forgets_it(self, runtime, legacy, memory):
        runtime.create(RID, {"adopt": {"label": LEGACY}})
        runtime.remove(RID, keep_data=False)
        assert LEGACY not in legacy.containers
        assert memory.name_for(RID, "fallback") == "fallback"


class TestTheMapItself:
    def test_it_is_written_so_only_the_agent_can_read_it(self, tmp_path):
        memory = Adopted(str(tmp_path / "adopted.json"))
        memory.record(RID, LEGACY)
        assert memory.name_for(RID, "derived") == LEGACY

    def test_an_unknown_runner_gets_the_derived_name(self, memory):
        assert memory.name_for(OTHER, "rnr-x") == "rnr-x"

    def test_a_file_that_is_not_there_yet_is_not_an_error(self, tmp_path):
        assert Adopted(str(tmp_path / "none.json")).name_for(RID, "d") == "d"

    def test_a_damaged_file_does_not_take_the_agent_down(self, tmp_path):
        path = tmp_path / "adopted.json"
        path.write_text("{ this is not json", encoding="utf-8")
        memory = Adopted(str(path))
        assert memory.name_for(RID, "derived") == "derived"

    def test_forgetting_what_was_never_there_is_no_error(self, memory):
        memory.forget(RID)

    def test_two_runners_can_be_adopted_side_by_side(self, memory):
        memory.record(RID, "github-runner-1")
        memory.record(OTHER, "github-runner-2")
        assert memory.name_for(RID, "d") == "github-runner-1"
        assert memory.name_for(OTHER, "d") == "github-runner-2"
