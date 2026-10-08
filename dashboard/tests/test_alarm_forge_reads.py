"""What the alarm monitor reads from the forges (GitHub #11).

Every page of every list, because a runner on page two is a runner the
alarm cannot see; a failed page is None, never a short list, because a
runner missing from a short list reads as one that was removed. And the
GitHub client keeps what the rate limit says, so the monitor can stop
before it spends the token every other reader shares.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import forgejo_api
import github_api

ORG = "NoMercy-Entertainment"


class _Answer:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _runner(n, status="online", labels=("self-hosted", "Linux")):
    return {"id": n, "name": f"runner-{n}", "status": status, "busy": False,
            "os": "linux", "version": "2.330.0",
            "labels": [{"name": label, "type": "custom"} for label in labels]}


def fake(monkeypatch, route):
    """urlopen answering from `route(path, query)`, recording every URL."""
    asked = []

    def urlopen(req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        asked.append((url.path, query, dict(req.header_items())))
        answer = route(url.path, query, dict(req.header_items()))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


class TestEveryGitHubRunner:
    def test_every_page_is_read(self, monkeypatch):
        pages = [[_runner(n) for n in range(1, 101)],
                 [_runner(101, "offline", ("self-hosted", "macOS", "xcode"))]]

        def route(path, query, headers):
            page = int(query.get("page", 1))
            return _Answer({"total_count": 101, "runners": pages[page - 1]})
        asked = fake(monkeypatch, route)
        runners = github_api.GitHub("t", ORG).all_runners()
        assert len(runners) == 101
        assert runners[-1] == {"id": 101, "name": "runner-101", "status": "offline",
                               "busy": False, "os": "linux", "version": "2.330.0",
                               "labels": ["self-hosted", "macOS", "xcode"]}
        assert [q["page"] for _, q, _ in asked] == ["1", "2"]
        assert all(p == f"/orgs/{ORG}/actions/runners" for p, _, _ in asked)

    def test_a_failed_page_is_none_never_a_short_list(self, monkeypatch):
        def route(path, query, headers):
            if query.get("page") == "2":
                return OSError("connection reset")
            return _Answer({"runners": [_runner(n) for n in range(1, 101)]})
        fake(monkeypatch, route)
        assert github_api.GitHub("t", ORG).all_runners() is None

    def test_the_old_reader_reads_every_page_too(self, monkeypatch):
        def route(path, query, headers):
            if path.endswith("/actions/runners"):
                page = int(query.get("page", 1))
                return _Answer({"runners": [_runner(n) for n in range(1, 101)]
                                if page == 1 else [_runner(101)]})
            return _Answer({"runner_groups": []})
        fake(monkeypatch, route)
        assert len(github_api.GitHub("t", ORG).runners()) == 101


class TestTheRateLimitIsKept:
    def test_every_answer_updates_what_is_left(self, monkeypatch):
        def route(path, query, headers):
            return _Answer({"runners": []}, {"X-RateLimit-Remaining": "4321",
                                             "X-RateLimit-Reset": "1790000000"})
        fake(monkeypatch, route)
        gh = github_api.GitHub("t", ORG)
        assert gh.rate_remaining is None
        gh.all_runners()
        assert gh.rate_remaining == 4321 and gh.rate_reset == 1790000000

    def test_a_refusal_updates_it_too(self, monkeypatch):
        def route(path, query, headers):
            return urllib.error.HTTPError(
                "https://api.github.com" + path, 403, "rate limited",
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000100"}, None)
        fake(monkeypatch, route)
        gh = github_api.GitHub("t", ORG)
        assert gh.all_runners() is None
        assert gh.rate_remaining == 0 and gh.rate_reset == 1790000100


class TestQueuedWork:
    def test_org_repos_every_page_without_archived_ones(self, monkeypatch):
        def route(path, query, headers):
            assert path == f"/orgs/{ORG}/repos"
            if query["page"] == "1":
                return _Answer([{"full_name": f"{ORG}/r{n}", "archived": n == 5}
                                for n in range(100)])
            return _Answer([{"full_name": f"{ORG}/last", "archived": False}])
        fake(monkeypatch, route)
        repos = github_api.GitHub("t", ORG).org_repos()
        assert len(repos) == 100 and f"{ORG}/r5" not in repos and repos[-1] == f"{ORG}/last"

    def test_runs_still_waiting_are_queued_and_in_progress_ones(self, monkeypatch):
        def route(path, query, headers):
            assert path == f"/repos/{ORG}/app/actions/runs"
            return _Answer({"workflow_runs": [{"id": {"queued": 1, "in_progress": 2}
                                               [query["status"]], "name": "build"}]})
        asked = fake(monkeypatch, route)
        runs = github_api.GitHub("t", ORG).waiting_runs(f"{ORG}/app")
        assert [r["id"] for r in runs] == [1, 2]
        assert sorted(q["status"] for _, q, _ in asked) == ["in_progress", "queued"]

    def test_a_run_list_that_could_not_be_read_is_none(self, monkeypatch):
        fake(monkeypatch, lambda path, query, headers: OSError("down"))
        assert github_api.GitHub("t", ORG).waiting_runs(f"{ORG}/app") is None

    def test_a_runs_jobs(self, monkeypatch):
        fake(monkeypatch, lambda path, query, headers: _Answer(
            {"jobs": [{"id": 7, "status": "queued", "labels": ["self-hosted", "xcode"]}]}))
        jobs = github_api.GitHub("t", ORG).run_jobs(f"{ORG}/app", 1)
        assert jobs == [{"id": 7, "status": "queued", "labels": ["self-hosted", "xcode"]}]

    def test_an_unchanged_answer_is_asked_for_conditionally(self, monkeypatch):
        """A 304 does not count against GitHub's rate limit, so asking the
        same question every five minutes costs nothing while nothing moved."""
        body = {"jobs": [{"id": 7, "status": "queued", "labels": ["x"]}]}

        def route(path, query, headers):
            if headers.get("If-none-match") == '"v1"':
                return urllib.error.HTTPError("u", 304, "not modified", {}, None)
            return _Answer(body, {"ETag": '"v1"'})
        asked = fake(monkeypatch, route)
        gh = github_api.GitHub("t", ORG)
        assert gh.run_jobs(f"{ORG}/app", 1) == body["jobs"]
        assert gh.run_jobs(f"{ORG}/app", 1) == body["jobs"]
        assert asked[1][2].get("If-none-match") == '"v1"'


class TestEveryForgejoRunner:
    def test_every_page_is_read(self, monkeypatch):
        def route(path, query, headers):
            assert path == "/api/v1/user/actions/runners"
            page = int(query["page"])
            if page == 1:
                return _Answer([{"uuid": f"u{n}", "name": f"r{n}", "status": "idle"}
                                for n in range(50)])
            return _Answer([{"uuid": "last", "name": "last", "status": "offline"}])
        asked = fake(monkeypatch, route)
        runners = forgejo_api.Forgejo("https://git.example", "t").all_runners()
        assert len(runners) == 51 and runners[-1]["uuid"] == "last"
        assert [q["page"] for _, q, _ in asked] == ["1", "2"]

    def test_a_server_that_ignores_the_page_does_not_loop(self, monkeypatch):
        same = [{"uuid": f"u{n}", "status": "idle"} for n in range(50)]
        asked = fake(monkeypatch, lambda path, query, headers: _Answer(same))
        runners = forgejo_api.Forgejo("https://git.example", "t").all_runners()
        assert len(runners) == 50 and len(asked) == 2

    def test_a_failed_page_is_none(self, monkeypatch):
        def route(path, query, headers):
            if query["page"] == "2":
                return OSError("down")
            return _Answer([{"uuid": f"u{n}"} for n in range(50)])
        fake(monkeypatch, route)
        assert forgejo_api.Forgejo("https://git.example", "t").all_runners() is None
