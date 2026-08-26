"""A killed job must not leave the runner showing BUSY for ever.

_job_state takes the last job event in the log tail. A job that is SIGTERMed
never writes its "completed" line, so the dangling "Running job:" stays the
last event and the runner reads as busy indefinitely - on the grid, and to
is_idle(), which then refuses prune and drain with "busy running a job".

Same rule as the history reconciler: a job that began before its container's
current start cannot still be running.
"""
import docker_ops

# Verbatim from `docker logs github-runner-7`, around the SIGTERM that
# orphaned build-base / docker-build on 2026-08-20.
KILLED_MID_JOB = (
    "2026-08-20 13:27:33Z: Running job: build-base / docker-build\n"
    "Received SIGTERM - shutting down runner nomercy-g5bxt...\n"
    "√ Removed .runner\n"
    "Runner nomercy-pkrxh registered.\n"
    "2026-08-20 14:23:54Z: Listening for Jobs\n"
)

RUNNING_NOW = (
    "2026-08-20 14:23:54Z: Listening for Jobs\n"
    "2026-08-20 15:02:11Z: Running job: build-base / docker-build\n"
)


def _fake_docker(logs, started="2026-08-20T14:23:37Z", inspect_ok=True):
    def call(*args, **kwargs):
        if args[0] == "inspect":
            return (inspect_ok, started + "\n" if inspect_ok else "", "")
        return (True, logs, "")
    return call


def test_a_job_the_container_outlived_reads_as_idle(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker(KILLED_MID_JOB))
    assert docker_ops._job_state("github-runner-7") == ("idle", "")


def test_a_job_started_since_the_container_did_reads_as_busy(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker(RUNNING_NOW))
    state, job = docker_ops._job_state("github-runner-7")
    assert (state, job) == ("busy", "build-base / docker-build")


def test_is_idle_stops_refusing_prune_on_a_dead_job(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker(KILLED_MID_JOB))
    assert docker_ops.is_idle("github-runner-7") is True


def test_an_unreadable_container_start_keeps_the_cautious_answer(monkeypatch):
    """Never downgrade to idle on a guess: prune acts on this."""
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker(KILLED_MID_JOB, inspect_ok=False))
    assert docker_ops._job_state("github-runner-7")[0] == "busy"


def test_an_idle_runner_costs_no_inspect(monkeypatch):
    """The start is only ever read when the answer would otherwise be busy."""
    seen = []

    def call(*args, **kwargs):
        seen.append(args[0])
        return (True, "2026-08-20 14:23:54Z: Listening for Jobs\n", "")

    monkeypatch.setattr(docker_ops, "_docker", call)
    assert docker_ops._job_state("github-runner-3") == ("idle", "")
    assert "inspect" not in seen
