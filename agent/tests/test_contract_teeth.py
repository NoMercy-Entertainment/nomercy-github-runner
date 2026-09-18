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
from agent.runtimes.macos_appliance import MacApplianceRuntime
from agent.runtimes.windows_process import WindowsProcessRuntime

from .contract import suite
from .contract.harnesses import (MAC_TOOLS, WINDOWS_TOOLS,
                                 LinuxContainerHarness, MacApplianceHarness,
                                 WindowsProcessHarness)


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
# the same teeth for the Windows runtime
# ---------------------------------------------------------------------------

class WinLeavesTheCacheBehind(WindowsProcessRuntime):
    def remove(self, runner_id, keep_data):
        super().remove(runner_id, keep_data=True)


class WinKeepsTheOldWorkspace(WindowsProcessRuntime):
    def remove(self, runner_id, keep_data):
        if keep_data:
            self._nssm("remove", naming.unit_name(runner_id), "confirm")
            return
        super().remove(runner_id, keep_data)


class WinClearsEveryonesCache(WindowsProcessRuntime):
    def clear_cache(self, runner_id, policy):
        result = super().clear_cache(runner_id, policy)
        for name in list(self._fs.services):
            rid = name[len("rnr-"):]
            if rid != runner_id:
                self._fs.clear_dir(self.paths(rid)["cache"])
        return result


class WinForgetsWhatItStarted(WindowsProcessRuntime):
    """Does not make the service when it is asked a second time."""

    def create(self, runner_id, spec):
        if self._fs.exists(self.paths(runner_id)["root"]):
            return naming.unit_name(runner_id)
        return super().create(runner_id, spec)


def windows_harness_with(runtime_class):
    h = WindowsProcessHarness()
    h.runtime = runtime_class(run=h.host, fs=h.host, tools=WINDOWS_TOOLS)
    return h


@pytest.mark.parametrize("broken,scenario", [
    (WinLeavesTheCacheBehind,
     suite.test_7_remove_leaves_no_forge_record_and_no_storage),
    (WinKeepsTheOldWorkspace,
     suite.test_6_recreate_gives_a_new_workspace_and_a_fresh_registration),
    (WinClearsEveryonesCache,
     suite.test_5_clear_cache_frees_space_is_idempotent_and_touches_no_one_else),  # noqa: E501
    (WinForgetsWhatItStarted,
     suite.test_8_a_create_cut_off_by_a_crash_converges_to_a_whole_instance),
], ids=["leaves-storage", "keeps-workspace", "clears-others",
        "half-created"])
def test_the_suite_fails_a_windows_runtime_that_breaks_it(broken, scenario):
    with pytest.raises((AssertionError, RuntimeError)):
        scenario(windows_harness_with(broken))


# ---------------------------------------------------------------------------
# and for the macOS appliance
# ---------------------------------------------------------------------------

class MacLeavesTheCacheBehind(MacApplianceRuntime):
    def remove(self, runner_id, keep_data):
        super().remove(runner_id, keep_data=True)


class MacForgetsWhatItStarted(MacApplianceRuntime):
    def create(self, runner_id, spec):
        if self._fs.exists(self.paths(runner_id)["root"]):
            return naming.unit_name(runner_id)
        return super().create(runner_id, spec)


class MacKeepsTheOldWorkspace(MacApplianceRuntime):
    def remove(self, runner_id, keep_data):
        if keep_data:
            self.stop(runner_id)
            self._fs.remove(self.paths(runner_id)["plist"])
            return
        super().remove(runner_id, keep_data)


def mac_harness_with(runtime_class):
    h = MacApplianceHarness()
    h.runtime = runtime_class(run=h.guest, fs=h.guest,
                              appliance=h.guest.appliance, tools=MAC_TOOLS)
    return h


@pytest.mark.parametrize("broken,scenario", [
    (MacLeavesTheCacheBehind,
     suite.test_7_remove_leaves_no_forge_record_and_no_storage),
    (MacForgetsWhatItStarted,
     suite.test_8_a_create_cut_off_by_a_crash_converges_to_a_whole_instance),
    (MacKeepsTheOldWorkspace,
     suite.test_6_recreate_gives_a_new_workspace_and_a_fresh_registration),
], ids=["leaves-storage", "half-created", "keeps-workspace"])
def test_the_suite_fails_a_macos_runtime_that_breaks_it(broken, scenario):
    with pytest.raises((AssertionError, RuntimeError)):
        scenario(mac_harness_with(broken))


# ---------------------------------------------------------------------------
# T-0503: a declaration is checked in both directions
# ---------------------------------------------------------------------------

class DrainsByKilling(LinuxContainerRuntime):
    """Claims to drain, and stops the unit - killing the job it has."""

    def drain(self, runner_id):
        self.stop(runner_id)


class DrainsButComesBack(LinuxContainerRuntime):
    """Signals the runner but leaves the restart policy on, so the engine
    brings it straight back to take the next job."""

    def drain(self, runner_id):
        self._check(["kill", "--signal=TERM",
                     naming.unit_name(runner_id)], timeout=30)


class StartsButStaysDrained(LinuxContainerRuntime):
    """Starts the unit and leaves the restart policy a drain took off, so
    the first time its runner exits it stays down."""

    def start(self, runner_id):
        self._check(["start", naming.unit_name(runner_id)], timeout=60)


class WinStartsButStaysDrained(WindowsProcessRuntime):
    """Starts the service with the drain request still there, which the job
    host answers at once."""

    def start(self, runner_id):
        self._check(self._nssm("start", naming.unit_name(runner_id)))


class DeniesDraining(LinuxContainerRuntime):
    """Says it cannot drain - and a drain asked of it goes through anyway."""

    def capabilities(self):
        return dict(super().capabilities(), supports_drain=False)


class SaysNothingAboutDrain(LinuxContainerRuntime):
    def capabilities(self):
        caps = dict(super().capabilities())
        del caps["supports_drain"]
        return caps


class DeniesClearingButClears(LinuxContainerRuntime):
    def capabilities(self):
        return dict(super().capabilities(), clear_cache=False)


@pytest.mark.parametrize("broken", [DrainsByKilling, DrainsButComesBack],
                         ids=["kills-the-job", "comes-back"])
def test_claiming_a_capability_it_does_not_have_fails(broken):
    """Declared true, and the drain does not do what drain means: the
    scenario runs and fails on it."""
    with pytest.raises(AssertionError):
        suite.test_2_drain_while_busy_finishes_the_job_and_takes_no_other(
            harness_with(broken))


@pytest.mark.parametrize("make", [
    lambda: harness_with(StartsButStaysDrained),
    lambda: windows_harness_with(WinStartsButStaysDrained),
], ids=["linux", "windows"])
def test_a_start_that_leaves_the_drain_in_place_fails(make):
    """A restart of a busy runner is drain, stop, start. A start that does
    not undo the drain leaves a runner that serves until its first exit, or
    not at all."""
    with pytest.raises(AssertionError):
        suite.test_3_cancel_drain_accepts_work_again(make())


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
    """Declared false, then goes through when asked instead of saying no."""
    with pytest.raises(pytest.fail.Exception, match="went through"):
        suite.test_2_drain_while_busy_finishes_the_job_and_takes_no_other(
            harness_with(DeniesDraining))
