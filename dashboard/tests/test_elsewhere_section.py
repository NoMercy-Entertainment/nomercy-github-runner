"""Forgejo runners the forge knows about that are not containers here.

The owner has Forgejo runners that never appear in docker_ops.list_runners()
at all - a Windows one running as a service, a macOS one inside a nested
Hyper-V VM. Both are real, both are alive, and forgejo_api.runner_statuses()
already answers with them on every sweep alongside the containers this
engine actually runs. This "Elsewhere" section shows them, read-only,
instead of silently discarding them the way collect() used to.

Two invariants matter more than the rendering:
  - matching a forge record to a container is done on uuid, never name -
    Forgejo documents runner names as not unique;
  - these runners must stay unreachable by every action route, because
    "read-only" is the entire point of the feature. The name allowlist
    (providers.valid_name) and list_runner_names() membership are what
    already guarantee that; this file pins it so a later change cannot
    quietly widen either guard to make an Elsewhere card clickable.
"""
import json

import pytest

import docker_ops
import providers


@pytest.fixture(autouse=True)
def _clear_forge_status_cache():
    """Same reasoning as test_forgejo_job_state.py: collect()'s fleet-wide
    Forgejo answer is cached in a module global, and leaving it warm would
    make these tests depend on execution order."""
    docker_ops._forge_status_cache = None
    yield
    docker_ops._forge_status_cache = None


KNOWN_UUID = "aa11bb22-0000-4757-8626-000000000000"
WINDOWS_UUID = "win-service-0001"
MACOS_UUID = "macos-vm-0002"


# --------------------------------------------------------------------------
# docker_ops._elsewhere() in isolation - no docker, no forge, pure function
# --------------------------------------------------------------------------

def test_a_record_with_no_matching_container_uuid_is_elsewhere():
    records = [{"uuid": WINDOWS_UUID, "name": "beaststack-windows-runner",
               "status": "idle", "labels": ["self-hosted", "windows"],
               "version": "6.2.0"}]
    got = docker_ops._elsewhere(records, known_uuids=set())
    assert got == [{"uuid": WINDOWS_UUID, "name": "beaststack-windows-runner",
                    "status": "idle", "labels": "self-hosted, windows",
                    "version": "6.2.0"}]


def test_a_record_whose_uuid_matches_a_container_is_excluded():
    """The uuid, not the name - two records can share a name and must still
    be told apart correctly."""
    records = [{"uuid": KNOWN_UUID, "name": "forgejo-runner-1",
               "status": "idle", "labels": [], "version": "12.0.0"}]
    assert docker_ops._elsewhere(records, known_uuids={KNOWN_UUID}) == []


def test_matching_is_never_by_name():
    """A record sharing a NAME with a known container, but a different uuid,
    still counts as Elsewhere - Forgejo documents runner names as not
    unique, so a name match would wrongly hide a genuinely separate runner."""
    records = [{"uuid": "different-uuid", "name": "forgejo-runner-1",
               "status": "idle", "labels": [], "version": "12.0.0"}]
    got = docker_ops._elsewhere(records, known_uuids={KNOWN_UUID})
    assert len(got) == 1
    assert got[0]["uuid"] == "different-uuid"


def test_a_failed_forge_call_answers_an_empty_list_not_a_stale_one():
    """forge_records is None exactly when runner_statuses() could not be
    asked. The same "unknown answers unknown" discipline _job_state() and
    is_idle() already apply to that sentinel applies here: no cards invented,
    none carried over from a previous good sweep."""
    assert docker_ops._elsewhere(None, known_uuids=set()) == []


def test_labels_are_joined_for_display():
    records = [{"uuid": WINDOWS_UUID, "name": "n", "status": "idle",
               "labels": ["a", "b", "c"], "version": "1"}]
    assert docker_ops._elsewhere(records, set())[0]["labels"] == "a, b, c"


