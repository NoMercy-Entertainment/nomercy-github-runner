"""The Windows process runtime, against a worker that behaves like Windows.

Four groups. The isolation, because it is the whole reason this runtime has
the shape it has: a service under its own virtual account, a directory tree
that only that account, LocalSystem and Administrators can open, and a Job
Object around everything it starts. The storage and `keep_data`, which follow
Linux's table. Safety under repetition - a create cut off anywhere converges.
And the job host, run for real against this machine's kernel where the tests
run on Windows, because a Job Object that is only faked proves nothing about
the cap.
"""
import json
import ntpath
import os
import subprocess
import sys
import textwrap

import pytest

from agent import jobhost, naming
from agent.runtimes.windows_process import (ADMINISTRATORS_SID,
                                            KEPT_ON_RECREATE, SYSTEM_SID,
                                            WindowsProcessRuntime,
                                            WindowsRegistrar, service_sid,
                                            size_bytes)

from .fake_windows import TEMPLATE, TOOLS, FakeWindows, _utf16ish

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER = "550e8400-e29b-41d4-a716-446655440000"
SECRET = "ghp_this-is-a-token-value-0123456789"
SPEC = {"image": TEMPLATE, "memory": "32g", "cpus": "16", "cpuset": "0-15",
        "env": {"RUNNER_LABELS": "self-hosted", "SOME_TOKEN": SECRET}}
PLAN = {"url": "https://github.com/NoMercy-Entertainment",
        "token": "AAAAREGISTRATIONTOKEN0123", "name": "win-1",
        "labels": "self-hosted,windows"}
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


@pytest.fixture
def host():
    return FakeWindows()


@pytest.fixture
def runtime(host):
    return WindowsProcessRuntime(run=host, fs=host, tools=TOOLS)


@pytest.mark.parametrize("service_state", ["SERVICE_STOP_PENDING", "SERVICE_START_PENDING",
                                           "SERVICE_PAUSED", "SERVICE_PAUSE_PENDING", "nonsense"])
def test_transitional_service_is_not_proof_of_quiescence(service_state):
    runtime = WindowsProcessRuntime(run=lambda *args, **kwargs: (True, service_state, ""))
    assert runtime.status(RID)["running"] is None


@pytest.fixture
def registrar(host):
    return WindowsRegistrar(run=host, fs=host, tools=TOOLS)


def calls(host, tool, verb=None):
    return [c for c in host.calls
            if c[0] == TOOLS[tool] and (verb is None or c[1] == verb)]


def test_service_launcher_ignores_runner_module_and_python_environment(tmp_path):
    package = tmp_path / "agent"
    package.mkdir()
    (package / "__init__.py").write_text("print('UNTRUSTED_RUNNER_MODULE'); raise SystemExit(42)")
    (tmp_path / "sitecustomize.py").write_text("print('UNTRUSTED_PYTHON_STARTUP')")
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONHOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-I", "-B", os.path.join(REPO, "agent", "launch_jobhost.py"), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "usage: jobhost" in result.stdout
    assert "--limits-json" in result.stdout
    assert "UNTRUSTED" not in result.stdout + result.stderr


class TestTheServiceSid:
    """Measured with `sc.exe showsid` on this host, 2026-09-18."""

    @pytest.mark.parametrize("name,sid", [
        ("TrustedInstaller",
         "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"),
        (f"rnr-{RID}",
         "S-1-5-80-2093951590-2521072173-1348894820-2634293686-1706955918"),
    ])
    def test_it_is_the_sid_windows_derives(self, name, sid):
        assert service_sid(name) == sid

    def test_it_ignores_case_as_windows_does(self):
        assert service_sid("RNR-ABC") == service_sid("rnr-abc")


class TestSizes:
    @pytest.mark.parametrize("text,expected", [
        ("32g", 32 * 1024 ** 3), ("512m", 512 * 1024 ** 2), ("1.5g",
                                                             int(1.5 * 1024 ** 3)),
        ("8GB", 8 * 1024 ** 3), ("", None), (None, None), ("0", None)])
    def test_docker_sizes_are_1024_based(self, text, expected):
        assert size_bytes(text) == expected


