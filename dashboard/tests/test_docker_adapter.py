"""The Docker runtime adapter, driven directly.

These assert the same argv the existing docker_ops tests assert, one level
down, so the two can be compared when docker_ops starts delegating. They also
pin the three behaviours that were learned the hard way and would be easy to
lose in a refactor:

  the 60s stop timeout, because start.sh deregisters on SIGTERM and Docker
  Engine 29.x creates containers with StopTimeout=1, which kills that
  mid-flight and orphans the registration;

  reading the data volume's real name from the container before removing it,
  because compose names it `<project>_<runner>-docker` and the dashboard names
  it `<runner>-docker`, so a reconstructed name silently leaves tens of GB
  behind;

  never turning an unreadable measurement into a zero, because a runner whose
  memory could not be sampled is not a runner using no memory.
"""
import pytest

from runtime import base
from runtime.docker_adapter import DockerRuntimeAdapter


def adapter(reply=None, record=None):
    """An adapter whose engine call is injected, so nothing shells out."""
    calls = record if record is not None else []

    def run(*args, **kwargs):
        calls.append(args)
        if callable(reply):
            return reply(*args, **kwargs)
        return reply or (True, "", "")

    a = DockerRuntimeAdapter(run=run, run_logs=run)
    a.calls = calls
    return a


REF = base.ExecUnitRef(kind=base.ExecUnitKind.LINUX_CONTAINER,
                       handle="github-runner-1")


class TestLifecycleArgv:
    def test_start(self):
        a = adapter()
        a.start(REF)
        assert a.calls[0] == ("start", "github-runner-1")

    def test_stop_uses_a_grace_period_long_enough_to_deregister(self):
        a = adapter()
        a.stop(REF)
        assert a.calls[0] == ("stop", "-t", "60", "github-runner-1")

    def test_restart(self):
        a = adapter()
        a.restart(REF)
        assert a.calls[0] == ("restart", "-t", "60", "github-runner-1")

    def test_a_failed_call_raises_rather_than_returning_false(self):
        """The controller distinguishes 'did not work' from 'nothing to do'."""
        a = adapter(reply=(False, "", "no such container"))
        with pytest.raises(RuntimeError, match="no such container"):
            a.start(REF)


class TestCreate:
    def _spec(self, **over):
        spec = {
            "name": "github-runner-9",
            "image": "ghcr.io/example/runner:latest",
            "labels": {"nomercy.runner": "true"},
            "env": {"GH_TOKEN": "t"},
            "mounts": ["github-runner-9-docker:/var/lib/docker"],
            "cpus": "0",
            "memory": "32g",
        }
        spec.update(over)
        return spec

    def test_it_builds_the_run_argv(self):
        a = adapter()
        ref = a.create(self._spec())
        argv = a.calls[0]
        assert argv[0] == "run" and "-d" in argv
        assert "--privileged" in argv
        assert ref.handle == "github-runner-9"
        assert ref.kind is base.ExecUnitKind.LINUX_CONTAINER

    def test_the_stop_timeout_is_set_explicitly(self):
        a = adapter()
        a.create(self._spec())
        argv = a.calls[0]
        assert argv[argv.index("--stop-timeout") + 1] == "60"

    def test_memory_and_swap_are_capped_together(self):
        """Swap above the memory limit would page the overflow to disk instead
        of making the kernel reclaim the runner's page cache."""
        a = adapter()
        a.create(self._spec(memory="32g"))
        argv = a.calls[0]
        assert argv[argv.index("--memory") + 1] == "32g"
        assert argv[argv.index("--memory-swap") + 1] == "32g"

    def test_a_zero_limit_passes_no_flag_at_all(self):
        a = adapter()
        a.create(self._spec(memory="0", cpus="0"))
        argv = a.calls[0]
        assert "--memory" not in argv and "--cpus" not in argv

    def test_it_knows_nothing_about_providers(self):
        """The spec is already resolved; the adapter never asks a forge."""
        import runtime.docker_adapter as mod
        src = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("providers.", "GITHUB", "FORGEJO", "github_api",
                          "forgejo_api"):
            assert forbidden not in src, forbidden

    def test_a_failed_create_raises(self):
        a = adapter(reply=(False, "", "name already in use"))
        with pytest.raises(RuntimeError, match="already in use"):
            a.create(self._spec())


