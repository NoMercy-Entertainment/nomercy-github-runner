"""GitHub run results for native history rows.

The runner's own completion hook closes a GitHub run without a result: the
hook cannot know whether the job passed. Every such row already stores its
repository, run id and attempt, so one call to that attempt's jobs gives the
conclusion. Before this, 521 GitHub runs in a row showed "unknown"
(2026-10-23..27).
"""
import history
import providers
import app as dash
import github_api
import pytest


@pytest.fixture(autouse=True)
def _nothing_asked_yet():
    """Row ids repeat across fresh databases; a row asked about in one
    test is not one asked about in the next."""
    dash._RESULTS_ASKED.clear()
    yield
    dash._RESULTS_ASKED.clear()


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()


def _native_github_row(name, job, started, ended, run_id=4242,
                       repo="NoMercy-Entertainment/app", result=None,
                       provider="github"):
    with history._lock, history._conn() as c:
        cur = c.execute(
            "INSERT INTO runs (runner, registration, job_name, started_at,"
            " ended_at, duration_s, result, provider, runner_id, gh_checked,"
            " gh_run_id, gh_repo) VALUES (?,?,?,?,?,0,?,?,?,1,?,?)",
            ("uuid-" + name, name, job, started, ended, result, provider,
             "uuid-" + name, run_id, repo))
        return cur.lastrowid


def _result_of(run_id):
    with history._conn() as c:
        row = c.execute("SELECT result, gh_conclusion, gh_url FROM runs"
                        " WHERE id=?", (run_id,)).fetchone()
        return dict(row)


class FakeProvider:
    def __init__(self, client):
        self._client = client

    def forge_client(self, env):
        return self._client


class FakeResults:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def job_result(self, repo, run_id, attempt, runner_name, started_at):
        self.calls.append((repo, run_id, attempt, runner_name))
        return self.answers.get(runner_name)


def _wire(monkeypatch, client):
    monkeypatch.setattr(providers, "by_key",
                        lambda k: FakeProvider(client) if k == "github" else None)


