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
