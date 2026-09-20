"""The macOS appliance runtime, against a guest that behaves like macOS.

What is particular to it, beyond the contract the suite already holds it to:
the vocabulary (CON-7 - a guest is never called a container), the two
recorded appliance failures it exists to absorb (the boot loop after a hard
stop, and a full root disk that reads as "offline"), a launchd job per
instance whose environment file only its user can read, and the fifth point
of design 10.6 - an instance says what template it was made from.
"""
import json
import os
import plistlib
import re

import pytest

from agent import naming
from agent.runtimes import macos_appliance
from agent.runtimes.macos_appliance import (KEPT_ON_RECREATE,
                                            MacApplianceRuntime,
                                            MacRegistrar)

from .fake_docker import Crash
from .fake_macos import TEMPLATE, TOOLS, FakeMac

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER = "550e8400-e29b-41d4-a716-446655440000"
SECRET = "ghp_this-is-a-token-value-0123456789"
SPEC = {"image": TEMPLATE, "env": {"SOME_TOKEN": SECRET}}
PLAN = {"url": "https://git.nomercy.tv", "token": "AAAAREGISTRATIONTOKEN0123",
        "name": "mac-1", "labels": "macos:host"}


@pytest.fixture
def guest():
    return FakeMac()


@pytest.fixture
def runtime(guest):
    return MacApplianceRuntime(run=guest, fs=guest,
                               appliance=guest.appliance, tools=TOOLS)


@pytest.fixture
def registrar(guest):
    return MacRegistrar(run=guest, fs=guest)


def launchctl(guest, verb=None):
    return [c for c in guest.calls
            if c[0] == TOOLS["launchctl"] and (verb is None or c[1] == verb)]


class TestTheVocabulary:
    """CON-7: a VM or QEMU guest is never called a container. The one
    exception is the capability key `job_containers`, which is the protocol's
    name for whether *jobs* may run images - here, that they may not."""

    def test_the_module_never_calls_anything_a_container(self):
        source = open(macos_appliance.__file__, encoding="utf-8").read()
        found = [m.group(0) for m in re.finditer(r"(?i)\w*container\w*",
                                                  source)]
        assert found == ["job_containers"]

    def test_nor_says_docker(self):
        source = open(macos_appliance.__file__, encoding="utf-8").read()
        assert "docker" not in source.lower()

    def test_its_areas_are_namings_minus_the_engine(self):
        assert macos_appliance.AREAS == tuple(
            a for a, path in naming.names(RID, "macos").items() if path)


class TestTheAppliance:
    def test_a_hard_stop_is_recovered_by_clearing_the_leftover(self, runtime,
                                                               guest):
        """The recorded restart loop: after a hard stop the next boot loops
        until /var/tmp/opencore-image-ng.sh-* is removed. Start does it."""
        runtime.create(RID, SPEC)
        guest.appliance.hard_stop()
        runtime.start(RID)
        assert guest.appliance.power == "running"
        assert guest.appliance.leftovers == []

    def test_create_boots_a_guest_that_is_down(self, runtime, guest):
        guest.appliance.hard_stop()
        runtime.create(RID, SPEC)
        assert guest.appliance.boots == 1
        assert runtime.status(RID)["running"] is True

    def test_an_unknown_power_state_is_not_booted_blind(self, runtime,
                                                        guest):
        guest.appliance.power = "unknown"
        with pytest.raises(RuntimeError, match="unknown"):
            runtime.start(RID)
        assert guest.appliance.boots == 0

    def test_inside_the_guest_there_is_nothing_to_boot(self, guest):
        """With no hypervisor side attached, the agent answering is the proof
        the guest is up."""
        rt = MacApplianceRuntime(run=guest, fs=guest, tools=TOOLS)
        rt.create(RID, SPEC)
        assert rt.status(RID)["running"] is True

    def test_telemetry_reports_the_guests_root_disk(self, runtime, guest):
        """The other recorded failure: a full root disk reads as the runner
        being offline while the hypervisor side looks fine."""
        runtime.create(RID, SPEC)
        guest.root_disk = (100 * 1024 ** 3, 99 * 1024 ** 3)
        t = runtime.telemetry(RID)
        assert t["root_disk_total_bytes"] == 100 * 1024 ** 3
        assert t["root_disk_used_bytes"] == 99 * 1024 ** 3

    def test_telemetry_reports_the_instances_processes(self, runtime):
        runtime.create(RID, SPEC)
        t = runtime.telemetry(RID)
        assert t["cpu_percent"] == 2.5
        assert t["mem_used_bytes"] == 102400 * 1024

    def test_a_stopped_instance_has_no_process_figures(self, runtime):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        t = runtime.telemetry(RID)
        assert t["cpu_percent"] is None and t["mem_used_bytes"] is None