class TestRemove:
    def test_the_volume_name_is_read_from_the_container(self):
        """Not reconstructed: compose prefixes it with the project name."""
        seen = []

        def reply(*args, **kwargs):
            if args[:2] == ("inspect", "--format"):
                return (True, "githubrunners_github-runner-1-docker", "")
            return (True, "", "")

        a = adapter(reply=reply, record=seen)
        a.remove(REF)
        flat = [" ".join(c) for c in seen]
        assert any("volume rm githubrunners_github-runner-1-docker" in f
                   for f in flat), flat

    def test_the_lookup_happens_before_the_container_is_gone(self):
        seen = []

        def reply(*args, **kwargs):
            if args[:2] == ("inspect", "--format"):
                return (True, "vol", "")
            return (True, "", "")

        a = adapter(reply=reply, record=seen)
        a.remove(REF)
        verbs = [c[0] for c in seen]
        assert verbs.index("inspect") < verbs.index("rm")

    def test_keep_data_never_touches_the_volume(self):
        a = adapter()
        a.remove(REF, keep_data=True)
        assert not any(c[0] == "volume" for c in a.calls)
        assert not any(c[0] == "inspect" for c in a.calls)

    def test_a_runner_without_such_a_volume_removes_nothing_extra(self):
        a = adapter(reply=(True, "", ""))
        a.remove(REF)
        assert not any(c[0] == "volume" for c in a.calls)

    def test_a_volume_that_will_not_delete_does_not_block_the_removal(self):
        def reply(*args, **kwargs):
            if args[:2] == ("inspect", "--format"):
                return (True, "vol", "")
            if args[0] == "volume":
                return (False, "", "volume is in use")
            return (True, "", "")

        a = adapter(reply=reply)
        a.remove(REF)  # must not raise


class TestObservation:
    def test_status_of_a_missing_container_is_not_an_error(self):
        a = adapter(reply=(False, "", "No such object"))
        s = a.status(REF)
        assert s.exists is False and s.running is False

    def test_status_parses_the_inspect_line(self):
        a = adapter(reply=(True, "running|0|2026-09-17T10:00:00Z|3", ""))
        s = a.status(REF)
        assert s.running and s.exit_code == 0 and s.restart_count == 3

    def test_unreadable_telemetry_is_none_not_zero(self):
        """A runner whose memory could not be sampled is not using no memory."""
        a = adapter(reply=(False, "", "boom"))
        t = a.telemetry(REF)
        assert t.cpu_percent is None
        assert t.mem_used_bytes is None

    def test_telemetry_parses_stats(self):
        def reply(*args, **kwargs):
            if args[0] == "stats":
                return (True, "412.5%\t8.5GiB / 32GiB", "")
            return (False, "", "")

        a = adapter(reply=reply)
        t = a.telemetry(REF)
        assert t.cpu_percent == 412.5
        assert t.mem_used_bytes == int(8.5 * 1024 ** 3)
        assert t.mem_limit_bytes == 32 * 1024 ** 3


class TestProbesAreClosed:
    def test_job_state_is_refused_here(self):
        """Busy or idle is authoritative at the forge, never from the runtime."""
        a = adapter()
        r = a.exec_probe(REF, base.Probe.JOB_STATE)
        assert r.ok is False and "forge" in r.error

    def test_agent_version_reads_a_fixed_path(self):
        a = adapter(reply=(True, "2.336.0\n", ""))
        r = a.exec_probe(REF, base.Probe.AGENT_VERSION)
        assert r.ok and r.value == "2.336.0"
        assert a.calls[0][:2] == ("exec", "github-runner-1")

    def test_there_is_no_probe_that_carries_a_command(self):
        for member in base.Probe:
            a = adapter()
            r = a.exec_probe(REF, member)
            assert isinstance(r, base.ProbeResult)


class TestCapabilities:
    def test_linux_declares_what_it_can_do(self):
        c = DockerRuntimeAdapter().capabilities()
        assert c.kind is base.ExecUnitKind.LINUX_CONTAINER
        assert c.job_containers is True
        assert c.resettable_os is False


