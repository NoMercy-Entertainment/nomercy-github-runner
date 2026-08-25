"""Forgejo's own status decides busy/idle; the log only names the job.

The forgejo-runner daemon logs a task starting and never logs it finishing, so
the GitHub approach - take the last job event in the tail - would pin every
Forgejo runner busy for ever after its first job. The API is asked instead.

The unknown-is-not-idle rule carries over unchanged. prune() and the drain
watcher both act on is_idle(), and a wrong "idle" deletes layers a running
build needs.
"""
import json
import time

import pytest

import docker_ops
import forgejo_api
import providers


@pytest.fixture(autouse=True)
def _clear_forge_status_cache():
    """collect()'s fleet-wide status answer is cached for a few seconds in a
    module global. Left warm, it makes the call-counting tests below depend
    on the order they run in - and on how fast the suite is."""
    docker_ops._forge_status_cache = None
    yield
    docker_ops._forge_status_cache = None

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


def test_an_unrecognised_status_is_unknown_not_idle(monkeypatch):
    """A status word this code does not know is not a licence to prune.

    Only "active", "idle" and "offline" are documented. Anything else -
    a future Forgejo release, a typo, a bug upstream - must not fall through
    to "idle" by default: that is exactly the wrong direction, since idle is
    what tells prune and the drain watcher it is safe to act.
    """
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    state, _ = docker_ops._job_state(
        "forgejo-runner-1", providers.FORGEJO, RUNNER_FILE,
        {UUID: "starting"})
    assert state == "unknown"
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, {UUID: "starting"}) is False


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
            # The widened shape - forgejo_api.Forgejo.runner_statuses() now
            # answers with the full records, not a {uuid: status} reduction.
            return [{"uuid": UUID, "status": "idle"}]

    monkeypatch.setattr(docker_ops, "_docker", _fake_docker())
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: _Forge())
    assert docker_ops.is_idle(
        "forgejo-runner-1", providers.FORGEJO, env={"x": "y"}) is True


def test_the_github_path_still_works_with_one_argument(monkeypatch):
    """Guards the default that keeps test_job_state.py green."""
    monkeypatch.setattr(docker_ops, "_docker", lambda *a, **k: (
        True, "2026-08-20 14:23:54Z: Listening for Jobs\n", ""))
    assert docker_ops._job_state("github-runner-3") == ("idle", "")


# --------------------------------------------------------------------------
# collect() must ask Forgejo once for the whole fleet, never per runner, and
# never at all when there is no Forgejo runner to ask about. A hand-trace
# confirmed this once; these pin it so a future edit that moves the call
# inside the per-runner loop fails loudly instead of just costing more API
# calls than the 5s poll interval can afford.
# --------------------------------------------------------------------------

def _docker_stub(ps_output):
    """Enough of `docker` to get collect() through a fleet of stopped
    containers: the list, an empty `stats`, and a non-"Up" status for every
    runner so collect() takes the early "stopped" branch and never needs
    _runner_file/_job_state/_inner_df mocked too."""
    def call(*args, **kwargs):
        if args[:3] == ("ps", "-a", "--format"):
            return (True, ps_output, "")
        if args and args[0] == "stats":
            return (True, "", "")
        if args[:3] == ("ps", "-a", "--filter"):
            return (True, "Exited (0) 2 hours ago", "")
        return (True, "", "")
    return call


def test_collect_asks_forgejo_once_for_a_mixed_fleet(monkeypatch):
    ps_output = (
        "github-runner-1\t\n"
        "forgejo-runner-1\tforgejo\n"
        "forgejo-runner-2\tforgejo\n"
    )
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub(ps_output))

    client_calls = []
    status_calls = []

    class _Forge:
        def runner_statuses(self):
            status_calls.append(1)
            return []

    def fake_forge_client(env):
        client_calls.append(1)
        return _Forge()

    monkeypatch.setattr(providers.FORGEJO, "forge_client", fake_forge_client)
    docker_ops.collect()
    assert len(client_calls) == 1
    assert len(status_calls) == 1


def test_collect_never_asks_forgejo_for_a_github_only_fleet(monkeypatch):
    ps_output = "github-runner-1\t\ngithub-runner-2\t\n"
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub(ps_output))

    client_calls = []

    def fake_forge_client(env):
        client_calls.append(1)
        return None

    monkeypatch.setattr(providers.FORGEJO, "forge_client", fake_forge_client)
    docker_ops.collect()
    assert client_calls == [], "a GitHub-only fleet must never even build a Forgejo client"


