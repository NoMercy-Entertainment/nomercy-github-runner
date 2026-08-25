"""Shapes captured from Forgejo 16.0.2's own swagger, not invented.

Two of them are easy to get wrong by assuming they mirror GitHub:
  - /user/actions/runners returns a BARE ARRAY, not {"runners": [...]}
  - /actions/tasks puts the tasks under "workflow_runs", despite the name
Getting either wrong yields an empty result rather than an error, which is
exactly the failure that survives review.
"""
import json
import urllib.error
import pytest
import forgejo_api

RUNNERS = [
    {"id": 3, "uuid": "edfa80e4-9f11-4757-8626-a707af9be520",
     "name": "beaststack-runner", "status": "idle",
     "labels": ["ubuntu-latest:docker://data.forgejo.org/oci/node:lts"],
     "version": "12.0.0", "ephemeral": False},
    {"id": 4, "uuid": "aa11bb22-0000-4757-8626-000000000000",
     "name": "nomercy-forgejo-1", "status": "active",
     "labels": [], "version": "12.0.0", "ephemeral": False},
]

TASKS = {
    "total_count": 2,
    "workflow_runs": [
        {"id": 830, "name": "build", "status": "success",
         "head_branch": "main", "head_sha": "abcdef1234567890",
         "event": "push", "display_title": "Build the plugin",
         "url": "https://forgejo.example/FiLL/p/actions/runs/12",
         "run_number": 12,
         "run_started_at": "2026-08-25T14:38:55Z",
         "updated_at": "2026-08-25T14:44:02Z",
         "workflow_id": "build.yml"},
        {"id": 829, "name": "test", "status": "failure",
         "head_branch": "main", "head_sha": "0000000000000000",
         "event": "push", "display_title": "Test",
         "url": "https://forgejo.example/FiLL/p/actions/runs/11",
         "run_number": 11,
         "run_started_at": "2026-08-25T14:33:15Z",
         "updated_at": "2026-08-25T14:35:00Z",
         "workflow_id": "test.yml"},
    ],
}


def _client(routes):
    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get = lambda path, params=None: routes.get(path)
    return fj


def test_runner_statuses_reads_a_bare_array():
    fj = _client({"/api/v1/user/actions/runners": RUNNERS})
    assert fj.runner_statuses() == {
        "edfa80e4-9f11-4757-8626-a707af9be520": "idle",
        "aa11bb22-0000-4757-8626-000000000000": "active",
    }


def test_a_failed_call_is_not_an_empty_fleet():
    """None and {} must stay distinguishable: {} would mark every runner
    unknown-but-answered, None says the API could not be reached."""
    fj = _client({})
    assert fj.runner_statuses() is None


def test_registration_token_is_unwrapped():
    fj = _client({"/api/v1/user/actions/runners/registration-token":
                  {"token": "REG-123"}})
    assert fj.registration_token() == "REG-123"


def test_registration_token_survives_a_failure():
    assert _client({}).registration_token() is None


def test_find_task_matches_on_id_and_maps_to_the_runs_columns():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z")
    assert got == {
        "run_id": 830,
        "repo": "FiLL/p",
        "workflow": "build.yml",
        "branch": "main",
        "sha": "abcdef12",
        "actor": None,
        "url": "https://forgejo.example/FiLL/p/actions/runs/12",
        "conclusion": "Succeeded",
        "ended_at": "2026-08-25T14:44:02Z",
    }


def test_find_task_falls_back_to_the_start_time():
    """If ActionTask.id turns out not to be the runner's task number, the
    exact start timestamp from the log still identifies the task."""
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 999999, "2026-08-25T14:33:15Z")
    assert got["run_id"] == 829
    assert got["conclusion"] == "Failed"


def test_conclusions_are_translated_into_the_history_vocabulary():
    """templates/history.html keys its CSS and its result filter on
    Succeeded/Failed/Canceled and history.summary() counts
    SUM(result='Succeeded'). A Forgejo run written with Forgejo's own
    lowercase word counted toward the run total, toward none of the tiles,
    could not be filtered for, and rendered unstyled - so the translation
    happens here, at the boundary, where every one of those three reads it."""
    for forgejo_word, expected in (("success", "Succeeded"),
                                   ("failure", "Failed"),
                                   ("cancelled", "Canceled"),
                                   ("skipped", "Skipped")):
        tasks = {"workflow_runs": [
            {"id": 1, "status": forgejo_word,
             "run_started_at": "2026-08-25T14:00:00Z",
             "updated_at": "2026-08-25T14:01:00Z"}]}
        fj = _client({"/api/v1/repos/o/r/actions/tasks": tasks})
        got = fj.find_task("o/r", 1, "2026-08-25T14:00:00Z")
        assert got["conclusion"] == expected, forgejo_word


def test_an_unfinished_task_is_still_no_task_at_all():
    """The mapping and the terminal-state set are the same table now, so a
    running task must still return None rather than KeyError its way out."""
    tasks = {"workflow_runs": [
        {"id": 1, "status": "running",
         "run_started_at": "2026-08-25T14:00:00Z",
         "updated_at": "2026-08-25T14:01:00Z"}]}
    fj = _client({"/api/v1/repos/o/r/actions/tasks": tasks})
    assert fj.find_task("o/r", 1, "2026-08-25T14:00:00Z") is None


def test_find_task_survives_a_body_that_is_not_an_object():
    """runner_statuses() already guards this exact case with isinstance. Here
    a list body reached `.get("workflow_runs")` and raised AttributeError,
    which propagates out of the enricher sweep instead of leaving the run
    unenriched until the next pass."""
    fj = _client({"/api/v1/repos/o/r/actions/tasks": []})
    assert fj.find_task("o/r", 1, "2026-08-25T14:00:00Z") is None