class TestClearCache:
    """The verb that had no test at all, and was broken because of it.

    `_df_sizes` returns display strings per scope - {"build_cache": "36GB",
    "images": "24GB"} - and the first version of clear_cache subtracted those
    two dicts as though they were byte counts. Every successful measurement
    raised TypeError. Nothing caught it because docker_ops.prune still carried
    its own copy of the body, so the adapter's copy was never executed.
    """

    def df(self, cache, images):
        return {"Build Cache": {"Size": cache}, "Images": {"Size": images}}

    def build(self, monkeypatch, dfs=(), reply=(True, "", ""), record=None):
        """An adapter whose df measurements are scripted in order.

        df is stubbed at `docker_ops._df_rows`, not through the engine stub,
        because that is the seam clear_cache actually reads through. An empty
        script means df never answers, which is the unmeasurable case.
        """
        import docker_ops
        seq = list(dfs)
        monkeypatch.setattr(docker_ops, "_df_rows",
                            lambda name: seq.pop(0) if seq else None)
        return adapter(reply=reply, record=record)

    def test_it_reports_a_real_number(self, monkeypatch):
        a = self.build(monkeypatch,
                       [self.df("36GB", "24GB"), self.df("0B", "24GB")])
        freed = a.clear_cache(REF, {})
        assert freed.measured is True
        assert freed.total_bytes == 36 * 10 ** 9
        assert freed.before["build_cache"] == "36GB"
        assert freed.after["build_cache"] == "0B"

    def test_both_prunes_are_issued(self, monkeypatch):
        calls = []
        a = self.build(monkeypatch,
                       [self.df("1GB", "1GB"), self.df("0B", "0B")],
                       record=calls)
        a.clear_cache(REF, {})
        joined = [" ".join(c) for c in calls]
        assert any("buildx prune" in c for c in joined)
        assert any("image prune" in c for c in joined)

    def test_an_unreadable_measurement_is_not_a_zero(self, monkeypatch):
        """The failure this class exists to prevent: reading a failed df as 0B
        turns an unknown after-state into "everything was reclaimed"."""
        freed = self.build(monkeypatch).clear_cache(REF, {})
        assert freed.measured is False
        assert freed.after is None

    def test_a_timed_out_buildx_prune_issues_no_second_prune(self, monkeypatch):
        """The client was killed; the daemon is very likely still sweeping, and
        a second prune would race it."""
        calls = []

        def reply(*args, **kwargs):
            if "buildx" in args:
                return False, "", "timed out after 300s"
            return True, "", ""

        a = self.build(monkeypatch, [self.df("1GB", "1GB")], reply=reply,
                       record=calls)
        freed = a.clear_cache(REF, {})
        joined = [" ".join(c) for c in calls]
        assert any("buildx prune" in c for c in joined)
        assert not any("image prune" in c for c in joined)
        assert freed.still_working is True
        assert freed.measured is False

    def test_still_working_is_a_flag_not_a_phrase(self, monkeypatch):
        """A caller must not have to match on message text to learn this."""
        freed = self.build(monkeypatch).clear_cache(REF, {})
        assert freed.still_working is False

    def test_a_concurrent_build_never_produces_a_negative(self, monkeypatch):
        """Another build on the same daemon can grow the cache mid-prune."""
        a = self.build(monkeypatch,
                       [self.df("1GB", "1GB"), self.df("5GB", "5GB")])
        freed = a.clear_cache(REF, {})
        assert freed.total_bytes == 0
        assert all(v >= 0 for v in freed.per_scope.values())

    def test_the_total_is_the_net_not_the_sum_of_the_clamps(self, monkeypatch):
        """Cache down 10GB while images grow 3GB is 7GB freed, not 10GB. The
        per-scope figures are clamped for display; the total is the truth."""
        a = self.build(monkeypatch,
                       [self.df("10GB", "1GB"), self.df("0B", "4GB")])
        freed = a.clear_cache(REF, {})
        assert freed.total_bytes == 7 * 10 ** 9
        assert freed.per_scope["engine-images-unused"] == 0

    def test_a_failed_prune_still_reports_the_other_scope(self, monkeypatch):
        """They are independent; reclaiming one of the two beats neither."""
        def reply(*args, **kwargs):
            if "image" in args:
                return False, "", "daemon refused"
            return True, "", ""

        a = self.build(monkeypatch,
                       [self.df("10GB", "1GB"), self.df("0B", "1GB")],
                       reply=reply)
        freed = a.clear_cache(REF, {})
        assert "engine-images-unused" in freed.errors
        assert freed.measured is True
        assert freed.per_scope["engine-build-cache"] == 10 * 10 ** 9

    def test_nothing_outside_the_unit_is_named(self, monkeypatch):
        """Every argv must address this container and nothing else."""
        calls = []
        a = self.build(monkeypatch,
                       [self.df("1GB", "1GB"), self.df("0B", "0B")],
                       record=calls)
        a.clear_cache(REF, {})
        for c in calls:
            assert c[0] == "exec" and c[1] == REF.handle, c
