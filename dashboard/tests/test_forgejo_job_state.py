"""Forgejo's own status decides busy/idle; the log only names the job.

The forgejo-runner daemon logs a task starting and never logs it finishing, so
the GitHub approach - take the last job event in the tail - would pin every
Forgejo runner busy for ever after its first job. The API is asked instead.

The unknown-is-not-idle rule carries over unchanged. prune() and the drain
watcher both act on is_idle(), and a wrong "idle" deletes layers a running
build needs.
"""
import json

import docker_ops
import providers

RUNNER_FILE = {
    "id": 4,
    "uuid": "aa11bb22-0000-4757-8626-000000000000",
    "name": "nomercy-forgejo-1",
    "address": "https://forgejo.example",
}

LOG_TAIL = (
    'time="2026-08-25T14:33:15Z" level=info msg="task 829 repo is FiLL/p '
    'https://data.forgejo.org https://forgejo.example"\n'
    'time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/q '
    'https://data.forgejo.org https://forgejo.example"\n'
)

UUID = RUNNER_FILE["uuid"]


def _fake_docker(logs=LOG_TAIL, runner_file=RUNNER_FILE):
    """Answers both docker calls _job_state/is_idle can make.

    `cat <registration_path>` (used by _runner_file to find the uuid) gets the
    registration JSON; every other call - `docker logs` - gets the log tail.
    A single fixed response for both would leave _runner_file unable to parse
    a uuid out of the log text, which made is_idle() see every Forgejo runner
    as unregistered no matter what the forge said.
    """
    def call(*args, **kwargs):
        if "cat" in args:
            return (True, json.dumps(runner_file), "")
        return (True, logs, "")
    return call


def test_active_reads_as_busy_and_names_the_last_task(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, job = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, {UUID: "active"})
    assert state == "busy"
    assert job == "task 830 - FiLL/q"


def test_idle_reads_as_idle_even_with_a_task_line_in_the_log(monkeypatch):
    """The log's last line is always a start. Only the API can say it ended."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    assert docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE,
        {UUID: "idle"}) == ("idle", "")


def test_an_unreachable_api_is_unknown_not_idle(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, None)
    assert state == "unknown"


def test_a_runner_forgejo_has_never_heard_of_is_unknown(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE, {})
    assert state == "unknown"


def test_an_unregistered_container_is_unknown(monkeypatch):
    """No .runner file yet - it is still registering. Not idle."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, {}, {UUID: "idle"})
    assert state == "unknown"


def test_is_idle_refuses_prune_when_forgejo_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, None) is False


def test_is_idle_allows_prune_on_a_confirmed_idle(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, {UUID: "idle"}) is True


def test_is_idle_asks_the_forge_itself_when_nobody_handed_it_a_status(
        monkeypatch):
    """The drain watcher and prune() hold no status of their own. If is_idle
    did not fetch one, every Forgejo runner would read unknown - and prune and
    drain would be refused for the Forgejo fleet permanently."""
    class _Forge:
        def runner_statuses(self):
            return {UUID: "idle"}

    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: _Forge())
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, env={"x": "y"}) is True


def test_the_github_path_still_works_with_one_argument(monkeypatch):
    """Guards the default that keeps test_job_state.py green."""
    monkeypatch.setattr(docker_ops, "_docker", lambda *a, **k: (
        True, "2026-08-20 14:23:54Z: Listening for Jobs\n", ""))
    assert docker_ops._job_state("github-runner-3") == ("idle", "")
