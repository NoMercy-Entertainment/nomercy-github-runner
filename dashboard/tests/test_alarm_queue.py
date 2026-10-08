"""An alarm when a job waits ten minutes and no online runner can take it
(GitHub #11).

The other half of 2026-09-30: jobs that asked for `xcode` sat in the queue
because the one runner with that label was offline. A job is matched the
way GitHub matches it - every label it asks for is one the runner has,
whatever the case - against the runners online right now. A job asking for
labels no self-hosted runner has ever carried is a GitHub-hosted runner's
job (ubuntu-latest) and none of ours. The check reads every repository of
the org, so it is throttled, asks conditionally, and stops when the token's
hourly budget runs low.
"""
import pytest

import alarms
from control import audit
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0
ORG = "NoMercy-Entertainment"
XCODE = ["self-hosted", "macOS", "xcode"]


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "control.db")
    schema.init(p)
    return p


def cfg(**over):
    return alarms.settings(over)


def job(jid=7, labels=XCODE, since=T0, repo=f"{ORG}/app"):
    return {"id": jid, "repo": repo, "workflow": "Build", "name": "ios",
            "labels": list(labels), "since": alarms.iso(since),
            "url": f"https://github.com/{repo}/actions/runs/1/job/{jid}", "run_id": 1}


def seen(path, *labels):
    """A self-hosted runner that has carried these labels, offline now."""
    alarms.AlarmBook(path).observe_runners(
        "github", [{"id": 99, "name": "mac", "online": False, "labels": list(labels)}],
        T0 - 60 * MIN, cfg())


class TestMatching:
    def test_every_label_must_be_on_one_runner(self):
        online = [{"self-hosted", "linux", "x64"}, {"self-hosted", "macos"}]
        assert alarms.can_run(["self-hosted", "Linux"], online)
        assert not alarms.can_run(["self-hosted", "macOS", "xcode"], online)
        assert not alarms.can_run(["linux", "macos"], online), "not split across two"

    def test_case_does_not_matter(self):
        assert alarms.can_run(["Self-Hosted", "XCODE"], [{"self-hosted", "xcode"}])

    def test_a_label_no_self_hosted_runner_ever_had_is_hosted(self):
        known = {"self-hosted", "linux", "x64", "macos", "xcode"}
        assert alarms.hosted(["ubuntu-latest"], known)
        assert alarms.hosted(["macos-14-xlarge"], known)
        assert not alarms.hosted(["Linux", "X64"], known)

    def test_a_job_that_says_self_hosted_is_never_hosted(self):
        assert not alarms.hosted(["self-hosted", "gpu"], {"linux"})


class TestTheQueueAlarm:
    def test_raised_when_queued_ten_minutes_with_no_runner(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [{"self-hosted", "linux"}],
                        T0 + 9 * MIN, cfg())
        assert not any(r["raised_at"] for r in b.rows() if r["kind"] == "job_queued")
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [{"self-hosted", "linux"}],
                        T0 + 10 * MIN, cfg())
        [alarm] = [r for r in b.rows() if r["kind"] == "job_queued"]
        assert alarm["raised_at"] == alarms.iso(T0 + 10 * MIN)
        assert alarm["since"] == alarms.iso(T0), "timed from GitHub's own queue time"
        assert alarm["detail"]["repo"] == f"{ORG}/app"
        assert alarm["detail"]["workflow"] == "Build" and alarm["detail"]["job"] == "ios"
        assert alarm["detail"]["labels"] == XCODE
        assert alarm["detail"]["url"].endswith("/job/7")
        assert "xcode" in alarm["message"] and f"{ORG}/app" in alarm["message"]
        assert audit.entries(path, verb="alarm", decision="raised")

    def test_a_job_some_online_runner_can_take_is_only_waiting_its_turn(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"},
                        [{"self-hosted", "macos", "xcode"}], T0 + 30 * MIN, cfg())
        assert [r for r in b.rows() if r["kind"] == "job_queued"] == []

    def test_a_hosted_runners_job_is_skipped(self, path):
        seen(path, "self-hosted", "linux")
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job(labels=["ubuntu-latest"])], {f"{ORG}/app"}, [],
                        T0 + 30 * MIN, cfg())
        assert [r for r in b.rows() if r["kind"] == "job_queued"] == []

    def test_a_job_no_longer_queued_resolves(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [], T0 + 10 * MIN, cfg())
        b.observe_queue("github", [], {f"{ORG}/app"}, [], T0 + 15 * MIN, cfg())
        assert [r for r in b.rows() if r["kind"] == "job_queued"] == []
        assert "no longer queued" in audit.entries(path, verb="alarm",
                                                   decision="resolved")[0]["outcome"]

    def test_a_repository_that_was_not_read_resolves_nothing(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [], T0 + 10 * MIN, cfg())
        b.observe_queue("github", [], set(), [], T0 + 15 * MIN, cfg())
        assert len([r for r in b.rows() if r["kind"] == "job_queued"]) == 1

    def test_a_runner_coming_online_resolves_it_between_sweeps(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [], T0 + 10 * MIN, cfg())
        b.recheck_queue("github", [{"self-hosted", "macos", "xcode"}], T0 + 11 * MIN, cfg())
        assert [r for r in b.rows() if r["kind"] == "job_queued"] == []

    def test_a_recheck_raises_on_time_between_sweeps(self, path):
        seen(path, *XCODE)
        b = alarms.AlarmBook(path)
        b.observe_queue("github", [job()], {f"{ORG}/app"}, [], T0 + 6 * MIN, cfg())
        b.recheck_queue("github", [], T0 + 10 * MIN, cfg())
        [alarm] = [r for r in b.rows() if r["kind"] == "job_queued"]
        assert alarm["raised_at"] == alarms.iso(T0 + 10 * MIN)