class TestTheLaunchdJob:
    def test_one_job_per_instance_named_from_its_runner_id(self, runtime,
                                                           guest):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        assert sorted(guest.jobs) == sorted([
            f"com.nomercy.rnr-{RID}", f"com.nomercy.rnr-{OTHER}"])

    def test_the_job_runs_the_templates_entry_point_in_its_own_tree(
            self, runtime, guest):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        job = plistlib.loads(guest.read_text(p["plist"]).encode())
        assert job["ProgramArguments"] == [p["reg"] + "/run"]
        assert job["WorkingDirectory"] == p["work"]
        assert job["EnvironmentVariables"]["TMPDIR"] == p["tmp"] + "/"
        assert job["EnvironmentVariables"]["RUNNER_CACHE_DIR"] == p["cache"]
        assert job["ExitTimeOut"] == 60

    def test_the_job_file_is_readable_by_its_user_alone(self, runtime, guest):
        """It carries the environment, and an environment can carry a
        token."""
        runtime.create(RID, SPEC)
        assert guest.modes[runtime.paths(RID)["plist"]] == 0o600

    def test_the_tree_is_private_to_its_user(self, runtime, guest):
        runtime.create(RID, SPEC)
        assert guest.modes[runtime.paths(RID)["root"]] == 0o700

    def test_no_environment_value_is_ever_on_a_command_line(self, runtime,
                                                           guest):
        runtime.create(RID, SPEC)
        runtime.restart(RID)
        assert not any(SECRET in " ".join(c) for c in guest.calls)

    def test_stop_unloads_so_launchd_does_not_restart_it(self, runtime,
                                                         guest):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        assert f"com.nomercy.rnr-{RID}" not in guest.jobs
        s = runtime.status(RID)
        assert (s["exists"], s["running"]) == (True, False)

    def test_start_after_stop_loads_it_again(self, runtime):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        runtime.start(RID)
        assert runtime.status(RID)["running"] is True

    def test_stopping_twice_is_fine(self, runtime):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        runtime.stop(RID)

    def test_restart_kills_and_starts_in_one_step(self, runtime, guest):
        runtime.create(RID, SPEC)
        before = guest.jobs[f"com.nomercy.rnr-{RID}"]["pid"]
        runtime.restart(RID)
        assert guest.jobs[f"com.nomercy.rnr-{RID}"]["pid"] != before
        assert ["-k"] == [a for a in launchctl(guest, "kickstart")[-1]
                          if a == "-k"]


class TestCreate:
    def test_an_unknown_template_is_refused_before_anything_is_made(
            self, runtime, guest):
        with pytest.raises(RuntimeError, match="no runner template"):
            runtime.create(RID, {"image": "nope"})
        assert guest.jobs == {}
        assert not guest.exists(runtime.paths(RID)["root"])

    def test_creating_twice_adopts(self, runtime, guest):
        runtime.create(RID, SPEC)
        runtime.create(RID, SPEC)
        assert len(launchctl(guest, "bootstrap")) == 1
        assert len(guest.jobs) == 1

    @pytest.mark.parametrize("step", ["bootstrap", "print"])
    def test_a_create_cut_off_anywhere_converges(self, runtime, guest, step):
        guest.crash_after = step
        with pytest.raises(Crash):
            runtime.create(RID, SPEC)
        runtime.create(RID, SPEC)
        assert runtime.status(RID)["running"] is True
        assert len(guest.jobs) == 1

    def test_an_instance_says_what_it_was_made_from(self, runtime):
        """Design 10.6, point 5."""
        runtime.create(RID, SPEC)
        assert runtime.status(RID)["runtime_template"] == TEMPLATE


