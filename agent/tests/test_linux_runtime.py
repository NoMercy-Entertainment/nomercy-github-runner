"""The Linux container runtime, against a docker that behaves like docker.

Three groups. The argv, because T-0002's lessons live in it - the stop timeout,
swap equal to memory, the removal timeout - and because it must never carry a
secret where `ps` can read it. The storage, because every name comes from the
runner_id and `keep_data` decides exactly which areas outlive a recreate. And
safety under repetition: a second create adopts, and a remove of something
already gone succeeds.

The fake is strict where Docker is strict - a name in use, a volume in use, an
exec into a stopped container all fail - because those are the cases the
runtime exists to handle.
"""
import json
import os

import pytest

from agent import naming
from agent.runtimes.linux_container import (CREATE_TIMEOUT,
                                            KEPT_ON_RECREATE, LAYOUT_ENV,
                                            MOUNTS, LinuxContainerRuntime,
                                            LinuxRegistrar)

from .fake_docker import FakeDocker, FakeForge

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER = "550e8400-e29b-41d4-a716-446655440000"
SPEC = {"image": "ghcr.io/nomercy/runner:latest", "memory": "32g",
        "cpuset": "0-15", "env": {"RUNNER_LABELS": "self-hosted"}}


@pytest.fixture
def docker():
    return FakeDocker()


@pytest.fixture
def runtime(docker):
    return LinuxContainerRuntime(run=docker)


@pytest.fixture
def registrar(docker):
    return LinuxRegistrar(run=docker)


def run_argv(docker):
    return next(c for c in docker.calls if c[0] == "run")