class FakeGitHub:
    """The four reads the queue sweep makes, and what they cost."""

    def __init__(self, repos, runs=None, jobs=None, remaining=4000, reset=None):
        self.org = ORG
        self.repos, self.runs, self.jobs = repos, runs or {}, jobs or {}
        self.rate_remaining, self.rate_reset = remaining, reset
        self.calls = []
        self.spend = 0

    def _pay(self):
        if self.rate_remaining is not None:
            self.rate_remaining -= self.spend

    def org_repos(self):
        self.calls.append(("repos",))
        self._pay()
        return self.repos

    def waiting_runs(self, repo):
        self.calls.append(("runs", repo))
        self._pay()
        return self.runs.get(repo, [])

    def run_jobs(self, repo, run_id):
        self.calls.append(("jobs", repo, run_id))
        self._pay()
        return self.jobs.get(run_id, [])

    def all_runners(self):
        self.calls.append(("runners",))
        return [{"id": 99, "name": "mac", "status": "offline", "labels": XCODE}]


def monitor_with(client):
    return alarms.Monitor(clients=lambda forge, env: client if forge == "github" else None)


def queued_run(run_id=1, created=T0):
    return {"id": run_id, "name": "Build", "created_at": alarms.iso(created)}


def queued_job(jid=7, created=T0, labels=XCODE, status="queued"):
    return {"id": jid, "name": "ios", "status": status, "labels": labels,
            "created_at": alarms.iso(created), "workflow_name": "Build",
            "html_url": f"https://github.com/{ORG}/app/actions/runs/1/job/{jid}"}