def test_the_repo_is_quoted_into_the_url_path():
    """`repo` is a non-whitespace capture of a runner log line that lands in
    an API path. Unescaped, a "?" ends the path early and turns the rest into
    a query - a different endpoint from the one this method means to call.
    This branch already allowlists container names because they reach a
    command line; a path segment gets the same treatment, with safe="/" so
    the owner/repo separator survives."""
    seen = []
    fj = forgejo_api.Forgejo("https://forgejo.example", "tok")
    fj._get = lambda path, params=None: seen.append(path)
    fj.find_task("owner/repo?limit=1#x", 1, "")
    assert seen == ["/api/v1/repos/owner/repo%3Flimit%3D1%23x/actions/tasks"]


def test_find_task_gives_up_rather_than_guessing():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    assert fj.find_task("FiLL/p", 12345, "2020-01-01T00:00:00Z") is None


def test_a_still_running_task_is_not_reported_as_finished():
    running = {"total_count": 1, "workflow_runs": [
        dict(TASKS["workflow_runs"][0], status="running", updated_at=None)]}
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": running})
    assert fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z") is None


# ============================================================================
# _request and delete_runner test coverage
# ============================================================================

class FakeResponse:
    def __init__(self, body_bytes):
        self.body_bytes = body_bytes

    def read(self):
        return self.body_bytes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_request_auth_header_is_forgejo_token_not_github_bearer(monkeypatch):
    """Forgejo rejects Bearer auth, which is GitHub's scheme. A future
    "consistency" edit toward github_api.py must fail loudly here."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'{"ok":true}')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "test-token-123")
    fj._get("/api/test")

    assert captured_req is not None
    assert captured_req.get_header("Authorization") == "token test-token-123"
    assert not captured_req.get_header("Authorization").startswith("Bearer")


def test_request_url_construction_with_params(monkeypatch):
    """URL joins base + path, and params produce a query string."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'[]')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get("/api/v1/user/actions/runners", {"limit": 100})

    assert captured_req.full_url == "https://forgejo.example/api/v1/user/actions/runners?limit=100"


def test_request_url_without_params_has_no_trailing_question(monkeypatch):
    """URL without params should not end with '?'."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'[]')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get("/api/test")

    assert not captured_req.full_url.endswith("?")
    assert captured_req.full_url == "https://forgejo.example/api/test"


def test_request_trailing_slash_on_base_is_normalized(monkeypatch):
    """A trailing slash on the base URL should not produce a doubled slash."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'[]')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get("/api/test")

    assert not "//" in captured_req.full_url.split("://")[1]


def test_request_method_dispatch_get(monkeypatch):
    """_get issues a GET request."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'[]')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get("/api/test")

    assert captured_req.get_method() == "GET"


def test_request_method_dispatch_delete(monkeypatch):
    """delete_runner issues a DELETE request."""
    captured_req = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_req
        captured_req = req
        return FakeResponse(b'')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj.delete_runner(123)

    assert captured_req is not None
    assert captured_req.get_method() == "DELETE"
    assert captured_req.full_url == "https://forgejo.example/api/v1/user/actions/runners/123"


def test_request_empty_body_returns_true_sentinel(monkeypatch):
    """An empty response body returns True, not None. This makes
    delete_runner see a no-content DELETE as success."""
    def fake_urlopen(req, timeout=None):
        return FakeResponse(b'')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    result = fj._request("/api/test", "DELETE")

    assert result is True


def test_delete_runner_returns_true_on_empty_response(monkeypatch):
    """delete_runner returns True when the endpoint succeeds (empty body)."""
    def fake_urlopen(req, timeout=None):
        return FakeResponse(b'')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    assert fj.delete_runner(123) is True


def test_request_http_error_returns_none(monkeypatch):
    """HTTPError from urlopen returns None."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    result = fj._request("/api/test")

    assert result is None


def test_request_generic_exception_returns_none(monkeypatch):
    """Generic exception from urlopen returns None."""
    def fake_urlopen(req, timeout=None):
        raise ConnectionError("Network unreachable")

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    result = fj._request("/api/test")

    assert result is None


def test_delete_runner_returns_false_on_failure(monkeypatch):
    """delete_runner returns False when the request fails."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("url", 500, "Server Error", {}, None)

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    assert fj.delete_runner(123) is False


def test_delete_runner_with_none_returns_false_without_request(monkeypatch):
    """delete_runner(None) returns False without issuing any request."""
    fake_called = False

    def fake_urlopen(req, timeout=None):
        nonlocal fake_called
        fake_called = True
        return FakeResponse(b'')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    result = fj.delete_runner(None)

    assert result is False
    assert fake_called is False


def test_delete_runner_with_zero_returns_false_without_request(monkeypatch):
    """delete_runner(0) returns False without issuing any request."""
    fake_called = False

    def fake_urlopen(req, timeout=None):
        nonlocal fake_called
        fake_called = True
        return FakeResponse(b'')

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", fake_urlopen)

    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    result = fj.delete_runner(0)

    assert result is False
    assert fake_called is False


def test_runner_statuses_rejects_non_list_body():
    """A non-list JSON body (e.g. dict with "runners" key) must not
    masquerade as an empty fleet. This pins the counterintuitive bare-array
    shape that someone might "fix" by assuming GitHub's shape."""
    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get = lambda path, params=None: {"runners": []}
    assert fj.runner_statuses() is None
