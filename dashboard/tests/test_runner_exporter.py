"""The exporter must answer "unknown" rather than guess.

Two Forgejo runners are not containers on this engine, so the dashboard has no
telemetry for them at all. This exporter runs on BEAST-UNIT, reads the Windows
runner service locally and the macOS VM over SSH, and serves both as JSON.

The parsers below are tested against output captured from the real machines on
2026-09-17, not against invented samples: the Windows runner stamps its log
with a UTC OFFSET ("+02:00") while the containerised Forgejo runners stamp
theirs with "Z", and docker_ops.RE_FORGEJO_TASK only matches the latter. A
parser written from the container format alone would silently never report a
job for the Windows runner.

Every probe returns None when it cannot answer. Zeros would render as a healthy
idle runner, which is exactly the state an unreachable machine must not be
mistaken for.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "exporters"))

import runner_exporter as ex


# Captured from C:\forgejo-runner\runner.err.log on BEAST-UNIT, 2026-09-17.
WINDOWS_LOG = '''\
time="2026-09-16T13:04:48+02:00" level=info msg="task 1120 repo is FiLL/pixel-desk https://data.forgejo.org https://forgejo.phillippepelzer.me"
time="2026-09-16T13:36:14+02:00" level=info msg="task 1126 repo is FiLL/pixel-desk https://data.forgejo.org https://forgejo.phillippepelzer.me"
time="2026-09-16T13:57:54+02:00" level=info msg="task 1130 repo is FiLL/pixel-desk https://data.forgejo.org https://forgejo.phillippepelzer.me"
'''

# Captured from the containerised runners, which stamp UTC with a trailing Z.
CONTAINER_LOG = (
    'time="2026-08-25T14:38:55Z" level=info '
    'msg="task 830 repo is FiLL/q https://data.forgejo.org"\n')

# `docker stats --no-stream` on macos-runner, 2026-09-17.
MACOS_STATS = "macos-sequoia\t110.72%\t12.52GiB / 23.47GiB"

# `df -P -k /` on macos-runner, same sweep.
MACOS_DF = ("/dev/mapper/ubuntu--vg-ubuntu--lv   305893952 109355904 "
            "182997532      38% /")


class TestJobFromLog:
    def test_reads_the_windows_offset_stamp(self):
        """The regression this parser exists for: +02:00, not Z."""
        job = ex.job_from_log(WINDOWS_LOG)
        assert job == "task 1130 repo is FiLL/pixel-desk"

    def test_reads_the_container_z_stamp_too(self):
        """Both fleets must report a job the same way."""
        assert ex.job_from_log(CONTAINER_LOG) == "task 830 repo is FiLL/q"

    def test_takes_the_last_task_not_the_first(self):
        assert "1130" in ex.job_from_log(WINDOWS_LOG)

    def test_no_task_lines_is_empty_not_none(self):
        """Empty means "running nothing"; None would mean "could not ask"."""
        assert ex.job_from_log("time=... level=info msg=\"starting\"\n") == ""

    def test_unreadable_log_is_empty(self):
        assert ex.job_from_log("") == ""


class TestDockerStats:
    def test_parses_cpu_and_memory(self):
        s = ex.parse_docker_stats(MACOS_STATS)
        assert s["cpu_percent"] == 110.72
        assert s["mem_used_bytes"] == int(12.52 * 1024 ** 3)
        assert s["mem_limit_bytes"] == int(23.47 * 1024 ** 3)

    def test_garbage_is_none_not_zero(self):
        """A confident 0% would read as an idle, healthy runner."""
        assert ex.parse_docker_stats("something went wrong") is None
        assert ex.parse_docker_stats("") is None


class TestDf:
    def test_parses_the_root_filesystem(self):
        d = ex.parse_df(MACOS_DF)
        assert d["total_bytes"] == 305893952 * 1024
        assert d["used_bytes"] == 109355904 * 1024
        assert d["percent"] == 38

    def test_garbage_is_none(self):
        assert ex.parse_df("df: /: No such file or directory") is None
        assert ex.parse_df("") is None


class TestPayload:
    def test_a_failed_probe_becomes_none_not_a_zeroed_runner(self):
        """The whole point: absent telemetry must be absent, not fabricated."""
        payload = ex.build_payload({"a": lambda: None})
        assert payload["runners"]["a"] is None

    def test_one_failing_probe_does_not_lose_the_other(self):
        def boom():
            raise OSError("ssh: connect to host ... timed out")

        payload = ex.build_payload({"a": boom, "b": lambda: {"cpu_percent": 1.0}})
        assert payload["runners"]["a"] is None
        assert payload["runners"]["b"]["cpu_percent"] == 1.0

    def test_payload_is_stamped(self):
        payload = ex.build_payload({})
        assert payload["generated"].endswith("Z")


class TestCoreCount:
    """CPU needs a denominator or the number misleads.

    `docker stats` reports CPU as a percentage of ONE core, so the macOS
    container's 113% is healthy on an 8-core VM and alarming without that
    context. Every other card on the page already shows "x / n cores".
    """

    MACOS_OUT = (
        "macos-sequoia\t110.72%\t12.52GiB / 23.47GiB\n"
        "===DF===\n"
        "/dev/mapper/ubuntu--vg-ubuntu--lv 305893952 109355904 182997532 38% /\n"
        "===CPU===\n8\n")

    def test_macos_probe_reports_the_vm_core_count(self, monkeypatch):
        monkeypatch.setattr(ex, "_run", lambda *a, **k: self.MACOS_OUT)
        got = ex.probe_macos_vm("h", "u", "k", "c", ssh="/usr/bin/ssh")
        assert got["cpu_cores"] == 8
        assert got["cpu_percent"] == 110.72
        assert got["disk"]["percent"] == 38

    def test_a_missing_core_count_is_none_not_zero(self, monkeypatch):
        """Zero cores would render as a divide-by-zero or "of 0 cores"."""
        out = self.MACOS_OUT.replace("===CPU===\n8\n", "===CPU===\n")
        monkeypatch.setattr(ex, "_run", lambda *a, **k: out)
        got = ex.probe_macos_vm("h", "u", "k", "c", ssh="/usr/bin/ssh")
        assert got["cpu_cores"] is None

    def test_unreachable_ssh_is_none_for_the_whole_probe(self, monkeypatch):
        monkeypatch.setattr(ex, "_run", lambda *a, **k: None)
        assert ex.probe_macos_vm("h", "u", "k", "c", ssh="/usr/bin/ssh") is None


class TestSshResolution:
    """The macOS probe silently returned None because ssh was not on PATH.

    Windows ships OpenSSH in System32 without adding it to PATH, so
    shutil.which("ssh") finds nothing. The exporter still answered 200 and
    still listed the runner - with null - so it looked healthy while half of
    what it exists for was missing. Resolution is explicit now.
    """

    def _serve(self):
        import importlib
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(
                _os.path.abspath(__file__)))), "exporters", "windows"))
        return importlib.import_module("serve")

    def test_an_explicit_path_is_used_when_it_exists(self, monkeypatch, tmp_path):
        serve = self._serve()
        fake = tmp_path / "ssh.exe"
        fake.write_text("")
        monkeypatch.setenv("EXPORTER_SSH", str(fake))
        assert serve.resolve_ssh() == str(fake)

    def test_a_configured_path_that_is_missing_is_none_not_a_guess(
            self, monkeypatch):
        """Silently falling back would hide a typo in the service config."""
        serve = self._serve()
        monkeypatch.setenv("EXPORTER_SSH", r"C:\nope\ssh.exe")
        assert serve.resolve_ssh() is None

    def test_path_lookup_wins_when_nothing_is_configured(self, monkeypatch):
        serve = self._serve()
        monkeypatch.delenv("EXPORTER_SSH", raising=False)
        monkeypatch.setattr(serve.shutil, "which", lambda n: "/usr/bin/ssh")
        assert serve.resolve_ssh() == "/usr/bin/ssh"

    def test_falls_back_to_the_system32_location(self, monkeypatch):
        """The regression: nothing on PATH must not mean no ssh at all."""
        serve = self._serve()
        monkeypatch.delenv("EXPORTER_SSH", raising=False)
        monkeypatch.setattr(serve.shutil, "which", lambda n: None)
        monkeypatch.setattr(serve.os.path, "exists",
                            lambda p: p == serve._WINDOWS_SSH)
        assert serve.resolve_ssh() == serve._WINDOWS_SSH


class TestPowerShellResolution:
    """Running as LocalSystem, a bare "powershell.exe" was not found.

    The service's PATH is not the interactive one, so the Windows probe failed
    on every sweep with WinError 2 while the exporter still answered 200. Same
    class of bug as the missing ssh, and the same fix: resolve absolutely.
    """

    def _serve(self):
        import importlib
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(
                _os.path.abspath(__file__)))), "exporters", "windows"))
        return importlib.import_module("serve")

    def test_prefers_the_system32_location(self, monkeypatch):
        serve = self._serve()
        monkeypatch.delenv("EXPORTER_POWERSHELL", raising=False)
        monkeypatch.setattr(serve.os.path, "exists",
                            lambda p: p == serve._WINDOWS_PS)
        assert serve.resolve_powershell() == serve._WINDOWS_PS

    def test_a_configured_path_that_is_missing_is_none(self, monkeypatch):
        serve = self._serve()
        monkeypatch.setenv("EXPORTER_POWERSHELL", r"C:\nope\powershell.exe")
        assert serve.resolve_powershell() is None


class TestFailuresAreExplained:
    """A probe that fails silently is the worst outcome: the card reads
    "unknown", which is correct, and indistinguishable from a machine that is
    genuinely down. Both real failures here - no ssh on PATH, no powershell
    for LocalSystem - hid behind a 200 with nulls."""

    def test_a_nonzero_exit_says_so_on_stderr(self, capsys, monkeypatch):
        class _P:
            returncode = 255
            stdout = ""
            stderr = "Permissions for key are too open."

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _P())
        assert ex._run(["x"], 5, "macos") is None
        assert "too open" in capsys.readouterr().err

    def test_a_raising_probe_names_the_exception(self, capsys):
        ex.build_payload({"a": lambda: 1 / 0})
        assert "ZeroDivisionError" in capsys.readouterr().err


class TestWindowsProbeMeasuresTheTree:
    """The daemon alone is not the runner's footprint.

    forgejo-runner.exe spawns a job's real work - git, docker, compilers - as
    child processes. Measuring only the daemon reported ~10 MB and 0% however
    hard the machine was working, so the card looked broken beside the macOS
    one, which measures a whole container. And a service has no cgroup
    ceiling, so without a denominator the memory row read a bare "0.01 GB".
    """

    PAYLOAD = ('{"cpu_percent":412.5,"mem_used":8589934592,'
               '"mem_limit":274751803392,"process_count":37,'
               '"uptime_seconds":428158,"disk_total":479423455232,'
               '"disk_free":141285547032,"state":"Running","cpu_cores":56}')

    def _probe(self, monkeypatch, stdout):
        import importlib
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(
                _os.path.abspath(__file__)))), "exporters", "windows"))
        serve = importlib.import_module("serve")

        class _P:
            returncode = 0
            stderr = ""

        _P.stdout = stdout
        monkeypatch.setattr(serve, "resolve_powershell", lambda: "pwsh")
        monkeypatch.setattr(serve.subprocess, "run", lambda *a, **k: _P())
        return serve.probe_windows_service("nope.log", "C:")

    def test_the_whole_tree_is_reported(self, monkeypatch):
        got = self._probe(monkeypatch, self.PAYLOAD)
        assert got["cpu_percent"] == 412.5
        assert got["process_count"] == 37
        assert got["mem_used_bytes"] == 8589934592

    def test_memory_has_a_denominator(self, monkeypatch):
        """The machine, since the service has no ceiling of its own."""
        got = self._probe(monkeypatch, self.PAYLOAD)
        assert got["mem_limit_bytes"] == 274751803392

    def test_an_absent_limit_stays_none_rather_than_zero(self, monkeypatch):
        """0 would make the card divide by it."""
        got = self._probe(monkeypatch, self.PAYLOAD.replace(
            '"mem_limit":274751803392', '"mem_limit":0'))
        assert got["mem_limit_bytes"] is None

    def test_an_unreadable_log_still_yields_the_rest(self, monkeypatch):
        """A missing log must cost the job name, not the whole probe."""
        got = self._probe(monkeypatch, self.PAYLOAD)
        assert got["job"] == ""
        assert got["state"] == "Running"
