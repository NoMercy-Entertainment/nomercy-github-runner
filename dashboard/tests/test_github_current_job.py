"""A busy GitHub card names its job from GitHub when the hook is late.

GitHub knows which job a runner took the moment it takes it; the runner's
job-started hook only runs after "Set up job" - on the macOS guest 24 s
later (2026-10-27, health on github-macos-x64-1: GitHub 05:47:42, hook
05:48:06). Until then the card said "runner is busy - job details
unavailable". The page asks GitHub, off the request path, and shows what it
says - never a job GitHub still calls running that the runner already
reported finished.
"""
from types import SimpleNamespace

import api_v2
import cards
import github_api


def _client(monkeypatch, runs, jobs):
    client = github_api.GitHub.__new__(github_api.GitHub)
    seen = []

    def fake_get(path, params=None, conditional=False):
        seen.append(path)
        if path.endswith("/actions/runs"):
            return {"workflow_runs": runs.get(path.split("/actions")[0][len("/repos/"):], [])}
        run_id = int(path.split("/runs/")[1].split("/")[0])
        return {"jobs": jobs.get(run_id, [])}

    monkeypatch.setattr(client, "_get", fake_get, raising=False)
    monkeypatch.setattr(client, "repos", lambda *a, **k: ["o/a", "o/b"], raising=False)
    return client, seen


def test_current_jobs_finds_each_runners_running_job(monkeypatch):
    client, seen = _client(monkeypatch,
                           runs={"o/b": [{"id": 7, "name": "CI"}]},
                           jobs={7: [
                               {"runner_name": "gh-1", "status": "completed", "name": "old",
                                "started_at": "2026-10-27T05:00:00Z"},
                               {"runner_name": "gh-1", "status": "in_progress", "name": "health",
                                "started_at": "2026-10-27T05:47:42Z", "workflow_name": "Health",
                                "html_url": "u"},
                               {"runner_name": "other", "status": "in_progress", "name": "x",
                                "started_at": "2026-10-27T05:47:42Z"}]})

    found = client.current_jobs({"gh-1", "gh-2"})

    assert found == {"gh-1": {"job": "health", "repo": "o/b", "workflow": "Health",
                              "started_at": "2026-10-27T05:47:42Z", "url": "u"}}
    assert "/repos/o/a/actions/runs" in seen and "/repos/o/b/actions/runs/7/jobs" in seen


def _busy_spec(**telemetry):
    return {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "provider": "github",
            "platform": "macos", "architecture": "x64", "host_id": "mac-1",
            "display_name": "github-macos-x64-1", "actual_state": "busy",
            "forge_state": "busy", "unit_state": "running",
            "last_seen_at": api_v2._now_iso(), "forge_seen_at": api_v2._now_iso(),
            "capabilities": {}, "fleet_id": "github-macos-x64", "telemetry": telemetry}


def _service():
    service = SimpleNamespace(env={}, buildable=lambda f: (True, None),
                              validate_replacement=lambda s: None,
                              _drains_first=lambda *a: False)
    return service


def test_an_unnamed_busy_card_takes_githubs_name(monkeypatch):
    api_v2._github_jobs.update(jobs={"github-macos-x64-1": {
        "job": "health", "started_at": "2026-10-27T05:47:42Z"}}, at=api_v2.time.monotonic())
    monkeypatch.setattr(api_v2, "_refresh_github_jobs", lambda service: None)
    card = api_v2._runner_card(_service(), _busy_spec(), True, host={})
    assert card["job"] == "health"


def test_a_job_the_runner_already_finished_is_not_shown(monkeypatch):
    """GitHub may still call the old job running for a moment after the
    runner's hook said it finished."""
    api_v2._github_jobs.update(jobs={"github-macos-x64-1": {
        "job": "health", "started_at": "2026-10-27T05:47:42Z"}}, at=api_v2.time.monotonic())
    monkeypatch.setattr(api_v2, "_refresh_github_jobs", lambda service: None)
    finished = {"state": "finished", "job": "health", "run_id": "1",
                "at": "2026-10-27T05:49:41.864050+00:00", "source": "worker"}
    card = api_v2._runner_card(_service(), _busy_spec(job_event=finished), True, host={})
    assert card["job"] != "health"


def test_an_old_answer_is_not_used(monkeypatch):
    api_v2._github_jobs.update(jobs={"github-macos-x64-1": {
        "job": "health", "started_at": "2026-10-27T05:47:42Z"}},
        at=api_v2.time.monotonic() - 10 * api_v2.GITHUB_JOBS_FRESH)
    asked = []
    monkeypatch.setattr(api_v2, "_refresh_github_jobs", lambda service: asked.append(1))
    card = api_v2._runner_card(_service(), _busy_spec(), True, host={})
    assert card["job"] == cards.UNNAMED_JOB
    assert asked == [1]


def test_a_newer_github_job_replaces_finishing(monkeypatch):
    finished = {"state": "finished", "job": "health", "run_id": "1",
                "at": "2026-10-27T05:49:41.864050+00:00", "source": "worker"}
    api_v2._github_jobs.update(jobs={"github-macos-x64-1": {
        "job": "next", "started_at": "2026-10-27T05:49:50Z"}}, at=api_v2.time.monotonic())
    monkeypatch.setattr(api_v2, "_refresh_github_jobs", lambda service: None)
    spec = _busy_spec(job_event=finished,
                      job_event_received_at="2026-10-27T05:49:42+00:00")
    card = api_v2._runner_card(_service(), spec, True, host={})
    assert card["job"] == "next"


class _Tasks:
    def __init__(self):
        self.calls = []

    def task_info(self, repo, task_id):
        self.calls.append((repo, task_id))
        return {"name": "Desktop · windows-2022", "workflow": "build.yml"}


def test_a_forgejo_event_job_is_shown_by_its_task_name(monkeypatch):
    """The patched Forgejo runner names a job by its key ("windows"); its
    task has the name the forge shows."""
    tasks = _Tasks()
    monkeypatch.setattr(api_v2.providers, "by_key", lambda key: SimpleNamespace(
        forge_client=lambda env: tasks))
    api_v2._task_name_cache.clear()
    started = {"state": "started", "job": "windows", "job_id": "1534", "run_id": "989",
               "repo": "FiLL/deepseek-bridge", "at": api_v2._now_iso(), "source": "worker"}
    spec = dict(_busy_spec(job_event=started, job_event_received_at=api_v2._now_iso(),
                           at=api_v2._now_iso()),
                provider="forgejo", display_name="forgejo-windows-x64-1")
    card = api_v2._runner_card(_service(), spec, True, host={})
    assert card["job"] == "FiLL/deepseek-bridge · build.yml / Desktop · windows-2022"
    assert tasks.calls == [("FiLL/deepseek-bridge", 1534)]
    finished = dict(started, state="finished")
    spec["telemetry"] = {"job_event": finished, "job_event_received_at": api_v2._now_iso(),
                         "at": api_v2._now_iso()}
    card = api_v2._runner_card(_service(), spec, True, host={})
    assert card["job"] == "FiLL/deepseek-bridge · build.yml / Desktop · windows-2022 - finishing"