# --------------------------------------------------------------------------
# the fleet-wide status call is cached
#
# collect() runs every 5s on a thread shared with the GitHub fleet, and this
# call has a 20s HTTP timeout. Uncached it was ~17,000 calls a day, and a slow
# or unreachable Forgejo stalled telemetry for the GITHUB runners too - the
# cross-engine coupling the whole isolation design exists to prevent, reached
# from the dashboard's side instead of Docker's.
# --------------------------------------------------------------------------

class _CountingForge:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def runner_statuses(self):
        self.calls += 1
        return self.answers.pop(0) if self.answers else None


def _wire_forge(monkeypatch, forge):
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    return forge


# Answers are the widened shape forgejo_api.Forgejo.runner_statuses() now
# returns - full records, not a {uuid: status} reduction. _forge_statuses()
# still reduces them to that map itself, so the assertions below - the
# public contract every existing caller of _forge_statuses() depends on -
# are unchanged.
def test_the_fleet_status_is_fetched_once_per_window(monkeypatch):
    forge = _wire_forge(monkeypatch, _CountingForge(
        [[{"uuid": UUID, "status": "idle"}],
         [{"uuid": UUID, "status": "active"}]]))
    assert docker_ops._forge_statuses({}) == {UUID: "idle"}
    assert docker_ops._forge_statuses({}) == {UUID: "idle"}
    assert docker_ops._forge_statuses({}) == {UUID: "idle"}
    assert forge.calls == 1


def test_the_window_expires(monkeypatch):
    forge = _wire_forge(monkeypatch, _CountingForge(
        [[{"uuid": UUID, "status": "idle"}],
         [{"uuid": UUID, "status": "active"}]]))
    assert docker_ops._forge_statuses({}) == {UUID: "idle"}
    # Rather than sleeping: move the stored deadline into the past.
    deadline, value = docker_ops._forge_status_cache
    docker_ops._forge_status_cache = (deadline - 3600, value)
    assert docker_ops._forge_statuses({}) == {UUID: "active"}
    assert forge.calls == 2


def test_a_failed_call_is_never_replayed_as_an_answer(monkeypatch):
    """The hazard host_info() documents, and the reason this is not a plain
    memoise. A failure must answer None - "we could not ask", which every
    caller turns into "unknown" - and must NOT hand back the last good map,
    which would report a runner idle on the strength of a call that failed.
    prune() and the drain watcher act on that answer."""
    forge = _wire_forge(monkeypatch, _CountingForge(
        [[{"uuid": UUID, "status": "idle"}], None]))
    assert docker_ops._forge_statuses({}) == {UUID: "idle"}
    deadline, value = docker_ops._forge_status_cache
    docker_ops._forge_status_cache = (deadline - 3600, value)

    assert docker_ops._forge_statuses({}) is None
    # And the stale good map is gone, not sitting in the cache waiting to be
    # served for the rest of the window.
    assert docker_ops._forge_statuses({}) is None
    assert forge.calls == 2, "the failed attempt is rate-limited, not retried"


def test_no_forgejo_client_answers_unknown_without_a_call(monkeypatch):
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)
    assert docker_ops._forge_statuses({}) is None


def test_a_slow_failure_is_not_retried_before_it_could_have_changed(
        monkeypatch):
    """Reproduces the bug: the cache deadline used to be taken from a `now`
    read BEFORE the call. A call slower than _FORGE_STATUS_TTL then stored a
    deadline already in the past - every subsequent sweep missed the cache
    and paid the full HTTP timeout again, stalling the shared collector
    thread (and with it the GITHUB fleet's telemetry) on every poll.

    time.monotonic is monkeypatched so the stub can stand in for a call that
    blocks for the real urllib timeout without an actual sleep - the call
    itself advances the fake clock by forgejo_api.REQUEST_TIMEOUT, exactly as
    a real request against a dead host would before urlopen gives up. Both
    the deadline check and the deadline write inside _forge_statuses still
    run for real; only the passage of time during the network call is
    simulated.
    """
    clock = {"t": time.monotonic()}
    monkeypatch.setattr(docker_ops.time, "monotonic", lambda: clock["t"])

    class _SlowDeadForge:
        def __init__(self):
            self.calls = 0

        def runner_statuses(self):
            self.calls += 1
            clock["t"] += forgejo_api.REQUEST_TIMEOUT
            return None

    forge = _wire_forge(monkeypatch, _SlowDeadForge())

    assert docker_ops._forge_statuses({}) is None
    assert forge.calls == 1

    # A later collector sweep, well within a normal 5s poll cadence, must
    # still be served from cache - not pay for another full timeout.
    clock["t"] += 5
    assert docker_ops._forge_statuses({}) is None
    assert forge.calls == 1, (
        "the failed call's cache window must be counted from when the call "
        "FINISHED, not when it started - a stale pre-call deadline let a "
        "slow failure be retried immediately")