def test_no_labels_is_an_empty_string_not_a_placeholder():
    records = [{"uuid": WINDOWS_UUID, "name": "n", "status": "idle",
               "labels": [], "version": "1"}]
    assert docker_ops._elsewhere(records, set())[0]["labels"] == ""


# --------------------------------------------------------------------------
# collect() end to end: one Forgejo container plus two forge-only runners
# --------------------------------------------------------------------------

REGISTRATION = {"id": 4, "uuid": KNOWN_UUID, "name": "forgejo-runner-1"}

PS_OUTPUT = "forgejo-runner-1\tforgejo\n"

FORGE_RECORDS = [
    {"uuid": KNOWN_UUID, "name": "forgejo-runner-1", "status": "idle",
     "labels": ["ubuntu-latest"], "version": "12.0.0"},
    {"uuid": WINDOWS_UUID, "name": "beaststack-windows-runner",
     "status": "idle", "labels": ["self-hosted", "windows"],
     "version": "6.2.0"},
    {"uuid": MACOS_UUID, "name": "beaststack-macos-runner",
     "status": "offline", "labels": ["self-hosted", "macos"],
     "version": "6.2.0"},
]


def _docker_stub(*, running=True):
    status_line = "Up 2 hours" if running else "Exited (0) 2 hours ago"

    def call(*args, **kwargs):
        if args[:3] == ("ps", "-a", "--format"):
            return (True, PS_OUTPUT, "")
        if args[:3] == ("ps", "-a", "--filter"):
            return (True, status_line, "")
        if args and args[0] == "stats":
            return (True, "", "")
        if "cat" in args:
            return (True, json.dumps(REGISTRATION), "")
        if args and args[0] == "logs":
            return (True, "", "")
        return (True, "", "")
    return call


class _Forge:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def runner_statuses(self):
        self.calls += 1
        return self.records


def test_collect_puts_the_container_backed_runner_only_in_runners(
        monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub())
    forge = _Forge(FORGE_RECORDS)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)

    status = docker_ops.collect({})

    assert [r["name"] for r in status["runners"]] == ["forgejo-runner-1"]
    assert {r["uuid"] for r in status["elsewhere"]} == {WINDOWS_UUID, MACOS_UUID}
    # exactly one API call for the whole sweep, per the existing cache
    assert forge.calls == 1


def test_elsewhere_carries_no_container_only_fields(monkeypatch):
    """CPU/memory/build cache/uptime/job all come from `docker stats` and
    `docker exec` against a container that does not exist for these - they
    must be genuinely absent, not a rendered dash or zero."""
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub())
    forge = _Forge(FORGE_RECORDS)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)

    status = docker_ops.collect({})
    windows = next(r for r in status["elsewhere"] if r["uuid"] == WINDOWS_UUID)

    assert windows == {
        "uuid": WINDOWS_UUID,
        "name": "beaststack-windows-runner",
        "status": "idle",
        "labels": "self-hosted, windows",
        "version": "6.2.0",
    }
    for absent in ("cpu_percent", "mem_used", "mem_limit", "build_cache",
                  "images", "uptime", "job", "state"):
        assert absent not in windows


def test_a_stopped_containers_own_record_is_a_known_gap(monkeypatch):
    """Documents a deliberate scope limit rather than hiding it.

    known_forgejo_uuids is only populated from RUNNING containers (see the
    comment in collect()): a stopped container's registration file sits
    behind `docker exec`, which needs a running container to answer, and
    nothing else in collect() reads a stopped container's file either - its
    "registration" field is a bare "-" for the exact same reason. So while a
    Forgejo container is stopped, its own forge record - if Forgejo still
    reports one - is indistinguishable from a genuine forge-only runner and
    surfaces as a (harmless, read-only, un-actionable) Elsewhere card
    alongside its "stopped" card in `runners`, until it is started again.
    """
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub(running=False))
    forge = _Forge([FORGE_RECORDS[0]])   # only the container's own record
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)

    status = docker_ops.collect({})

    assert status["runners"][0]["state"] == "stopped"
    assert status["runners"][0]["name"] == "forgejo-runner-1"
    assert [e["uuid"] for e in status["elsewhere"]] == [KNOWN_UUID]