class TestRemove:
    def test_keep_data_false_removes_the_whole_tree_and_the_job(self,
                                                                runtime,
                                                                guest):
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=False)
        assert guest.jobs == {}
        assert not guest.exists(runtime.paths(RID)["root"])
        assert not guest.exists(runtime.paths(RID)["plist"])

    def test_keep_data_true_keeps_the_cache_and_the_logs(self, runtime,
                                                         guest):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        for area in ("work", "cache", "logs"):
            guest.put(p[area] + "/x", 10)
        runtime.remove(RID, keep_data=True)
        assert KEPT_ON_RECREATE == ("cache", "logs")
        assert guest.exists(p["cache"] + "/x")
        assert guest.exists(p["logs"] + "/x")
        assert not guest.exists(p["work"])
        assert not guest.exists(p["reg"])

    def test_removing_what_is_gone_succeeds(self, runtime):
        runtime.remove(RID, keep_data=False)

    def test_storage_that_cannot_be_deleted_is_a_failure(self, runtime,
                                                         guest):
        runtime.create(RID, SPEC)
        held = runtime.paths(RID)["work"] + "/held"
        guest.put(held, 1)
        guest.locked.add(held)
        with pytest.raises(RuntimeError, match="storage left behind"):
            runtime.remove(RID, keep_data=False)


class TestObservation:
    def test_status_is_unknown_when_launchd_cannot_be_asked(self, guest):
        rt = MacApplianceRuntime(run=guest, fs=guest, tools=TOOLS)
        rt.create(RID, SPEC)

        def broken(args, input=None, timeout=None):
            return False, "", "launchctl: operation not permitted"
        rt._run = broken
        assert rt.status(RID)["exists"] is None

    def test_instances_lists_this_appliances_runners(self, runtime, guest):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        runtime.stop(OTHER)
        guest.put(TOOLS["launch_agents"] + "/com.example.other.plist", 1)
        assert sorted((i["runner_id"], i["state"])
                      for i in runtime.instances()) == sorted(
            [(RID, "running"), (OTHER, "stopped")])

    def test_probes(self, runtime, guest):
        runtime.create(RID, SPEC)
        guest.put(runtime.paths(RID)["cache"] + "/c", 700)
        assert runtime.probe(RID, "cache_size") == {"ok": True, "value": 700}
        assert runtime.probe(RID, "job_state")["ok"] is False

    def test_logs(self, runtime, guest):
        runtime.create(RID, SPEC)
        guest.write_text(runtime.paths(RID)["logs"] + "/runner.log",
                         "boot\nready")
        assert runtime.logs(RID, 60, max_bytes=5) == "ready"


class TestClearCache:
    def test_derived_data_is_not_offered(self, runtime):
        """It lives in the user's Library, shared by every instance in the
        guest, so no instance can prove it owns it."""
        assert "derived-data" not in runtime.capabilities()["cache_scopes"]

    def test_the_default_clears_the_caches_and_leaves_the_workspace(
            self, runtime, guest):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        guest.put(p["cache"] + "/a", 5000)
        guest.put(p["work"] + "/repo", 900)
        result = runtime.clear_cache(RID, {})
        assert result["per_scope"]["toolcache"] == 5000
        assert guest.exists(p["work"] + "/repo")

    def test_engine_scopes_are_errors_not_silence(self, runtime):
        runtime.create(RID, SPEC)
        result = runtime.clear_cache(RID, {"scopes": ["engine-images-unused"]})
        assert "engine-images-unused" in result["errors"]


class TestRegistrar:
    def test_the_plan_goes_on_stdin_and_never_in_argv(self, runtime,
                                                      registrar, guest):
        runtime.create(RID, SPEC)
        registrar.register(RID, PLAN)
        assert json.loads(guest.inputs[-1])["token"] == PLAN["token"]
        assert not any(PLAN["token"] in " ".join(c) for c in guest.calls)

    def test_it_runs_the_templates_own_entry_point(self, runtime, registrar,
                                                   guest):
        runtime.create(RID, SPEC)
        registrar.register(RID, PLAN)
        assert guest.calls[-1] == [runtime.paths(RID)["reg"] + "/register"]

    def test_deregistering_a_gone_instance_points_at_the_forge(self,
                                                               registrar):
        with pytest.raises(RuntimeError, match="removed at the forge"):
            registrar.deregister(RID)


