"""Routes must reach both fleets, and destructive ones must not guess.

"Recreate fleet" removes and rebuilds every runner it is given. With two
fleets on one engine, a request that does not say which one is a request to
destroy the wrong one - so it is rejected rather than defaulted.
"""
import docker_ops
import providers
import runner_detail

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    runner_detail._cache.clear()
    yield


PS = ("github-runner-1\t\n"
      "forgejo-runner-1\tforgejo\n")


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, PS, ""))


def test_a_forgejo_name_is_accepted_by_the_guard(client, fleet):
    r = client.post("/api/runner/start", json={"name": "forgejo-runner-1"})
    assert r.status_code != 400


def test_an_unknown_forgejo_index_is_still_rejected(client, fleet):
    r = client.post("/api/runner/start", json={"name": "forgejo-runner-9"})
    assert r.status_code == 404


def test_a_crafted_name_is_still_refused(client, fleet):
    for bad in ("forgejo-runner-1;id", "gitlab-runner-1", "forgejo-runner-"):
        r = client.post("/api/runner/start", json={"name": bad})
        assert r.status_code == 400, bad


def test_add_requires_a_provider(client, fleet):
    r = client.post("/api/runner/add", json={})
    assert r.status_code == 400
    assert "provider" in r.get_json()["error"]


def test_add_rejects_an_unknown_provider(client, fleet):
    r = client.post("/api/runner/add", json={"provider": "gitlab"})
    assert r.status_code == 400


def test_recreate_requires_a_provider(client, fleet):
    """The one that would otherwise destroy the wrong fleet."""
    r = client.post("/api/recreate", json={})
    assert r.status_code == 400


def test_prune_all_requires_a_provider(client, fleet):
    """Two per-fleet buttons posting to one fleet-blind endpoint would make
    one of them a lie."""
    r = client.post("/api/prune-all", json={})
    assert r.status_code == 400


def test_add_passes_the_provider_through(client, fleet, monkeypatch):
    seen = {}

    def fake_create(index, env, provider=None):
        seen["provider"] = provider
        return True, provider.name_for(index), ""

    monkeypatch.setattr(docker_ops, "create", fake_create)
    r = client.post("/api/runner/add", json={"provider": "forgejo"})
    assert r.status_code == 200
    assert seen["provider"] is providers.FORGEJO
    assert r.get_json()["name"] == "forgejo-runner-2"


# --------------------------------------------------------------------------
# provider/env actually reaching the seam
#
# Until now this wiring was verified by reading the code. docker_ops._bare()
# is exactly the kind of thing that breaks silently: get it wrong and a
# Forgejo runner reads "unknown" from is_idle(), which counts as not-idle,
# and prune is refused for the whole Forgejo fleet for ever - with a 409 that
# says "busy running a job" and looks entirely reasonable.
# --------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    """A populated .env, so `env` reaching the seam is observable at all."""
    import app as dash
    values = {"FORGEJO_INSTANCE_URL": "https://forgejo.example",
              "FORGEJO_API_TOKEN": "tok",
              "FORGEJO_RUNNER_LABELS": "ubuntu-latest:docker://node:lts"}
    monkeypatch.setattr(dash, "read_env", lambda: dict(values))
    return values


def test_prune_route_hands_is_idle_the_provider_and_env(client, fleet, env,
                                                        monkeypatch):
    seen = {}

    def fake_is_idle(name, provider=None, forge_status=None, env=None):
        seen["args"] = (name, provider, env)
        return True

    def fake_prune(name, **kw):
        seen["prune"] = kw
        return {"name": name, "ok": True, "freed_bytes": 0}

    monkeypatch.setattr(docker_ops, "is_idle", fake_is_idle)
    monkeypatch.setattr(docker_ops, "prune", fake_prune)

    r = client.post("/api/runner/forgejo-runner-1/prune")
    assert r.status_code == 200
    assert seen["args"] == ("forgejo-runner-1", providers.FORGEJO, env)
    # and prune() itself gets them too - it re-checks idleness of its own
    assert seen["prune"]["provider"] is providers.FORGEJO
    assert seen["prune"]["env"] == env