class TestTheSweep:
    ENV = {"GH_TOKEN": "t", "GITHUB_ORG": ORG}

    def sweep(self, path, client, now, env=None):
        m = monitor_with(client)
        m.tick(path, env or self.ENV, [], now)
        m.sweep_queue(path, env or self.ENV, now)
        return m

    def test_a_queued_job_with_no_runner_is_found(self, path):
        client = FakeGitHub([f"{ORG}/app"], {f"{ORG}/app": [queued_run()]},
                            {1: [queued_job(), queued_job(8, status="in_progress")]})
        self.sweep(path, client, T0 + 10 * MIN)
        [alarm] = [r for r in alarms.AlarmBook(path).rows() if r["kind"] == "job_queued"]
        assert alarm["alarm_key"] == "queue:github:7" and alarm["raised_at"]

    def test_a_run_too_young_to_cross_the_threshold_is_not_read(self, path):
        client = FakeGitHub([f"{ORG}/app"], {f"{ORG}/app": [queued_run(created=T0)]})
        self.sweep(path, client, T0 + 2 * MIN)
        assert ("jobs", f"{ORG}/app", 1) not in client.calls

    def test_the_repository_list_is_kept_for_an_hour(self, path):
        client = FakeGitHub([f"{ORG}/app"])
        m = monitor_with(client)
        m.tick(path, self.ENV, [], T0)
        m.sweep_queue(path, self.ENV, T0)
        m.sweep_queue(path, self.ENV, T0 + 30 * MIN)
        assert client.calls.count(("repos",)) == 1
        m.sweep_queue(path, self.ENV, T0 + 61 * MIN)
        assert client.calls.count(("repos",)) == 2

    def test_it_stops_below_the_rate_limit_floor_until_the_reset(self, path):
        repos = [f"{ORG}/r{n}" for n in range(10)]
        client = FakeGitHub(repos, remaining=1003, reset=int(T0 + 40 * MIN))
        client.spend = 1
        m = monitor_with(client)
        m.tick(path, self.ENV, [], T0)
        m.sweep_queue(path, self.ENV, T0)
        read = [c for c in client.calls if c[0] == "runs"]
        assert len(read) < len(repos), "stopped part way"
        assert m.status["queue"]["paused_until"] == alarms.iso(T0 + 40 * MIN)
        before = len(client.calls)
        m.sweep_queue(path, self.ENV, T0 + 10 * MIN)
        assert len(client.calls) == before, "nothing asked while paused"
        client.rate_remaining = 5000
        m.sweep_queue(path, self.ENV, T0 + 41 * MIN)
        assert len(client.calls) > before

    def test_a_secondary_limit_stops_the_sweep_for_retry_after(self, path):
        repos = [f"{ORG}/r{n}" for n in range(10)]
        client = FakeGitHub(repos)
        original = client.waiting_runs

        def waiting_runs(repo):
            if repo == f"{ORG}/r2":
                client.throttled = 120
                return None
            return original(repo)
        client.waiting_runs = waiting_runs
        client.throttled = None
        m = monitor_with(client)
        m.tick(path, self.ENV, [], T0)
        m.sweep_queue(path, self.ENV, T0)
        asked = [c for c in client.calls if c[0] == "runs"]
        assert len(asked) == 2, "nothing after the refusal"
        assert m.status["queue"]["paused_until"] == alarms.iso(T0 + 120)
        assert client.throttled is None, "taken note of once"
        before = len(client.calls)
        m.sweep_queue(path, self.ENV, T0 + 60)
        assert len(client.calls) == before
        assert [r for r in alarms.AlarmBook(path).rows() if r["kind"] == "monitor"] == [], \
            "a limit is a pause, not a blind monitor"

    def test_a_secondary_limit_without_retry_after_waits_minutes(self, path):
        client = FakeGitHub([f"{ORG}/a", f"{ORG}/b"])
        original = client.waiting_runs

        def waiting_runs(repo):
            client.throttled = 0
            return None
        client.waiting_runs = waiting_runs
        client.throttled = None
        m = monitor_with(client)
        m.tick(path, self.ENV, [], T0)
        m.sweep_queue(path, self.ENV, T0)
        assert m.status["queue"]["paused_until"] == alarms.iso(T0 + alarms.THROTTLE_SECONDS)
        assert original

    def test_the_floor_is_configurable(self, path):
        client = FakeGitHub([f"{ORG}/app"], remaining=150)
        env = dict(self.ENV, ALARM_RATE_LIMIT_FLOOR="100")
        self.sweep(path, client, T0, env)
        assert ("runs", f"{ORG}/app") in client.calls

    def test_no_runner_reading_means_no_judgement(self, path):
        client = FakeGitHub([f"{ORG}/app"], {f"{ORG}/app": [queued_run()]},
                            {1: [queued_job()]})
        client.all_runners = lambda: None
        self.sweep(path, client, T0 + 10 * MIN)
        assert ("repos",) not in client.calls

    def test_an_unreadable_repository_list_says_the_monitor_is_blind(self, path):
        client = FakeGitHub(None)
        self.sweep(path, client, T0)
        [row] = [r for r in alarms.AlarmBook(path).rows() if r["kind"] == "monitor"]
        assert row["alarm_key"] == "monitor:github:queue"
        assert "queued jobs" in row["message"]

    def test_an_unreadable_repository_is_not_swept(self, path):
        book = alarms.AlarmBook(path)
        seen(path, *XCODE)
        book.observe_queue("github", [job()], {f"{ORG}/app"}, [], T0 + 10 * MIN, cfg())
        client = FakeGitHub([f"{ORG}/app"])
        client.waiting_runs = lambda repo: None
        self.sweep(path, client, T0 + 15 * MIN)
        assert [r["alarm_key"] for r in book.rows() if r["kind"] == "job_queued"] \
            == ["queue:github:7"]
