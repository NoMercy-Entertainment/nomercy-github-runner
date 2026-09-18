"""The contract suite catches a runtime that breaks the contract.

A suite that passes against the one runtime it was written beside proves only
that the two agree. These build runtimes that are each wrong in one way the
design forbids, run the relevant scenario against them, and require it to
fail. If a scenario ever stops failing here, it has stopped checking what it
claims to check.
"""
import pytest

from agent import naming
from agent.runtimes.linux_container import LinuxContainerRuntime

from .contract import suite
from .contract.harnesses import LinuxContainerHarness


class LeavesTheCacheBehind(LinuxContainerRuntime):
    """Removes everything except the cache volume."""

    def remove(self, runner_id, keep_data):
        super().remove(runner_id, keep_data=True)


class CreatesTwice(LinuxContainerRuntime):
    """Does not adopt a unit a dead attempt left behind."""

    def create(self, runner_id, spec):
        rid = naming.check(runner_id)
        ok, _, err = self._run(["run", "-d", "--name", naming.unit_name(rid),
                                spec["image"]], timeout=180)
        if not ok:
            raise RuntimeError(err)
        return naming.unit_name(rid)


class ClearsEveryonesCache(LinuxContainerRuntime):
    """Prunes every runner's engine, not just the one asked for."""

    def clear_cache(self, runner_id, policy):
        result = super().clear_cache(runner_id, policy)
        for unit in list(self._run.containers):
            if unit != naming.unit_name(runner_id):
                self._run(["exec", unit, "docker", "buildx", "prune", "-af"])
        return result


class KeepsTheOldWorkspace(LinuxContainerRuntime):
    """Treats a recreate's keep_data as keep everything."""

    def remove(self, runner_id, keep_data):
        if keep_data:
            self._run(["rm", "-f", "-v", naming.unit_name(runner_id)],
                      timeout=180)
            return
        super().remove(runner_id, keep_data)


def harness_with(runtime_class):
    h = LinuxContainerHarness()
    h.runtime = runtime_class(run=h.docker)
    return h


@pytest.mark.parametrize("broken,scenario", [
    (LeavesTheCacheBehind,
     suite.test_7_remove_leaves_no_forge_record_and_no_storage),
    (CreatesTwice,
     suite.test_8_a_create_cut_off_by_a_crash_converges_to_a_whole_instance),
    (ClearsEveryonesCache,
     suite.test_5_clear_cache_frees_space_is_idempotent_and_touches_no_one_else),  # noqa: E501
    (KeepsTheOldWorkspace,
     suite.test_6_recreate_gives_a_new_workspace_and_a_fresh_registration),
], ids=["leaves-storage", "creates-twice", "clears-others", "keeps-workspace"])
def test_the_suite_fails_a_runtime_that_breaks_it(broken, scenario):
    with pytest.raises((AssertionError, RuntimeError)):
        scenario(harness_with(broken))


def test_and_passes_the_real_one():
    """The same scenarios, against the runtime as written."""
    for scenario in (
            suite.test_5_clear_cache_frees_space_is_idempotent_and_touches_no_one_else,  # noqa: E501
            suite.test_6_recreate_gives_a_new_workspace_and_a_fresh_registration,  # noqa: E501
            suite.test_7_remove_leaves_no_forge_record_and_no_storage,
            suite.test_8_a_create_cut_off_by_a_crash_converges_to_a_whole_instance):  # noqa: E501
        scenario(LinuxContainerHarness())


# ---------------------------------------------------------------------------
# T-0503: a declaration is checked in both directions
# ---------------------------------------------------------------------------

class ClaimsToDrain(LinuxContainerRuntime):
    def capabilities(self):
        return dict(super().capabilities(), supports_drain=True)


class SaysNothingAboutDrain(LinuxContainerRuntime):
    def capabilities(self):
        caps = dict(super().capabilities())
        del caps["supports_drain"]
        return caps


class DeniesClearingButClears(LinuxContainerRuntime):
    def capabilities(self):
        return dict(super().capabilities(), clear_cache=False)


class DrainsInSilence(LinuxContainerHarness):
    """Declared unable to drain, and quietly accepts being asked."""

    def drain(self, runner_id):
        return None


def test_claiming_a_capability_it_does_not_have_fails():
    """Declared true, and the platform cannot do it: the scenario runs, and
    the platform's own refusal is what fails it."""
    with pytest.raises(suite.NotSupported):
        suite.test_2_drain_while_busy_finishes_the_job_and_takes_no_other(
            harness_with(ClaimsToDrain))


def test_saying_nothing_about_one_fails():
    """Omission is the quietest way to skip a scenario."""
    with pytest.raises(AssertionError, match="true or false"):
        suite.test_every_gating_capability_is_declared(
            harness_with(SaysNothingAboutDrain))


def test_denying_one_it_has_fails():
    with pytest.raises(AssertionError):
        suite.test_5_clear_cache_frees_space_is_idempotent_and_touches_no_one_else(  # noqa: E501
            harness_with(DeniesClearingButClears))


def test_a_declared_absence_that_is_not_refused_fails():
    """Declared false, then does nothing when asked instead of saying no."""
    with pytest.raises(pytest.fail.Exception, match="went through"):
        suite.test_2_drain_while_busy_finishes_the_job_and_takes_no_other(
            DrainsInSilence())
