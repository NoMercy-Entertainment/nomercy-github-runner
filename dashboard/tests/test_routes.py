import pytest

import docker_ops
import runner_detail


@pytest.fixture(autouse=True)
def _clear_cache():
    """Routes memoise. Without this, one test's monkeypatched collector result
    is served to the next test and the failure is baffling to debug."""
    runner_detail._cache.clear()
    yield


ENDPOINTS = ["inspect", "engine", "logs", "series", "github", "history"]


def test_all_detail_endpoints_require_auth(anon_client):
    for ep in ENDPOINTS:
        r = anon_client.get(f"/api/runner/github-runner-1/{ep}")
        assert r.status_code == 401, ep


def test_page_requires_auth(anon_client):
    r = anon_client.get("/runner/github-runner-1")
    assert r.status_code in (301, 302)
    assert "/login" in r.headers["Location"]


def test_no_crafted_name_can_reach_a_docker_call(client, monkeypatch):
    """The property that matters: a name from the URL never reaches a command line.

    Two layers enforce it and both are acceptable outcomes. Path-traversal forms
    never match Flask's <name> converter, so Werkzeug rejects them before any
    application code runs. Everything else reaches _detail_target() and is
    rejected there. Asserting one specific status would be testing which layer
    happened to catch it, not the property itself.
    """
    called = []
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: called.append(a) or (True, "", ""))
    monkeypatch.setattr(docker_ops, "list_runner_names",
                        lambda: ["github-runner-1"])
    for bad in ["../etc", "..%2fetc", "%2e%2e%2fetc",
                "github-runner-1/../../x", "github-runner-1;id",
                "github-runner-1 --privileged", "github-runner-",
                "github-runner-1x", "nope"]:
        r = client.get(f"/api/runner/{bad}/inspect")
        assert r.status_code in (400, 404), (bad, r.status_code)
        assert not called, f"docker was invoked for {bad!r}"


def test_routable_invalid_names_return_the_400_envelope(client, monkeypatch):
    """Names that do reach the route must come back in the documented shape."""
    monkeypatch.setattr(docker_ops, "list_runner_names",
                        lambda: ["github-runner-1"])
    for bad in ["github-runner-", "github-runner-1x", "nope",
                "github-runner-1;id"]:
        r = client.get(f"/api/runner/{bad}/inspect")
        assert r.status_code == 400, bad
        body = r.get_json()
        assert body["ok"] is False
        assert body["error"] == "bad runner name"


def test_unknown_runner_is_404(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    r = client.get("/api/runner/github-runner-7/inspect")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_inspect_route_returns_the_collector_payload(client, monkeypatch):
    import runner_detail
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(runner_detail, "inspect",
                        lambda n: {"ok": True, "data": {"image": "x"}})
    r = client.get("/api/runner/github-runner-1/inspect")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "data": {"image": "x"}}


def test_collector_failure_is_500_with_the_error(client, monkeypatch):
    import runner_detail
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(runner_detail, "inspect",
                        lambda n: {"ok": False, "error": "boom"})
    r = client.get("/api/runner/github-runner-1/inspect")
    assert r.status_code == 500
    assert r.get_json()["error"] == "boom"


def test_logs_route_passes_the_cursor_through(client, monkeypatch):
    import runner_detail
    seen = {}
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])

    def fake_logs(name, since=""):
        seen["since"] = since
        return {"ok": True, "data": {"lines": [], "cursor": since}}

    monkeypatch.setattr(runner_detail, "logs", fake_logs)
    client.get("/api/runner/github-runner-1/logs?since=2026-07-28T10:00:00Z")
    assert seen["since"] == "2026-07-28T10:00:00Z"


def test_series_route_returns_a_list(client, monkeypatch):
    import app as dash
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    dash._series.clear()
    dash._record_series({"generated": "t", "runners": [
        {"name": "github-runner-1", "cpu_percent": 5,
         "mem_used": "1GiB", "build_cache": "1GB"}]})
    r = client.get("/api/runner/github-runner-1/series")
    assert r.status_code == 200
    assert len(r.get_json()["data"]) == 1


def test_github_route_returns_500_when_runners_api_is_unreachable(client, monkeypatch):
    """None from runners() means the API call itself failed - the route must
    say so, not silently render an empty (and misleading) runner list."""
    import app as dash
    import github_api
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(dash, "read_env",
                        lambda: {"GH_TOKEN": "x", "GITHUB_ORG": "org"})

    class FakeGH:
        def __init__(self, token, org):
            pass

        def runners(self):
            return None

    monkeypatch.setattr(github_api, "GitHub", FakeGH)
    r = client.get("/api/runner/github-runner-1/github")
    assert r.status_code == 500
    assert r.get_json()["ok"] is False


def test_github_route_returns_empty_list_for_a_genuinely_empty_org(client, monkeypatch):
    """[] must still mean "asked, and there are none" - distinct from the
    failure case above, which is the whole point of the None/[] split."""
    import app as dash
    import github_api
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(dash, "read_env",
                        lambda: {"GH_TOKEN": "x", "GITHUB_ORG": "org"})

    class FakeGH:
        def __init__(self, token, org):
            pass

        def runners(self):
            return []

    monkeypatch.setattr(github_api, "GitHub", FakeGH)
    r = client.get("/api/runner/github-runner-1/github")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["data"] == []


def test_page_renders_for_a_known_runner(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    r = client.get("/runner/github-runner-1")
    assert r.status_code == 200
    assert b"github-runner-1" in r.data
