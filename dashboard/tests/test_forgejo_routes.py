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
