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


def test_prune_requires_post(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    assert client.get("/api/runner/github-runner-1/prune").status_code == 405


def test_prune_rejects_a_bad_name_before_any_docker_call(client, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("docker must not be called for an invalid name")
    monkeypatch.setattr(docker_ops, "_docker", explode)
    r = client.post("/api/runner/nope/prune")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_prune_skips_a_busy_runner(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: False)
    called = []
    monkeypatch.setattr(docker_ops, "prune", lambda n, **k: called.append(n))
    r = client.post("/api/runner/github-runner-1/prune")
    assert r.status_code == 409
    assert r.get_json()["ok"] is False
    assert "busy" in r.get_json()["error"].lower()
    assert called == [], "a busy runner must not be pruned"


def test_prune_runs_on_an_idle_runner(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)
    monkeypatch.setattr(docker_ops, "prune",
                        lambda n, **k: {"name": n, "ok": True, "error": None,
                                        "before": {}, "after": {}, "freed_bytes": 5})
    r = client.post("/api/runner/github-runner-1/prune")
    assert r.status_code == 200
    assert r.get_json()["data"]["freed_bytes"] == 5


def test_prune_all_skips_busy_and_totals_the_rest(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names",
                        lambda: ["github-runner-1", "github-runner-2", "github-runner-3"])
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: n != "github-runner-2")
    monkeypatch.setattr(docker_ops, "prune",
                        lambda n, **k: {"name": n, "ok": True, "error": None,
                                        "before": {}, "after": {}, "freed_bytes": 10})
    body = client.post("/api/prune-all").get_json()
    assert body["ok"] is True
    assert [s["name"] for s in body["data"]["skipped"]] == ["github-runner-2"]
    assert len(body["data"]["results"]) == 2
    assert body["data"]["freed_bytes"] == 20


def test_page_has_the_four_tabs_and_the_action_footer(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: ["github-runner-1"])
    body = client.get("/runner/github-runner-1").data.decode()
    for anchor in ["rd-header", "rd-tabs", "tab-overview", "tab-engine",
                   "tab-logs", "tab-history", "rd-actions"]:
        assert anchor in body, anchor


def test_missing_runner_page_says_so(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: [])
    r = client.get("/runner/github-runner-1")
    assert r.status_code == 404
    assert b"no longer exists" in r.data


def test_recreate_happy_path_removes_and_recreates_every_runner(client, monkeypatch):
    names = ["github-runner-1", "github-runner-2"]
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: list(names))
    remove_calls = []

    def fake_remove(name):
        remove_calls.append(name)
        return True, "", ""

    create_calls = []

    def fake_create(idx, env):
        create_calls.append(idx)
        return True, f"github-runner-{idx}", None

    monkeypatch.setattr(docker_ops, "remove", fake_remove)
    monkeypatch.setattr(docker_ops, "create", fake_create)

    r = client.post("/api/recreate")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "aborted_at" not in body
    assert remove_calls == ["github-runner-1", "github-runner-2"]
    assert create_calls == [1, 2]
    assert len(body["results"]) == 2
    assert all(res["ok"] for res in body["results"])


def test_removal_reported_as_failed_but_container_gone_still_creates_replacement(
        client, monkeypatch):
    """A `docker rm -f` that exceeds its timeout reports failure to the caller
    even when the container is actually gone. The route must not trust that
    exit status - it must check list_runner_names() and, finding the runner
    really gone, still create its replacement."""
    state = {"names": ["github-runner-1"]}
    monkeypatch.setattr(docker_ops, "list_runner_names",
                        lambda: list(state["names"]))

    def fake_remove(name):
        # Slow-but-successful removal: the container is gone, but the
        # command itself reports failure (e.g. a timeout).
        state["names"].remove(name)
        return False, "", "timed out after 180s"

    monkeypatch.setattr(docker_ops, "remove", fake_remove)
    monkeypatch.setattr(docker_ops, "create",
                        lambda idx, env: (True, f"github-runner-{idx}", None))

    r = client.post("/api/recreate")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["results"] == [
        {"name": "github-runner-1", "ok": True, "error": None},
    ]


def test_removal_that_leaves_the_container_present_aborts_the_sweep(client, monkeypatch):
    """Regression test for the incident that destroyed six production runners
    in one recreate sweep: a removal that both reported failure AND left the
    container behind was, before this fix, treated as "skip this one and
    keep going" - which meant the loop moved on to destroy the next runner,
    and the next, until the whole fleet was gone with nothing recreated.

    The fix must stop at the first runner that is confirmed still present,
    and - the assertion that actually encodes the incident - never call
    remove() again for any runner after that point.
    """
    names = ["github-runner-1", "github-runner-2", "github-runner-3"]
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: list(names))

    remove_calls = []

    def fake_remove(name):
        remove_calls.append(name)
        # Reports failure AND the container is still present (not removed
        # from `names`) - the exact shape of the wedged/Dead-state removal
        # that triggered the outage.
        return False, "", "timed out after 180s"

    create_calls = []

    monkeypatch.setattr(docker_ops, "remove", fake_remove)
    monkeypatch.setattr(docker_ops, "create",
                        lambda idx, env: create_calls.append(idx) or
                        (True, f"github-runner-{idx}", None))

    r = client.post("/api/recreate")
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert body["aborted_at"] == "github-runner-1"
    assert remove_calls == ["github-runner-1"], \
        "the sweep must stop, not cascade into removing the rest of the fleet"
    assert create_calls == []


def test_failing_create_also_aborts_the_sweep(client, monkeypatch):
    """A runner removed and not replaced is lost capacity. Repeating that
    across the fleet is the same cascade as the remove-side bug, just
    triggered from the create() side - it must abort too."""
    names = ["github-runner-1", "github-runner-2"]
    monkeypatch.setattr(docker_ops, "list_runner_names", lambda: list(names))

    remove_calls = []

    def fake_remove(name):
        remove_calls.append(name)
        names.remove(name)
        return True, "", ""

    monkeypatch.setattr(docker_ops, "remove", fake_remove)
    monkeypatch.setattr(docker_ops, "create",
                        lambda idx, env: (False, f"github-runner-{idx}", "create failed"))

    r = client.post("/api/recreate")
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert body["aborted_at"] == "github-runner-1"
    assert remove_calls == ["github-runner-1"], \
        "must not remove the next runner after a failed create"


def test_prune_all_still_reports_when_one_runner_fails(client, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_runner_names",
                        lambda: ["github-runner-1", "github-runner-2"])
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)

    def one_fails(n, **k):
        ok = n == "github-runner-1"
        return {"name": n, "ok": ok, "error": None if ok else "boom",
                "before": {}, "after": {}, "freed_bytes": 10 if ok else 0}

    monkeypatch.setattr(docker_ops, "prune", one_fails)
    body = client.post("/api/prune-all").get_json()
    # One failure must not hide the other runner's result or abort the sweep.
    assert len(body["data"]["results"]) == 2
    assert body["data"]["freed_bytes"] == 10
    assert body["ok"] is False