def test_collect_never_calls_forgejo_twice_for_one_sweep(monkeypatch):
    """The whole point of sharing the cache: busy/idle AND Elsewhere both
    come out of the one call collect() already made before this feature
    existed."""
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub())
    forge = _Forge(FORGE_RECORDS)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)

    docker_ops.collect({})
    assert forge.calls == 1


def test_an_unreachable_forge_leaves_elsewhere_empty(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _docker_stub())

    class _DeadForge:
        def runner_statuses(self):
            return None

    monkeypatch.setattr(providers.FORGEJO, "forge_client",
                        lambda env: _DeadForge())

    status = docker_ops.collect({})
    assert status["elsewhere"] == []


def test_a_github_only_fleet_never_touches_the_forge_for_elsewhere(
        monkeypatch):
    """Unchanged pre-existing behaviour, still true with Elsewhere added:
    collect() never builds a Forgejo client at all when there is no Forgejo
    container on this engine."""
    def ps(*args, **kwargs):
        if args[:3] == ("ps", "-a", "--format"):
            return (True, "github-runner-1\t\n", "")
        if args[:3] == ("ps", "-a", "--filter"):
            return (True, "Up 1 hour", "")
        if args and args[0] == "stats":
            return (True, "", "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", ps)
    calls = []
    monkeypatch.setattr(providers.FORGEJO, "forge_client",
                        lambda env: calls.append(1) or None)

    status = docker_ops.collect({})
    assert calls == []
    assert status["elsewhere"] == []


# --------------------------------------------------------------------------
# safety: these runners must stay unreachable by every action route
# --------------------------------------------------------------------------

ELSEWHERE_NAME = "beaststack-windows-runner"


def test_the_elsewhere_name_shape_does_not_pass_valid_name():
    """The allowlist every action route is built on. If this ever started
    matching, the tests below would be the only thing standing between an
    Elsewhere card and a clickable Stop button."""
    assert providers.valid_name(ELSEWHERE_NAME) is False


def test_every_body_carrying_action_route_refuses_the_name(client,
                                                            monkeypatch):
    """Refused before docker or the forge is ever consulted - _target()
    checks providers.valid_name() first. No docker/forge stub is installed;
    if the route reached either, this would error or hang instead of
    answering 400."""
    def explode(*a, **k):
        raise AssertionError(
            "an Elsewhere name must be refused before docker is touched")
    monkeypatch.setattr(docker_ops, "_docker", explode)

    for action in ("start", "stop", "restart", "drain", "canceldrain",
                  "remove"):
        r = client.post(f"/api/runner/{action}",
                        json={"name": ELSEWHERE_NAME})
        assert r.status_code == 400, action
        assert "bad runner name" in r.get_json()["error"]


def test_the_name_in_path_prune_route_refuses_it_too(client, monkeypatch):
    def explode(*a, **k):
        raise AssertionError(
            "an Elsewhere name must be refused before docker is touched")
    monkeypatch.setattr(docker_ops, "_docker", explode)

    r = client.post(f"/api/runner/{ELSEWHERE_NAME}/prune")
    assert r.status_code == 400


def test_refusal_does_not_depend_on_the_forge_being_reachable(client,
                                                               monkeypatch):
    """The safety property the whole section rests on: even if the forge
    were reachable and confirmed the name as a real runner, the guard
    refuses on shape alone and never asks."""
    def unreachable(env):
        raise AssertionError(
            "the guard must refuse before ever building a forge client")
    monkeypatch.setattr(providers.FORGEJO, "forge_client", unreachable)
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("docker must not be touched")))

    r = client.post("/api/runner/start", json={"name": ELSEWHERE_NAME})
    assert r.status_code == 400


def test_the_runner_detail_page_404s_for_an_elsewhere_name(client):
    r = client.get(f"/runner/{ELSEWHERE_NAME}")
    assert r.status_code == 404