class TestTheArgv:
    def test_it_carries_t0002s_stop_timeout(self, runtime, docker):
        """Docker Engine 29 defaults to one second, which kills the runner's
        deregistration on SIGTERM half-way."""
        runtime.create(RID, SPEC)
        argv = run_argv(docker)
        assert argv[argv.index("--stop-timeout") + 1] == "60"

    def test_swap_is_capped_at_the_memory_limit(self, runtime, docker):
        runtime.create(RID, SPEC)
        argv = run_argv(docker)
        assert argv[argv.index("--memory") + 1] == "32g"
        assert argv[argv.index("--memory-swap") + 1] == "32g"

    def test_the_cpuset_is_passed(self, runtime, docker):
        runtime.create(RID, SPEC)
        argv = run_argv(docker)
        assert argv[argv.index("--cpuset-cpus") + 1] == "0-15"

    def test_a_cpu_limit_of_zero_means_none(self, runtime, docker):
        runtime.create(RID, dict(SPEC, cpus="0"))
        assert "--cpus" not in run_argv(docker)

    def test_the_unit_is_named_and_labelled_by_its_runner_id(self, runtime,
                                                            docker):
        runtime.create(RID, SPEC)
        argv = run_argv(docker)
        assert argv[argv.index("--name") + 1] == naming.unit_name(RID)
        assert f"nomercy.runner_id={RID}" in argv

    def test_the_image_is_last(self, runtime, docker):
        runtime.create(RID, SPEC)
        assert run_argv(docker)[-1] == SPEC["image"]

    def test_removal_has_the_long_timeout(self, docker):
        """Tearing down a nested engine has been measured at 110 seconds."""
        seen = {}

        def run(args, **kw):
            if args[0] == "rm":
                seen["timeout"] = kw.get("timeout")
            return docker(args, **kw)

        LinuxContainerRuntime(run=run).remove(RID, keep_data=False)
        assert seen["timeout"] == 180

    def test_stop_gives_the_runner_its_grace_period(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        assert ["stop", "-t", "60", naming.unit_name(RID)] in docker.calls


class TestNoSecretReachesAnArgumentList:
    SECRET = "tok-SENTINEL-in-the-environment-1234"

    def test_the_environment_goes_in_a_file(self, runtime, docker):
        """Any user on the worker can read an argument list in `ps` for as
        long as the command runs."""
        runtime.create(RID, dict(SPEC, env={"TOKEN": self.SECRET}))
        for call in docker.calls:
            assert self.SECRET not in " ".join(call), call[0]
        assert "-e" not in run_argv(docker)

    def test_the_file_reached_docker(self, runtime, docker):
        runtime.create(RID, dict(SPEC, env={"TOKEN": self.SECRET}))
        env = docker.containers[naming.unit_name(RID)]["env"]
        assert env["TOKEN"] == self.SECRET

    def test_and_is_gone_afterwards(self, runtime, docker):
        runtime.create(RID, SPEC)
        argv = run_argv(docker)
        assert not os.path.exists(argv[argv.index("--env-file") + 1])

    def test_a_value_with_a_line_break_is_refused(self, runtime):
        """It would write a second variable of the sender's choosing."""
        with pytest.raises(ValueError, match="not one line"):
            runtime.create(RID, dict(SPEC, env={"A": "x\nPATH=/evil"}))

    def test_the_registration_plan_goes_on_standard_input(self, runtime,
                                                          registrar,
                                                          docker):
        runtime.create(RID, SPEC)
        registrar.register(RID, {"url": "https://git.example",
                                 "token": self.SECRET, "name": "rnr-1"})
        for call in docker.calls:
            assert self.SECRET not in " ".join(call)
        assert self.SECRET in docker.inputs[-1]


class TestTheLayout:
    def test_all_five_areas_are_mounted_where_the_layout_says(self, runtime,
                                                             docker):
        runtime.create(RID, SPEC)
        mounts = docker.containers[naming.unit_name(RID)]["mounts"]
        names = naming.names(RID, "linux")
        assert mounts == {names[a]: MOUNTS[a] for a in naming.AREAS}

    def test_the_volume_names_come_from_the_runner_id(self, runtime, docker):
        runtime.create(RID, SPEC)
        for area, name in naming.names(RID, "linux").items():
            assert name in docker.volumes, area
            assert docker.volumes[name]["labels"]["nomercy.runner_id"] == RID

    def test_the_image_is_told_the_layout(self, runtime, docker):
        runtime.create(RID, SPEC)
        env = docker.containers[naming.unit_name(RID)]["env"]
        for key, value in LAYOUT_ENV.items():
            assert env[key] == value

    def test_a_spec_cannot_move_the_layout(self, runtime, docker):
        runtime.create(RID, dict(SPEC, env={"RUNNER_WORK_DIR": "/etc"}))
        env = docker.containers[naming.unit_name(RID)]["env"]
        assert env["RUNNER_WORK_DIR"] == MOUNTS["work"]

    def test_the_nested_engine_keeps_its_data_where_t0002_put_it(self):
        assert MOUNTS["docker"] == "/var/lib/docker"


class TestCreatingIsSafeToRepeat:
    def test_a_second_create_adopts(self, runtime, docker):
        """After a crash or a lost reply, the controller asks again."""
        first = runtime.create(RID, SPEC)
        second = runtime.create(RID, SPEC)
        assert first == second
        assert [c[0] for c in docker.calls].count("run") == 1

    def test_a_create_that_stopped_half_way_is_finished(self, runtime,
                                                        docker):
        """A create whose client gave up while the engine was still
        unpacking the image leaves the unit made but never started. The
        next create must finish it: handing back a unit that is not running
        makes the registration that follows exec into nothing, which is
        what every rebuild did on 2026-09-20."""
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        assert runtime.create(RID, SPEC) == naming.unit_name(RID)
        assert runtime.status(RID)["running"] is True

    def test_the_create_has_room_for_a_cold_image(self, docker):
        """The first container made from a freshly built unit image waits
        for its layers to be unpacked into the snapshotter: measured at
        three minutes for the 17 GB GitHub unit on the WSL worker, against
        a deadline of 180 seconds that no rebuild ever survived. The room
        is bounded by the controller's own deadline for a slow verb."""
        seen = {}

        def run(args, **kw):
            if args[0] == "run":
                seen["timeout"] = kw.get("timeout")
            return docker(args, **kw)

        LinuxContainerRuntime(run=run).create(RID, SPEC)
        assert seen["timeout"] == CREATE_TIMEOUT == 240

    def test_a_unit_still_being_removed_is_waited_out(self, docker,
                                                     monkeypatch):
        """Docker's removal is asynchronous: the client returns while the
        daemon is still taking the unit apart, and anything said to it
        meanwhile is refused with "container is marked for removal". A
        recreate that reached the create while the old unit was still
        going therefore failed, and the one it was replacing was already
        gone (2026-09-20)."""
        import agent.runtimes.linux_container as lc
        monkeypatch.setattr(lc, "sleep", lambda seconds: None)
        runtime = LinuxContainerRuntime(run=docker)
        answers = iter([{"exists": True, "running": False,
                         "state": "removing"},
                        {"exists": True, "running": False,
                         "state": "removing"},
                        {"exists": False, "running": False,
                         "state": "absent"}])
        monkeypatch.setattr(runtime, "status",
                            lambda rid: next(answers))
        assert runtime.create(RID, SPEC) == naming.unit_name(RID)
        verbs = [c[0] for c in docker.calls]
        assert "run" in verbs and "update" not in verbs

    def test_losing_a_race_to_another_create_adopts_too(self, docker):
        runtime = LinuxContainerRuntime(run=docker)
        docker.fail_once["run"] = ('Conflict. The container name "/x" is '
                                   'already in use')
        assert runtime.create(RID, SPEC) == naming.unit_name(RID)

    def test_a_create_without_an_image_is_refused(self, runtime):
        with pytest.raises(ValueError, match="needs an image"):
            runtime.create(RID, {})

    def test_a_malformed_runner_id_never_becomes_a_name(self, runtime,
                                                       docker):
        with pytest.raises(naming.InvalidRunnerId):
            runtime.create("../../var/lib", SPEC)
        assert docker.calls == []


class TestRemoving:
    def test_without_keep_data_everything_goes(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=False)
        assert naming.unit_name(RID) not in docker.containers
        for name in naming.names(RID, "linux").values():
            assert name not in docker.volumes

    def test_keep_data_keeps_what_a_recreate_keeps(self, runtime, docker):
        """The engine's data, the cache and the logs survive; the workspace
        and the registration do not, because a recreate is for a clean
        runner with a fresh registration."""
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=True)
        names = naming.names(RID, "linux")
        for area in naming.AREAS:
            assert (names[area] in docker.volumes) == \
                (area in KEPT_ON_RECREATE), area

    def test_the_unit_is_stopped_before_it_is_forced(self, runtime, docker):
        """`rm -f` kills a unit ten seconds after asking, which takes its
        nested engine down mid-write. The engine was twice left unable to
        finish such a removal: the container sits in "removing", its name
        cannot be used again, and the runner that was being rebuilt is
        already gone (2026-09-20)."""
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=False)
        verbs = [c[0] for c in docker.calls]
        assert verbs.index("stop") < verbs.index("rm")

    def test_storage_goes_only_once_the_unit_has(self, docker,
                                                monkeypatch):
        """`docker rm` returns while the daemon is still taking the unit
        apart, and a volume the unit still holds cannot be removed:
        "volume is in use" is exactly what the undo of a failed create
        reported, leaving five volumes behind (2026-09-20)."""
        import agent.runtimes.linux_container as lc
        monkeypatch.setattr(lc, "sleep", lambda seconds: None)
        seen = []

        def run(args, **kw):
            seen.append("/".join(args[:2]) if args[0] == "volume"
                        else args[0])
            return docker(args, **kw)

        runtime = LinuxContainerRuntime(run=run)
        runtime.create(RID, SPEC)
        answers = iter([{"exists": True, "running": False,
                         "state": "removing"},
                        {"exists": False, "running": False,
                         "state": "absent"}])
        gone = {"exists": False, "running": False, "state": "absent"}

        def status(rid):
            seen.append("status")
            return next(answers, gone)

        monkeypatch.setattr(runtime, "status", status)
        runtime.remove(RID, keep_data=False)
        assert seen.index("status") < seen.index("volume/rm")

    def test_it_is_safe_when_nothing_is_there(self, runtime):
        runtime.remove(RID, keep_data=False)

    def test_storage_that_will_not_go_is_a_failure_not_a_footnote(
            self, runtime, docker):
        """To the controller, storage outliving its runner is a half
        instance."""
        runtime.create(RID, SPEC)
        docker.fail_once["volume"] = "permission denied"
        with pytest.raises(RuntimeError, match="storage left behind"):
            runtime.remove(RID, keep_data=False)

    def test_another_runners_storage_is_untouched(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        runtime.remove(RID, keep_data=False)
        for name in naming.names(OTHER, "linux").values():
            assert name in docker.volumes


class TestObserving:
    def test_status_of_a_running_unit(self, runtime):
        runtime.create(RID, SPEC)
        status = runtime.status(RID)
        assert status["exists"] is True and status["running"] is True

    def test_status_of_a_unit_that_is_not_there(self, runtime):
        assert runtime.status(RID) == {"exists": False, "running": False,
                                       "state": "absent"}

    def test_an_engine_that_cannot_answer_is_unknown_not_absent(self):
        runtime = LinuxContainerRuntime(
            run=lambda args, **kw: (False, "", "Cannot connect to the "
                                               "Docker daemon"))
        assert runtime.status(RID)["exists"] is None

    def test_instances_lists_this_engines_runners(self, runtime):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        runtime.stop(OTHER)
        states = {u["runner_id"]: u["state"] for u in runtime.instances()}
        assert states == {RID: "running", OTHER: "stopped"}

    def test_instances_raises_rather_than_claiming_nothing(self):
        runtime = LinuxContainerRuntime(
            run=lambda args, **kw: (False, "", "daemon down"))
        with pytest.raises(RuntimeError):
            runtime.instances()

    def test_logs_merge_both_streams(self, docker):
        seen = {}

        def run(args, **kw):
            if args[0] == "logs":
                seen["merge"] = kw.get("merge_stderr")
            return docker(args, **kw)

        runtime = LinuxContainerRuntime(run=run)
        runtime.create(RID, SPEC)
        runtime.logs(RID, 60)
        assert seen["merge"] is True

    def test_telemetry_reads_sizes(self, runtime):
        runtime.create(RID, SPEC)
        t = runtime.telemetry(RID)
        assert t["cpu_percent"] == 1.5
        assert t["mem_limit_bytes"] == 32 * 1024 ** 3

    def test_probes(self, runtime, docker):
        runtime.create(RID, SPEC)
        docker.engine(naming.names(RID, "linux")["docker"], build_cache=500,
                      images=700)
        assert runtime.probe(RID, "cache_size")["value"] == 500
        assert runtime.probe(RID, "disk_usage")["value"] == 700
        assert runtime.probe(RID, "agent_version")["value"] == "2.336.0"
        assert runtime.probe(RID, "job_state")["ok"] is False


class TestClearingTheCache:
    def test_engine_scopes_report_what_they_freed(self, runtime, docker):
        runtime.create(RID, SPEC)
        docker.engine(naming.names(RID, "linux")["docker"], build_cache=500,
                      images=700)
        freed = runtime.clear_cache(RID, {})
        assert freed["per_scope"] == {"engine-build-cache": 500,
                                      "engine-images-unused": 700}
        assert freed["total_bytes"] == 1200
        assert freed["measured"] is True

    def test_the_workspace_scope_empties_this_runners_workspace(
            self, runtime, docker):
        runtime.create(RID, SPEC)
        work = naming.names(RID, "linux")["work"]
        docker.put(work, "repo/checkout", 300)
        freed = runtime.clear_cache(RID, {"scopes": ["workspace"]})
        assert freed["per_scope"]["workspace"] == 300
        assert docker.volume_bytes(work) == 0

    def test_a_second_call_frees_nothing_and_succeeds(self, runtime, docker):
        runtime.create(RID, SPEC)
        docker.engine(naming.names(RID, "linux")["docker"], build_cache=500)
        runtime.clear_cache(RID, {})
        again = runtime.clear_cache(RID, {})
        assert again["total_bytes"] == 0
        assert again["errors"] == {}

    def test_nothing_of_another_runner_changes(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        docker.engine(naming.names(OTHER, "linux")["docker"],
                      build_cache=900)
        docker.put(naming.names(OTHER, "linux")["work"], "x", 50)
        theirs = {n: docker.snapshot()[n]
                  for n in naming.names(OTHER, "linux").values()}

        runtime.clear_cache(RID, {"scopes": ["engine-build-cache",
                                             "workspace", "temp"]})

        after = docker.snapshot()
        for name, before in theirs.items():
            assert after[name] == before, name

    def test_a_scope_this_runtime_cannot_clear_is_an_error_not_a_skip(
            self, runtime):
        runtime.create(RID, SPEC)
        freed = runtime.clear_cache(RID, {"scopes": ["somewhere-else"]})
        assert "not supported" in freed["errors"]["somewhere-else"]

    def test_a_unit_that_is_not_running_is_refused(self, runtime):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        with pytest.raises(RuntimeError, match="not running"):
            runtime.clear_cache(RID, {})


class TestRegistering:
    @pytest.fixture
    def unit(self, runtime):
        runtime.create(RID, SPEC)
        return runtime

    def test_register_reaches_the_forge(self, unit, registrar, docker):
        ids = registrar.register(RID, {"url": "https://git.example",
                                       "token": "tok-123456789",
                                       "name": "rnr-3f25"})
        assert ids["registration_id"] in docker.forge.records

    def test_deregister_removes_the_forge_record(self, unit, registrar,
                                                 docker):
        registrar.register(RID, {"url": "https://x", "token": "t" * 10})
        registrar.deregister(RID)
        assert docker.forge.records == {}

    def test_an_unreachable_forge_is_a_failure(self, unit, registrar,
                                               docker):
        docker.forge.reachable = False
        with pytest.raises(RuntimeError, match="did not answer"):
            registrar.register(RID, {"url": "https://x", "token": "t" * 10})
        assert docker.forge.records == {}

    def test_a_gone_unit_says_to_deregister_at_the_forge(self, registrar):
        with pytest.raises(RuntimeError, match="only be removed at the forge"):
            registrar.deregister(RID)


class TestWhatItDeclares:
    def test_it_runs_job_containers_and_nested_builds(self, runtime):
        caps = runtime.capabilities()
        assert caps["job_containers"] is True
        assert caps["nested_builds"] is True
        assert caps["kind"] == "linux-container"

    def test_it_drains(self, runtime):
        """OPEN-7, settled: a graceful stop that stays stopped."""
        assert runtime.capabilities()["supports_drain"] is True


class TestDrain:
    """SIGTERM, never `docker stop`: stop kills a job when its timeout runs
    out. And the restart policy off first, or the engine brings the unit
    straight back when its process exits."""

    def test_the_policy_goes_before_the_signal(self, runtime, docker):
        runtime.create(RID, SPEC)
        docker.calls.clear()
        runtime.drain(RID)
        verbs = [c[0] for c in docker.calls if c[0] in ("update", "kill",
                                                        "stop")]
        assert verbs == ["update", "kill"]
        assert ["update", "--restart=no", naming.unit_name(RID)] in             docker.calls
        assert ["kill", "--signal=TERM", naming.unit_name(RID)] in             docker.calls

    def test_a_running_job_is_finished_not_killed(self, runtime, docker):
        runtime.create(RID, SPEC)
        LinuxRegistrar(run=docker).register(RID, {"url": "https://x",
                                                  "token": "t" * 12})
        unit = naming.unit_name(RID)
        assert docker.start_job(unit)
        runtime.drain(RID)
        assert runtime.status(RID)["running"] is True, "still finishing"
        assert docker.finish_job(unit) is True
        assert runtime.status(RID)["running"] is False, "and then down"

    def test_a_drained_unit_stays_down(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.drain(RID)
        assert runtime.status(RID)["running"] is False
        assert docker.containers[naming.unit_name(RID)]["restart"] == "no"

    def test_cancel_puts_it_back_as_it_was(self, runtime, docker):
        runtime.create(RID, SPEC)
        runtime.drain(RID)
        runtime.cancel_drain(RID)
        assert runtime.status(RID)["running"] is True
        assert docker.containers[naming.unit_name(RID)]["restart"] ==             "unless-stopped"

    def test_draining_a_stopped_unit_only_keeps_it_down(self, runtime,
                                                        docker):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        docker.calls.clear()
        runtime.drain(RID)
        assert not any(c[0] == "kill" for c in docker.calls)