class TestCapabilities:
    def test_they_say_what_the_appliance_cannot_do(self, runtime):
        caps = runtime.capabilities()
        assert caps["kind"] == "macos-appliance"
        assert caps["job_containers"] is False
        assert caps["nested_builds"] is False
        assert caps["supports_drain"] is True
        assert "no per-instance memory or CPU cap" in caps["notes"]


class TestAdoptingARunnerThatIsAlreadyThere:
    """MIG-4: the Forgejo runner that has been serving from this appliance
    for months becomes an ordinary managed instance without being rebuilt,
    re-registered or interrupted.

    It was installed by hand: its own launchd label, its own directory,
    neither of them the ones a runner_id derives. So `create` takes an
    `adopt` block naming what is already there, records it, and from then on
    every verb acts on that job. Nothing is copied, nothing is loaded, and
    the job is not touched - a runner with a job running must not notice
    that it has been adopted."""

    LEGACY = "org.forgejo.runner"
    ADOPT = {"adopt": {"label": LEGACY,
                       "root": "/usr/local/forgejo-runner",
                       "template": "forgejo-runner-darwin-amd64-v12.0.1"}}

    @pytest.fixture
    def legacy(self, guest):
        guest.jobs[self.LEGACY] = {"state": "running", "pid": 270,
                                   "job": {"Label": self.LEGACY}}
        return guest

    def test_it_does_not_touch_the_running_job(self, runtime, legacy):
        before = dict(legacy.jobs[self.LEGACY])
        runtime.create(RID, self.ADOPT)
        assert legacy.jobs[self.LEGACY] == before
        assert not [c for c in legacy.calls
                    if c[0] == TOOLS["launchctl"]
                    and c[1] in ("bootstrap", "kickstart", "bootout", "kill")]

    def test_it_writes_no_plist_and_copies_no_template(self, runtime, legacy):
        runtime.create(RID, self.ADOPT)
        assert not legacy.exists(runtime.paths(RID)["plist"])

    def test_afterwards_it_is_an_ordinary_running_instance(self, runtime,
                                                           legacy):
        runtime.create(RID, self.ADOPT)
        status = runtime.status(RID)
        assert status["exists"] is True
        assert status["running"] is True
        assert status["pid"] == 270

    def test_it_reports_what_it_was_adopted_from(self, runtime, legacy):
        runtime.create(RID, self.ADOPT)
        assert runtime.status(RID)["runtime_template"] == \
            self.ADOPT["adopt"]["template"]

    def test_adopting_twice_is_the_same_as_adopting_once(self, runtime,
                                                         legacy):
        first = runtime.create(RID, self.ADOPT)
        assert runtime.create(RID, self.ADOPT) == first
        assert runtime.status(RID)["running"] is True

    def test_adopting_what_is_not_there_is_refused(self, runtime, guest):
        with pytest.raises(RuntimeError, match="no launchd job"):
            runtime.create(RID, {"adopt": {"label": "org.absent.runner"}})

    def test_an_adopted_instance_drains_like_any_other(self, runtime, legacy):
        runtime.create(RID, self.ADOPT)
        runtime.drain(RID)
        assert legacy.jobs[self.LEGACY]["state"] != "running"

    def test_an_adopted_instance_stops_like_any_other(self, runtime, legacy):
        runtime.create(RID, self.ADOPT)
        runtime.stop(RID)
        assert self.LEGACY not in legacy.jobs

    def test_removing_it_leaves_the_software_it_was_adopted_from(
            self, runtime, legacy):
        legacy.makedirs("/usr/local/forgejo-runner")
        legacy.write_text("/usr/local/forgejo-runner/forgejo-runner", "bin")
        runtime.create(RID, self.ADOPT)
        runtime.remove(RID, keep_data=False)
        assert legacy.exists("/usr/local/forgejo-runner/forgejo-runner"), \
            "what this runtime did not install, it does not delete"
        assert not legacy.exists(runtime.paths(RID)["root"])

    def test_telemetry_reads_the_adopted_process(self, runtime, legacy):
        runtime.create(RID, self.ADOPT)
        assert runtime.telemetry(RID)["root_disk_total_bytes"]