def test_prune_route_still_calls_is_idle_bare_for_github(client, fleet, env,
                                                         monkeypatch):
    """The GitHub half of the same convention. Pre-existing tests stub
    is_idle as `lambda n: True`; if this route ever passed the extra
    arguments on the GitHub path, those stubs would start raising
    TypeError."""
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)
    monkeypatch.setattr(docker_ops, "prune",
                        lambda name, **kw: {"name": name, "ok": True,
                                            "freed_bytes": 0})
    r = client.post("/api/runner/github-runner-1/prune")
    assert r.status_code == 200


def test_prune_all_hands_is_idle_the_provider_and_env(client, fleet, env,
                                                      monkeypatch):
    seen = []
    monkeypatch.setattr(
        docker_ops, "is_idle",
        lambda name, provider=None, forge_status=None, env=None:
            seen.append((name, provider, env)) or True)
    monkeypatch.setattr(docker_ops, "prune",
                        lambda name, **kw: {"name": name, "ok": True,
                                            "freed_bytes": 0})

    r = client.post("/api/prune-all", json={"provider": "forgejo"})
    assert r.status_code == 200
    assert seen == [("forgejo-runner-1", providers.FORGEJO, env)]


def test_recreate_hands_remove_and_create_the_provider_and_env(client, fleet,
                                                               env,
                                                               monkeypatch):
    removed, created = [], []

    def fake_remove(name, provider=None, env=None):
        removed.append((name, provider, env))
        return True, "", ""

    def fake_create(index, env, provider=None):
        created.append((index, env, provider))
        return True, provider.name_for(index), None

    monkeypatch.setattr(docker_ops, "remove", fake_remove)
    monkeypatch.setattr(docker_ops, "create", fake_create)

    r = client.post("/api/recreate", json={"provider": "forgejo"})
    assert r.status_code == 200
    assert removed == [("forgejo-runner-1", providers.FORGEJO, env)]
    assert created == [(1, env, providers.FORGEJO)]


def test_recreate_still_calls_remove_and_create_bare_for_github(client, fleet,
                                                                env,
                                                                monkeypatch):
    """tests/test_routes.py - pre-existing, not editable - stubs these as
    `fake_remove(name)` and `fake_create(idx, env)`. This is the guard that
    the GitHub path keeps matching those signatures."""
    monkeypatch.setattr(docker_ops, "remove", lambda name: (True, "", ""))
    monkeypatch.setattr(docker_ops, "create",
                        lambda idx, env: (True, f"github-runner-{idx}", None))
    r = client.post("/api/recreate", json={"provider": "github"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# --------------------------------------------------------------------------
# the runner detail page knows which forge it is looking at
#
# templates/runner.html was written when there was only one. On
# /runner/forgejo-runner-1 it rendered a "GitHub view" panel that fetches the
# GitHub ORG's runner list - a list a Forgejo runner is not in - and told the
# operator the runner would be "deregistered from GitHub".
# --------------------------------------------------------------------------

def test_the_github_panel_is_absent_on_a_forgejo_runner(client, fleet):
    body = client.get("/runner/forgejo-runner-1").get_data(as_text=True)
    assert "hist-gh-panel" not in body
    assert "GitHub view" not in body
    # and nothing is left behind to fetch it
    assert "if (PROVIDER === 'github') fetchHistoryGithub();" in body


def test_the_github_panel_is_still_there_on_a_github_runner(client, fleet):
    body = client.get("/runner/github-runner-1").get_data(as_text=True)
    assert "hist-gh-panel" in body
    assert "GitHub view" in body


def test_the_page_names_the_right_forge(client, fleet):
    forgejo = client.get("/runner/forgejo-runner-1").get_data(as_text=True)
    assert '"Forgejo"' in forgejo
    github = client.get("/runner/github-runner-1").get_data(as_text=True)
    assert '"GitHub"' in github
    # The remove confirmation reads the label rather than hardcoding a forge.
    for body in (forgejo, github):
        assert ("It is deregistered from ${FORGE} and the container is "
                "deleted.") in body
        assert "deregistered from GitHub and the container" not in body