class TestIsolation:
    def test_the_service_runs_as_its_own_virtual_account(self, runtime, host):
        runtime.create(RID, SPEC)
        svc = host.services[naming.unit_name(RID)]
        assert svc["account"] == f"NT SERVICE\\rnr-{RID}"
        assert svc["sidtype"] == "unrestricted"

    def test_it_never_runs_as_localsystem(self, runtime, host):
        """NSSM installs services as LocalSystem. Configured before it is
        started, every time."""
        runtime.create(RID, SPEC)
        order = [c[1] for c in host.calls
                 if c[0] in (TOOLS["nssm"], TOOLS["sc"])]
        assert order.index("config") < order.index("start")

    def test_the_tree_is_locked_to_this_runner_alone(self, runtime, host):
        runtime.create(RID, SPEC)
        root = runtime.paths(RID)["root"]
        acl = host.acls[os.path.normcase(root).replace("/", "\\")]
        assert acl["inheritance"] == "r", "nothing inherited from above"
        sids = sorted(g.split(":")[0].lstrip("*") for g in acl["grants"])
        assert sids == sorted([SYSTEM_SID, ADMINISTRATORS_SID,
                               service_sid(f"rnr-{RID}")])

    def test_another_runners_account_is_not_in_it(self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        mine = host.acls[os.path.normcase(runtime.paths(RID)["root"])]
        assert not any(service_sid(f"rnr-{OTHER}") in g
                       for g in mine["grants"])

    def test_the_tree_is_locked_before_anything_is_put_in_it(self, host):
        seen = {}
        real_copy = host.copytree

        def copy(src, dst):
            seen["acl_first"] = bool(host.acls)
            return real_copy(src, dst)
        host.copytree = copy
        WindowsProcessRuntime(run=host, fs=host, tools=TOOLS).create(RID,
                                                                     SPEC)
        assert seen["acl_first"] is True

    def test_the_job_host_is_what_the_service_runs(self, runtime, host):
        runtime.create(RID, SPEC)
        svc = host.services[naming.unit_name(RID)]
        assert svc["program"] == TOOLS["python"]
        launcher = os.path.join(REPO, "agent", "launch_jobhost.py")
        assert svc["args"][:5] == ["-I", "-B", launcher, "--root",
                                   runtime.paths(RID)["root"]]
        assert svc["args"][5] == "--limits-json"
        assert json.loads(svc["args"][6]) == {
            "memory_bytes": 32 * 1024**3, "cpus": 16.0, "cpuset": "0-15"}
        assert svc["settings"]["AppParameters"] == [subprocess.list2cmdline(svc["args"])]

    def test_limits_and_environment_go_to_the_unit_file(self, runtime, host):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        unit = json.loads(host.read_text(p["reg"] + r"\unit.json"))
        assert unit["memory_bytes"] == 32 * 1024 ** 3
        assert unit["cpus"] == 16.0
        assert unit["cpuset"] == "0-15"
        assert unit["env"]["SOME_TOKEN"] == SECRET
        assert unit["env"]["RUNNER_WORK_DIR"] == p["work"]
        assert unit["env"]["TEMP"] == unit["env"]["TMP"] == p["tmp"]

    def test_no_environment_value_is_ever_on_a_command_line(self, runtime,
                                                           host):
        runtime.create(RID, SPEC)
        runtime.restart(RID)
        assert not any(SECRET in " ".join(c) for c in host.calls)

    def test_the_stop_gives_the_runner_its_grace(self, runtime, host):
        runtime.create(RID, SPEC)
        settings = host.services[naming.unit_name(RID)]["settings"]
        assert settings["AppStopMethodConsole"] == ["60000"]
        assert settings["AppExit"] == ["Default", "Restart"]


class TestCreate:
    def test_it_makes_every_area_but_the_engine(self, runtime, host):
        runtime.create(RID, SPEC)
        for area, path in naming.names(RID, "windows").items():
            if path is not None:
                assert host.exists(path), area
        assert naming.names(RID, "windows")["docker"] is None

    def test_it_copies_the_template_into_reg(self, runtime, host):
        runtime.create(RID, SPEC)
        reg = runtime.paths(RID)["reg"]
        for f in ("run.cmd", "register.ps1", "deregister.ps1"):
            assert host.exists(reg + "\\" + f)

    def test_the_unit_is_started(self, runtime):
        runtime.create(RID, SPEC)
        assert runtime.status(RID)["running"] is True

    def test_an_unknown_template_is_refused_before_anything_is_made(
            self, runtime, host):
        with pytest.raises(RuntimeError, match="no runner template"):
            runtime.create(RID, dict(SPEC, image="nope-1.0"))
        assert host.services == {}
        assert not host.exists(runtime.paths(RID)["root"])

    def test_no_template_at_all_is_refused(self, runtime):
        with pytest.raises(ValueError):
            runtime.create(RID, {})

    def test_creating_twice_adopts(self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.create(RID, SPEC)
        assert len(calls(host, "nssm", "install")) == 1
        assert list(host.services) == [naming.unit_name(RID)]

    def test_the_template_is_copied_once(self, runtime, host):
        """A second copy would write over the files a registered, running
        runner holds open."""
        copies = []
        real = host.copytree
        host.copytree = lambda s, d: (copies.append(d), real(s, d))
        runtime.create(RID, SPEC)
        runtime.create(RID, SPEC)
        assert len(copies) == 1

    @pytest.mark.parametrize("step", ["install", "config", "sidtype", "set"])
    def test_a_create_cut_off_anywhere_converges(self, runtime, host, step):
        from .fake_docker import Crash
        host.crash_after = step
        with pytest.raises(Crash):
            runtime.create(RID, SPEC)
        runtime.create(RID, SPEC)
        svc = host.services[naming.unit_name(RID)]
        assert svc["account"] == f"NT SERVICE\\rnr-{RID}"
        assert svc["state"] == "running"
        assert len(host.services) == 1


class TestRemove:
    def test_keep_data_false_removes_the_whole_tree(self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=False)
        assert host.services == {}
        assert not host.exists(runtime.paths(RID)["root"])

    def test_keep_data_true_keeps_the_cache_and_the_logs(self, runtime, host):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        for area in ("work", "cache", "logs"):
            host.put(p[area] + r"\x", 10)
        runtime.remove(RID, keep_data=True)
        assert KEPT_ON_RECREATE == ("cache", "logs")
        assert host.exists(p["cache"] + r"\x")
        assert host.exists(p["logs"] + r"\x")
        assert not host.exists(p["work"])
        assert not host.exists(p["reg"])
        assert not host.exists(p["tmp"])

    def test_the_service_is_stopped_before_it_is_removed(self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=False)
        verbs = [c[1] for c in calls(host, "nssm")]
        assert verbs.index("stop") < verbs.index("remove")

    def test_removing_what_is_gone_succeeds(self, runtime):
        runtime.remove(RID, keep_data=False)
        runtime.remove(RID, keep_data=True)

    def test_storage_that_cannot_be_deleted_is_a_failure(self, runtime, host):
        runtime.create(RID, SPEC)
        host.locked.add(runtime.paths(RID)["work"] + r"\held.dll")
        host.put(runtime.paths(RID)["work"] + r"\held.dll", 1)
        with pytest.raises(RuntimeError, match="storage left behind"):
            runtime.remove(RID, keep_data=False)

    def test_a_recreate_after_keep_data_copies_the_template_again(
            self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.remove(RID, keep_data=True)
        runtime.create(RID, SPEC)
        assert host.exists(runtime.paths(RID)["reg"] + r"\register.ps1")


class TestObservation:
    def test_status_of_an_absent_unit(self, runtime):
        assert runtime.status(RID) == {"exists": False, "running": False,
                                       "state": "absent"}

    def test_status_stopped(self, runtime):
        runtime.create(RID, SPEC)
        runtime.stop(RID)
        s = runtime.status(RID)
        assert (s["exists"], s["running"], s["state"]) == (True, False,
                                                           "exited")

    def test_status_is_unknown_when_nobody_answers(self, host):
        def broken(args, input=None, timeout=None):
            return False, "", "RPC server is unavailable"
        rt = WindowsProcessRuntime(run=broken, fs=host, tools=TOOLS)
        assert rt.status(RID)["exists"] is None

    def test_telemetry_reads_a_fresh_report(self, runtime, host):
        import time
        runtime.create(RID, SPEC)
        host.write_text(runtime.paths(RID)["logs"] + r"\telemetry.json",
                        json.dumps({"at": time.time(), "cpu_percent": 12.5,
                                    "mem_used_bytes": 1024}))
        t = runtime.telemetry(RID)
        assert t == {"cpu_percent": 12.5, "mem_used_bytes": 1024,
                     "mem_limit_bytes": 32 * 1024 ** 3,
                     "cpu_cores": 16, "host_cores": os.cpu_count()}

    def test_a_stale_report_is_unknown_not_zero(self, runtime, host):
        runtime.create(RID, SPEC)
        host.write_text(runtime.paths(RID)["logs"] + r"\telemetry.json",
                        json.dumps({"at": 0, "cpu_percent": 0,
                                    "mem_used_bytes": 0}))
        t = runtime.telemetry(RID)
        assert t["cpu_percent"] is None and t["mem_used_bytes"] is None

    def test_logs_are_the_tail_of_the_runner_log(self, runtime, host):
        runtime.create(RID, SPEC)
        host.write_text(runtime.paths(RID)["logs"] + r"\runner.log",
                        "x" * 10 + "last line")
        assert runtime.logs(RID, 300, max_bytes=9) == "last line"

    def test_no_log_is_empty_not_an_error(self, runtime):
        assert runtime.logs(RID, 300) == ""

    def test_probes(self, runtime, host):
        runtime.create(RID, SPEC)
        host.put(runtime.paths(RID)["cache"] + r"\c", 700)
        assert runtime.probe(RID, "cache_size") == {"ok": True, "value": 700}
        assert runtime.probe(RID, "disk_usage")["value"] >= 700
        assert runtime.probe(RID, "job_state")["ok"] is False
        assert runtime.probe(RID, "nonsense")["ok"] is False

    def test_instances_lists_runner_services_only(self, runtime, host):
        runtime.create(RID, SPEC)
        runtime.create(OTHER, SPEC)
        runtime.stop(OTHER)
        host.services["Spooler"] = dict(host.services[f"rnr-{RID}"])
        host.services["rnr-not-a-uuid"] = dict(host.services[f"rnr-{RID}"])
        assert sorted((i["runner_id"], i["state"])
                      for i in runtime.instances()) == sorted(
            [(RID, "running"), (OTHER, "stopped")])

    def test_instances_raises_rather_than_saying_nothing_runs(self, host):
        def broken(args, input=None, timeout=None):
            return False, "", "access denied"
        with pytest.raises(RuntimeError):
            WindowsProcessRuntime(run=broken, fs=host,
                                  tools=TOOLS).instances()


class TestClearCache:
    def test_the_default_is_the_caches_not_the_workspace(self, runtime,
                                                        host):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        host.put(p["cache"] + r"\a", 5000)
        host.put(p["tmp"] + r"\b", 300)
        host.put(p["work"] + r"\repo", 900)
        result = runtime.clear_cache(RID, {})
        assert result["per_scope"] == {"toolcache": 5000, "temp": 300}
        assert result["total_bytes"] == 5300
        assert host.exists(p["work"] + r"\repo")

    def test_engine_scopes_are_errors_not_silence(self, runtime):
        runtime.create(RID, SPEC)
        result = runtime.clear_cache(RID, {"scopes": ["engine-build-cache",
                                                      "toolcache"]})
        assert "engine-build-cache" in result["errors"]

    def test_a_failing_scope_does_not_stop_the_others(self, runtime, host):
        runtime.create(RID, SPEC)
        p = runtime.paths(RID)
        host.put(p["cache"] + r"\held", 10)
        host.locked.add(p["cache"] + r"\held")
        host.put(p["tmp"] + r"\t", 20)
        result = runtime.clear_cache(RID, {"scopes": ["toolcache", "temp"]})
        assert "toolcache" in result["errors"]
        assert result["per_scope"]["temp"] == 20

    def test_a_second_call_frees_nothing_and_succeeds(self, runtime, host):
        runtime.create(RID, SPEC)
        host.put(runtime.paths(RID)["cache"] + r"\a", 5000)
        runtime.clear_cache(RID, {})
        again = runtime.clear_cache(RID, {})
        assert again["total_bytes"] == 0 and again["errors"] == {}

    def test_every_scope_is_inside_this_runners_tree(self, runtime):
        root = runtime.paths(RID)["root"]
        p = runtime.paths(RID)
        for path in (p["work"], p["cache"], p["tmp"]):
            assert path.startswith(root + "\\")


class TestRegistrar:
    def test_the_plan_goes_on_stdin_and_never_in_argv(self, runtime,
                                                      registrar, host):
        runtime.create(RID, SPEC)
        registrar.register(RID, PLAN)
        assert json.loads(host.inputs[-1])["token"] == PLAN["token"]
        assert not any(PLAN["token"] in " ".join(c) for c in host.calls)

    def test_it_requests_registration_through_the_service(self, runtime, registrar,
                                              host):
        runtime.create(RID, SPEC)
        registrar.register(RID, PLAN)
        argv = calls(host, "python")[-1]
        assert argv[1:] == ["-m", "agent.windows_registration", "--runner-id", RID]
        assert not any(runtime.paths(RID)["reg"] in " ".join(call)
                       for call in calls(host, "powershell"))

    def test_it_returns_the_forges_ids(self, runtime, registrar, host):
        runtime.create(RID, SPEC)
        ids = registrar.register(RID, PLAN)
        assert host.forge.records[ids["registration_id"]]["unit"] == \
            naming.unit_name(RID)

    def test_a_unit_with_no_entry_point_is_refused(self, registrar):
        with pytest.raises(RuntimeError, match="no registration entry"):
            registrar.register(RID, PLAN)

    def test_deregistering_a_gone_unit_points_at_the_forge(self, registrar):
        with pytest.raises(RuntimeError, match="removed at the forge"):
            registrar.deregister(RID)

    def test_an_unreadable_answer_is_an_error(self, runtime, host):
        runtime.create(RID, SPEC)

        def mute(args, input=None, timeout=None):
            return True, "done", ""
        with pytest.raises(RuntimeError, match="did not say"):
            WindowsRegistrar(run=mute, fs=host, tools=TOOLS).register(RID,
                                                                      PLAN)


class TestCapabilities:
    def test_they_say_what_windows_cannot_do(self, runtime):
        caps = runtime.capabilities()
        assert caps["kind"] == "windows-process"
        assert caps["job_containers"] is False
        assert caps["nested_builds"] is False
        assert caps["supports_drain"] is True
        assert caps["cache_scopes"] == ["temp", "toolcache", "workspace"]

    def test_it_declares_its_own_host_cores(self, runtime):
        """The same key the Linux runtime declares, the same way
        (agent/runtimes/linux_container.py's own `host_cores`): what a
        pinned window on this worker is cut from, known before any runner
        is here to report it in telemetry. Without it, a small guest with
        no runner yet is invisible to the controller's window sizing,
        which is how one was pinned to cores that do not exist on it
        (2026-09-23)."""
        assert runtime.capabilities()["host_cores"] == os.cpu_count()


class TestTheJobHostsArithmetic:
    @pytest.mark.parametrize("cpus,count,rate", [
        (16, 56, 2857), (56, 56, 10000), (100, 56, 10000), (0, 56, None),
        (None, 56, None), (0.001, 56, 1)])
    def test_cpus_become_a_rate_per_ten_thousand(self, cpus, count, rate):
        assert jobhost.cpu_rate(cpus, count) == rate

    def test_a_cpuset_becomes_an_affinity_mask(self):
        assert jobhost.affinity_mask("0-3") == 0b1111
        assert jobhost.affinity_mask("0,2") == 0b101
        assert jobhost.affinity_mask("") is None

    def test_a_cpuset_beyond_one_mask_is_refused_not_truncated(self):
        with pytest.raises(ValueError):
            jobhost.affinity_mask("60-70")


def _is_packaged(path):
    """The Microsoft Store Python lives behind an app execution alias."""
    return "\\windowsapps\\" in os.path.normcase(path or "")


def _regular_python():
    """An interpreter that is not the Store's, if this machine has one."""
    import glob
    for c in (sys.executable, getattr(sys, "_base_executable", None),
              *sorted(glob.glob(os.path.expanduser(
                  r"~\.local\bin\python3*.exe")))):
        if c and os.path.exists(c) and not _is_packaged(c):
            return c
    return None


HOG = ("@powershell -NoProfile -Command \"$ErrorActionPreference='Stop'; "
       "$a = New-Object byte[] (512MB); 'escaped'\"\r\n"
       "@exit /b %ERRORLEVEL%\r\n")


@pytest.mark.skipif(sys.platform != "win32",
                    reason="a Job Object needs the Windows kernel")
class TestTheJobHostForReal:
    """Against this machine's kernel: a child process, a memory cap, and
    nothing else touched. The hog is PowerShell reached through `run.cmd`, so
    the cap is shown to hold two processes down, as it must for a runner and
    the job it starts."""

    def _unit(self, tmp_path, run_cmd, memory):
        for d in ("reg", "work", "logs"):
            (tmp_path / d).mkdir()
        (tmp_path / "reg" / "unit.json").write_text(json.dumps(
            {"env": {"MARKER": "from-the-unit-file"},
             "memory_bytes": memory, "cpus": None, "cpuset": None}))
        (tmp_path / "reg" / "run.cmd").write_text(run_cmd, newline="")

    def _run(self, tmp_path, python):
        return subprocess.run(
            [python, "-I", "-B", os.path.join(REPO, "agent", "launch_jobhost.py"),
             "--root", str(tmp_path),
             "--report-seconds", "0.5"],
            cwd=tmp_path / "work", capture_output=True, text=True, timeout=120)

    @pytest.fixture
    def python(self):
        found = _regular_python()
        if found is None:
            pytest.skip("only the Store Python is installed here")
        return found

    def test_a_memory_hog_is_capped(self, tmp_path, python):
        self._unit(tmp_path, HOG, 128 * 1024 * 1024)
        p = self._run(tmp_path, python)
        assert "escaped" not in p.stdout
        assert "OutOfMemoryException" in p.stdout + p.stderr
        assert p.returncode != 0

    def test_the_same_hog_runs_under_a_cap_it_fits(self, tmp_path, python):
        """So the failure above is the cap, not the hog."""
        self._unit(tmp_path, HOG, 2 * 1024 ** 3)
        p = self._run(tmp_path, python)
        assert "escaped" in p.stdout, p.stderr
        assert p.returncode == 0

    def test_the_runner_gets_the_units_environment_and_reports(
            self, tmp_path, python):
        self._unit(tmp_path, "@echo %MARKER%\r\n"
                   "@powershell -NoProfile -Command Start-Sleep 2\r\n",
                   256 * 1024 * 1024)
        p = self._run(tmp_path, python)
        assert "from-the-unit-file" in p.stdout
        report = json.loads((tmp_path / "logs" / "telemetry.json").read_text())
        assert report["mem_used_bytes"] > 0

    def test_a_drain_lets_the_job_finish_and_the_host_exit(self, tmp_path,
                                                            python):
        """OPEN-7 on Windows: the runtime writes drain.request, the job host
        sends the runner a Ctrl+Break, the runner finishes and exits cleanly,
        and so does the host - without a kill, and without cmd.exe's
        "Terminate batch job?" holding it up."""
        worker = textwrap.dedent("""\
            import signal, sys, time
            def done(*a):
                print('finishing the job', flush=True)
                time.sleep(1)
                sys.exit(0)
            signal.signal(signal.SIGBREAK, done)
            print('working', flush=True)
            # Short sleeps: on Windows a Ctrl+Break does not cut a long
            # time.sleep short, so the handler would wait for it to end.
            for _ in range(600):
                time.sleep(0.1)
            print('never', flush=True)
            """)
        (tmp_path / "worker.py").write_text(worker)
        self._unit(tmp_path, f'@"{python}" "{tmp_path / "worker.py"}"\r\n'
                             '@exit /b %ERRORLEVEL%\r\n', 256 * 1024 ** 2)
        import threading
        import time as _time
        threading.Timer(3.0, lambda: (tmp_path / "reg" / "drain.request")
                        .write_text("drain")).start()
        started = _time.monotonic()
        p = self._run(tmp_path, python)
        took = _time.monotonic() - started
        assert "finishing the job" in p.stdout, (p.stdout, p.stderr)
        assert "never" not in p.stdout
        assert p.returncode == 0, (p.returncode, p.stderr)
        assert took < 30, "the host exited when the runner did"

    @pytest.mark.skipif(not _is_packaged(sys.executable),
                        reason="needs the Store Python to show the refusal")
    def test_a_packaged_python_is_refused_before_anything_runs(self,
                                                               tmp_path):
        """Its children leave every job, so it must not host one."""
        self._unit(tmp_path, "@echo ran > \"%~dp0ran.txt\"\r\n", 128 * 1024 ** 2)
        p = self._run(tmp_path, sys.executable)
        assert p.returncode == 3
        assert "packaged" in p.stderr
        assert not (tmp_path / "reg" / "ran.txt").exists()


class TestWhatTheFirstRealWorkerTaught:
    """Two faults the fake could not show until it was made to answer as
    NSSM really does, found when the controller made its first Windows unit
    on this host on 2026-09-19."""

    def test_the_service_is_made_before_the_tree_is_locked_to_it(
            self, host, runtime):
        """Windows maps a service's virtual-account SID to an account only
        once the service exists; icacls on it before that answers "No
        mapping between account names and security IDs was done", which is
        how the first create failed."""
        runtime.create(RID, {"image": TEMPLATE})
        order = [a[0] for a in host.calls if a[0] in (TOOLS["nssm"],
                                                      TOOLS["icacls"])]
        installs = [i for i, a in enumerate(host.calls)
                    if a[0] == TOOLS["nssm"] and a[1] == "install"]
        acls = [i for i, a in enumerate(host.calls)
                if a[0] == TOOLS["icacls"]]
        assert order and installs and acls
        assert installs[0] < acls[0], "the ACL needs the service to exist"
        starts = [i for i, a in enumerate(host.calls)
                  if a[0] == TOOLS["nssm"] and a[1] == "start"]
        assert acls[0] < starts[0], "nothing runs while the tree is open"

    def test_removing_a_service_that_was_never_made_is_a_no_op(self,
                                                               runtime):
        """NSSM says "Can't open service!" in UTF-16, which read as text
        carries a NUL between every character and matched nothing. The
        removal then failed - and it is the compensation that cleans up
        after a create that failed."""
        runtime.remove(RID, keep_data=False)        # nothing exists at all

    def test_nssm_answers_are_read_as_text(self, runtime, host):
        runtime.create(RID, {"image": TEMPLATE})
        raw = host._nssm(["status", naming.unit_name(RID)], None)
        assert "\x00" in _utf16ish(raw[1]), "the fake answers as NSSM does"
        assert runtime.status(RID)["running"] is True


class TestWhatThisWorkerCanBuildFrom:
    """A worker that makes units from templates on its own disk can only
    make the ones it has. Saying so is what stops a fleet from offering
    `+ Add runner` for a cell whose creation could only fail here - which is
    what GitHub on Windows did, with one Forgejo template installed and
    nothing for GitHub (2026-09-20).
    """

    def test_it_says_it_builds_from_templates(self, runtime):
        assert runtime.capabilities()["builds_from"] == "template"

    def test_it_lists_the_templates_it_has(self, runtime, host):
        host.makedirs(ntpath.join(TOOLS["templates"],
                                  "forgejo-runner-v13.1.0-windows"))
        host.makedirs(ntpath.join(TOOLS["templates"],
                                  "actions-runner-v2.336.0"))
        listed = runtime.capabilities()["templates"]
        assert {"actions-runner-v2.336.0",
                "forgejo-runner-v13.1.0-windows"} <= set(listed)
        assert listed == sorted(listed)

    def test_no_templates_is_an_empty_list_not_a_missing_key(self, host):
        """A worker with nothing installed says so, which is what makes a
        cell unavailable rather than merely unknown."""
        bare = WindowsProcessRuntime(run=host, fs=host,
                                     tools=dict(TOOLS,
                                                templates=r"D:\none"))
        caps = bare.capabilities()
        assert caps["templates"] == []
        assert caps["builds_from"] == "template"

    def test_a_directory_it_cannot_read_is_no_templates_not_a_crash(
            self, runtime, host):
        host.explode_on_listdir = True
        assert runtime.capabilities()["templates"] == []