def test_pending_results_are_closed_github_rows_without_a_result(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    old = _native_github_row("gh-1", "build", "2026-10-26T10:00:00.1Z", "2026-10-26T10:05:00.1Z")
    new = _native_github_row("gh-2", "test", "2026-10-26T11:00:00.1Z", "2026-10-26T11:05:00.1Z")
    _native_github_row("gh-3", "open", "2026-10-26T12:00:00.1Z", None)
    _native_github_row("gh-4", "done", "2026-10-26T09:00:00.1Z", "2026-10-26T09:01:00.1Z", result="Succeeded")
    _native_github_row("fj-1", "fj", "2026-10-26T09:00:00.1Z", "2026-10-26T09:01:00.1Z", provider="forgejo")
    _native_github_row("gh-5", "norun", "2026-10-26T09:00:00.1Z", "2026-10-26T09:01:00.1Z", run_id=None)

    pending = history.pending_results(limit=10)

    assert [p["id"] for p in pending] == [new, old]
    assert pending[0]["attempt"] == "1"
    assert pending[0]["gh_repo"] == "NoMercy-Entertainment/app"


def test_attempt_comes_from_the_native_job_ledger(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_github_row("gh-1", "build", "2026-10-26T10:00:00.1Z", "2026-10-26T10:05:00.1Z")
    with history._lock, history._conn() as c:
        c.execute("CREATE TABLE native_job_runs(runner_id TEXT, provider TEXT,"
                  " forge_run TEXT, attempt TEXT, job_id TEXT, job_name TEXT,"
                  " history_id INTEGER, source TEXT, at TEXT)")
        c.execute("INSERT INTO native_job_runs VALUES(?,?,?,?,?,?,?,?,?)",
                  ("uuid-gh-1", "github", "4242", "3", "hook:x", "build", rid,
                   "worker", "2026-10-26T10:00:00Z"))

    assert history.pending_results(limit=10)[0]["attempt"] == "3"


def test_apply_result_never_overwrites_a_known_result(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_github_row("gh-1", "build", "2026-10-26T10:00:00.1Z",
                             "2026-10-26T10:05:00.1Z", result="Failed")

    history.apply_result(rid, "Succeeded", "success", "https://x")

    assert _result_of(rid)["result"] == "Failed"


def test_sweep_fills_the_result_from_the_github_job(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ok = _native_github_row("gh-1", "build", "2026-10-26T10:00:00.1Z", "2026-10-26T10:05:00.1Z")
    bad = _native_github_row("gh-2", "test", "2026-10-26T10:00:00.1Z", "2026-10-26T10:05:00.1Z")
    client = FakeResults({
        "gh-1": {"conclusion": "success", "url": "https://gh/job/1"},
        "gh-2": {"conclusion": "cancelled", "url": "https://gh/job/2"},
    })
    _wire(monkeypatch, client)

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z")

    assert _result_of(ok) == {"result": "Succeeded", "gh_conclusion": "success",
                              "gh_url": "https://gh/job/1"}
    assert _result_of(bad)["result"] == "Canceled"
    assert ("NoMercy-Entertainment/app", 4242, "1", "gh-1") in client.calls


def test_unresolved_young_run_waits_and_stale_run_becomes_unknown(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    young = _native_github_row("gh-1", "build", "2026-10-26T10:00:00.1Z", "2026-10-26T10:05:00.1Z")
    stale = _native_github_row("gh-2", "build", "2026-10-20T10:00:00.1Z", "2026-10-20T10:05:00.1Z")
    _wire(monkeypatch, FakeResults({}))

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z")

    assert _result_of(young)["result"] is None
    assert _result_of(stale)["result"] == "Unknown"


def test_no_github_client_leaves_rows_untouched(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_github_row("gh-1", "build", "2026-10-20T10:00:00.1Z", "2026-10-20T10:05:00.1Z")
    monkeypatch.setattr(providers, "by_key", lambda k: None)

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z")

    assert _result_of(rid)["result"] is None


def _client_with_jobs(monkeypatch, jobs):
    cls = next(v for v in vars(github_api).values()
               if isinstance(v, type) and hasattr(v, "find_job"))
    client = cls.__new__(cls)
    seen = []

    def fake_get(path, params=None, conditional=False):
        seen.append(path)
        return {"jobs": jobs}

    monkeypatch.setattr(client, "_get", fake_get, raising=False)
    return client, seen


def test_job_result_matches_the_runner_and_the_closest_start(monkeypatch):
    client, seen = _client_with_jobs(monkeypatch, [
        {"runner_name": "gh-9", "status": "completed", "conclusion": "failure",
         "started_at": "2026-10-26T10:00:00Z", "html_url": "u0"},
        {"runner_name": "gh-1", "status": "completed", "conclusion": "failure",
         "started_at": "2026-10-26T08:00:00Z", "html_url": "u1"},
        {"runner_name": "gh-1", "status": "completed", "conclusion": "success",
         "started_at": "2026-10-26T10:00:02Z", "html_url": "u2",
         "workflow_name": "CI", "head_branch": "master"},
    ])

    found = client.job_result("o/r", 4242, "2", "gh-1", "2026-10-26T10:00:00.1Z")

    assert seen == ["/repos/o/r/actions/runs/4242/attempts/2/jobs"]
    assert found["conclusion"] == "success" and found["url"] == "u2"


def test_job_result_is_none_until_the_job_completes(monkeypatch):
    client, _ = _client_with_jobs(monkeypatch, [
        {"runner_name": "gh-1", "status": "in_progress", "conclusion": None,
         "started_at": "2026-10-26T10:00:02Z"},
    ])

    assert client.job_result("o/r", 4242, "1", "gh-1", "2026-10-26T10:00:00Z") is None


def test_job_result_is_none_for_another_runner(monkeypatch):
    client, _ = _client_with_jobs(monkeypatch, [
        {"runner_name": "gh-2", "status": "completed", "conclusion": "success",
         "started_at": "2026-10-26T10:00:02Z"},
    ])

    assert client.job_result("o/r", 4242, "1", "gh-1", "2026-10-26T10:00:00Z") is None


# ---------------------------------------------------------------------------
# rows the completion hook never closed, Forgejo rows, and the backlog order
# ---------------------------------------------------------------------------

class FakeClosing(FakeResults):
    """job_result also says when the job completed, as GitHub does."""


def test_an_open_github_run_whose_job_completed_is_closed(tmp_path, monkeypatch):
    """Three GitHub rows read "running" for days: the completion hook never
    arrived (2026-10-24/25). GitHub knows the job ended and how."""
    _fresh_db(tmp_path, monkeypatch)
    stuck = _native_github_row("gh-1", "build", "2026-10-24T17:22:51.5Z", None)
    _wire(monkeypatch, FakeResults({"gh-1": {
        "conclusion": "failure", "url": "https://gh/job/9",
        "completed_at": "2026-10-24T17:30:00Z"}}))

    dash._enrich_pending({}, stale_before="2026-10-26T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    with history._conn() as c:
        row = dict(c.execute("SELECT ended_at, result, duration_s, gh_url"
                             " FROM runs WHERE id=?", (stuck,)).fetchone())
    assert row == {"ended_at": "2026-10-24T17:30:00Z", "result": "Failed",
                   "duration_s": 428, "gh_url": "https://gh/job/9"}


def test_a_young_open_run_is_not_asked_about(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _native_github_row("gh-1", "build", "2026-10-27T05:10:00.1Z", None)
    client = FakeResults({})
    _wire(monkeypatch, client)

    dash._enrich_pending({}, stale_before="2026-10-26T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    assert client.calls == []


def test_an_open_run_github_never_finished_is_closed_unknown_once_stale(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_github_row("gh-1", "build", "2026-10-20T10:00:00.1Z", None)
    _wire(monkeypatch, FakeResults({}))

    dash._enrich_pending({}, stale_before="2026-10-26T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    with history._conn() as c:
        row = c.execute("SELECT ended_at, result FROM runs WHERE id=?", (rid,)).fetchone()
    assert row["result"] == "Unknown" and row["ended_at"] == "2026-10-20T10:00:00.1Z"


def test_rows_asked_about_recently_let_the_older_backlog_through(tmp_path, monkeypatch):
    """Newest first, a page at a time: rows GitHub cannot answer yet must not
    hold the page forever while the backlog behind them waits."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(dash, "RESULTS_PAGE", 2)
    dash._RESULTS_ASKED.clear()
    young = [_native_github_row(f"gh-{i}", "b", f"2026-10-26T1{i}:00:00.1Z",
                                f"2026-10-26T1{i}:05:00.1Z") for i in (5, 6)]
    old = _native_github_row("gh-1", "b", "2026-10-26T09:00:00.1Z", "2026-10-26T09:05:00.1Z")
    client = FakeResults({"gh-1": {"conclusion": "success"}})
    _wire(monkeypatch, client)

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")
    assert _result_of(old)["result"] is None          # the page was the two young ones
    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    assert _result_of(old)["result"] == "Succeeded"
    assert all(_result_of(y)["result"] is None for y in young)


class FakeTasks:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def find_task(self, repo, task_id, started_at):
        self.calls.append((repo, task_id))
        return self.answer


def _native_forgejo_row(name, started, ended, result=None, task=1535):
    with history._lock, history._conn() as c:
        cur = c.execute(
            "INSERT INTO runs (runner, registration, job_name, started_at,"
            " ended_at, result, provider, runner_id, gh_checked, gh_run_id,"
            " gh_repo, forge_task_id) VALUES (?,?,?,?,?,?,'forgejo',?,1,991,"
            " 'FiLL/nomercy-runners',?)",
            ("uuid-" + name, name, "macos", started, ended, result,
             "uuid-" + name, task))
        return cur.lastrowid


def test_a_forgejo_run_left_open_is_closed_from_its_task(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_forgejo_row("fj-1", "2026-10-26T10:00:00.1Z", None)
    tasks = FakeTasks({"conclusion": "Succeeded", "ended_at": "2026-10-26T10:03:00Z",
                       "url": "https://fj/run/1"})
    monkeypatch.setattr(providers, "by_key",
                        lambda k: FakeProvider(tasks) if k == "forgejo" else None)

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    assert tasks.calls == [("FiLL/nomercy-runners", 1535)]
    with history._conn() as c:
        row = c.execute("SELECT ended_at, result FROM runs WHERE id=?", (rid,)).fetchone()
    assert (row["ended_at"], row["result"]) == ("2026-10-26T10:03:00Z", "Succeeded")


def test_a_closed_forgejo_run_without_a_result_gets_its_task_result(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_forgejo_row("fj-1", "2026-10-26T10:00:00.1Z", "2026-10-26T10:03:00.1Z")
    tasks = FakeTasks({"conclusion": "Failed", "ended_at": "2026-10-26T10:03:00Z"})
    monkeypatch.setattr(providers, "by_key",
                        lambda k: FakeProvider(tasks) if k == "forgejo" else None)

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z",
                         open_before="2026-10-27T05:00:00Z")

    assert _result_of(rid)["result"] == "Failed"


def test_job_result_carries_the_completion_time(monkeypatch):
    client, _ = _client_with_jobs(monkeypatch, [
        {"runner_name": "gh-1", "status": "completed", "conclusion": "success",
         "started_at": "2026-10-26T10:00:02Z", "completed_at": "2026-10-26T10:09:00Z"},
    ])

    found = client.job_result("o/r", 4242, "1", "gh-1", "2026-10-26T10:00:00Z")

    assert found["completed_at"] == "2026-10-26T10:09:00Z"


def test_a_github_that_does_not_answer_never_closes_a_run_unknown(tmp_path, monkeypatch):
    """A DNS failure answered like "no such job" closed a stale run as
    Unknown (2026-10-27). No answer is asked again, never a verdict."""
    _fresh_db(tmp_path, monkeypatch)
    rid = _native_github_row("gh-1", "build", "2026-10-20T10:00:00.1Z", "2026-10-20T10:05:00.1Z")

    class Unreachable(FakeResults):
        def job_result(self, *a):
            return False
    _wire(monkeypatch, Unreachable({}))

    dash._enrich_pending({}, stale_before="2026-10-25T00:00:00Z")

    assert _result_of(rid)["result"] is None


def test_job_result_is_false_when_github_does_not_answer(monkeypatch):
    client, _ = _client_with_jobs(monkeypatch, [])
    monkeypatch.setattr(client, "_get", lambda *a, **k: None, raising=False)
    assert client.job_result("o/r", 4242, "1", "gh-1", "2026-10-26T10:00:00Z") is False
