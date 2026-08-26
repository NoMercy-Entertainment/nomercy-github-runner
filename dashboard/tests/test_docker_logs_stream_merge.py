"""forgejo-runner logs to stderr; the GitHub Actions runner logs to stdout.

Every pre-existing test in this suite stubs docker_ops._docker directly and
hands the parsers ready-made text, so none of them can see the stdout/stderr
split - they never exercise subprocess.run at all. That is exactly how this
bug shipped: measured against a live Forgejo runner that had just completed
real jobs, `docker logs --tail 200` returned 3 lines on stdout (shell echo)
and 10 on stderr (the runner's actual log). Every log-reading call site read
stdout only, so the Forgejo history path parsed 0 job starts where reading
both streams would have parsed 2 - the whole Forgejo history table stayed
empty in production, and the busy card showed no job name.

These tests stub subprocess.run itself, one level below _docker, so they can
tell a real merged read from a real stdout-only read - the thing every other
test in this suite structurally cannot do.
"""
import subprocess

import docker_ops

# What a Forgejo runner container's two streams actually look like, captured
# in shape (not verbatim) from the live measurement described above: a
# handful of shell/entrypoint lines on stdout, and the runner's real log -
# including the "task N repo is ..." lines both the busy-card label and the
# history reconciler key on - on stderr.
STDOUT_ECHO = (
    "+ exec forgejo-runner daemon --config /etc/forgejo-runner/config.yml\n"
    "using config file /etc/forgejo-runner/config.yml\n"
)
STDERR_RUNNER_LOG = (
    'time="2026-08-25T14:33:15Z" level=info msg="task 855 repo is FiLL/p '
    'https://data.forgejo.org https://forgejo.example"\n'
    'time="2026-08-25T14:38:55Z" level=info msg="task 856 repo is FiLL/q '
    'https://data.forgejo.org https://forgejo.example"\n'
)


class _FakeCompleted:
    def __init__(self, stdout, stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _real_docker_logs_behaviour(args, **kwargs):
    """Stands in for the real `docker logs` process: two genuinely separate
    OS-level streams. Mirrors what subprocess.run actually does - when the
    caller merges stderr onto stdout (stderr=subprocess.STDOUT) the two
    arrive interleaved on `.stdout`; otherwise they land in `.stdout` and
    `.stderr` separately, exactly like a real `docker logs` process would.
    """
    merged = kwargs.get("stderr") is subprocess.STDOUT
    if merged:
        return _FakeCompleted(STDOUT_ECHO + STDERR_RUNNER_LOG)
    return _FakeCompleted(STDOUT_ECHO, STDERR_RUNNER_LOG)


def test_docker_logs_runs_the_command_with_stderr_redirected_to_stdout(
        monkeypatch):
    """The mechanism itself: _docker_logs must ask the OS to interleave the
    streams (stderr=subprocess.STDOUT), not capture them separately and glue
    them together afterwards - capture_output=True buffers the two apart, and
    concatenating after the fact would reorder a real interleaved log."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted("merged output")

    monkeypatch.setattr(docker_ops.subprocess, "run", fake_run)
    ok, out, err = docker_ops._docker_logs(
        "logs", "--tail", "200", "forgejo-runner-1", timeout=10)

    assert captured["kwargs"].get("stderr") is subprocess.STDOUT
    # capture_output=True is NOT used here - it sets stdout AND stderr to
    # PIPE, which is the separate-buffers shape this helper exists to avoid.
    assert "capture_output" not in captured["kwargs"]
    assert ok is True
    assert out == "merged output"
    assert err == ""


def test_forgejo_current_job_finds_the_task_when_it_only_lives_in_stderr(
        monkeypatch):
    """Drives a fake process whose real output arrives on stderr, exactly
    like the live forgejo-runner measurement. Against the un-merged code path
    (plain _docker(), stdout only) this sees just the 2-line shell echo and
    reports no job - against the merged helper it finds task 856."""
    monkeypatch.setattr(docker_ops.subprocess, "run",
                        _real_docker_logs_behaviour)
    job = docker_ops._forgejo_current_job("forgejo-runner-1")
    assert job == "task 856 - FiLL/q"


def test_forgejo_job_state_reports_busy_with_the_task_from_stderr(
        monkeypatch):
    """The busy card's job label goes through _job_state -> _forgejo_current_job
    for an active Forgejo runner. This is the path that showed "busy" with no
    job name in production."""
    import providers

    monkeypatch.setattr(docker_ops.subprocess, "run",
                        _real_docker_logs_behaviour)
    uuid = "aa11bb22-0000-4757-8626-000000000000"
    state, job = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO,
        {"uuid": uuid}, {uuid: "active"})
    assert state == "busy"
    assert job == "task 856 - FiLL/q"


def test_logs_since_carries_stderr_lines_into_history_parsing(monkeypatch):
    """logs_since() feeds history.parse_forgejo_events() directly (see
    app.py's collector loop and its startup backfill). If it only carried
    stdout, the whole Forgejo history path parses zero starts - which is
    exactly what happened in production: thousands of GitHub rows, zero
    Forgejo ones, after two real Forgejo jobs had completed."""
    import history

    monkeypatch.setattr(docker_ops.subprocess, "run",
                        _real_docker_logs_behaviour)
    text = docker_ops.logs_since("forgejo-runner-1", 45)
    events = history.parse_forgejo_events(text)
    assert events == [
        ("start", "2026-08-25T14:33:15Z", "FiLL/p", 855),
        ("start", "2026-08-25T14:38:55Z", "FiLL/q", 856),
    ]


def test_plain_docker_still_captures_streams_separately(monkeypatch):
    """Regression guard: the merge is specific to _docker_logs(). Every other
    _docker() caller (docker rm, exec, inspect, ...) must keep getting stdout
    and stderr apart - e.g. `docker rm` puts the removed name on stdout and a
    real error on stderr, and callers tell them apart."""
    monkeypatch.setattr(docker_ops.subprocess, "run",
                        _real_docker_logs_behaviour)
    ok, out, err = docker_ops._docker("logs", "--tail", "200",
                                      "forgejo-runner-1", timeout=10)
    assert ok is True
    assert out == STDOUT_ECHO.strip()
    assert err == STDERR_RUNNER_LOG.strip()
