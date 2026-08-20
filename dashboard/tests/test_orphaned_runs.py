"""A job killed mid-flight must not stay "running" forever.

The closer keys on the runner's own "Job ... completed with result:" log line.
A job that is SIGTERMed, restarted or recreated out from under never writes
that line, so its row keeps ended_at NULL and the UI renders it as running for
good. Two such rows survived a month in production. The rule that resolves
them: a run that began before its container's current start cannot still be
running.
"""
import docker_ops
import history

# Verbatim from `docker inspect -f '{{.State.StartedAt}}' github-runner-7`.
REAL_STARTED_AT = "2026-08-20T14:23:37.714612450Z\n"


def test_started_at_normalises_dockers_stamp_to_whole_seconds(monkeypatch):
    """history stores '...:33Z'; a nanosecond stamp must compare against it."""
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, REAL_STARTED_AT, ""))
    assert docker_ops.started_at("github-runner-7") == "2026-08-20T14:23:37Z"


def test_started_at_is_empty_when_the_container_is_gone(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (False, "", "No such object"))
    assert docker_ops.started_at("github-runner-9") == ""


def _run(runner):
    rows = history.list_runs(runner=runner, limit=10)
    assert len(rows) == 1
    return rows[0]


def test_a_run_open_from_before_the_restart_is_closed_as_interrupted():
    history.init()
    name = "orphan-test-1"
    history.open_run(name, None, "build-base / docker-build",
                     "2026-08-20T13:27:33Z")

    history.close_interrupted(name, "2026-08-20T14:23:37Z")

    row = _run(name)
    assert row["ended_at"] == "2026-08-20T14:23:37Z"
    assert row["result"] == "Interrupted"
    assert row["duration_s"] == 3364        # 13:27:33 -> 14:23:37


def test_a_job_still_running_on_this_container_is_left_alone():
    history.init()
    name = "orphan-test-2"
    history.open_run(name, None, "deploy", "2026-08-20T15:00:00Z")

    history.close_interrupted(name, "2026-08-20T14:23:37Z")

    assert _run(name)["ended_at"] is None


def test_an_already_closed_run_is_not_rewritten():
    history.init()
    name = "orphan-test-3"
    history.open_run(name, None, "deploy", "2026-08-20T13:00:00Z")
    history.close_run(name, "deploy", "2026-08-20T13:05:00Z", "Succeeded")

    history.close_interrupted(name, "2026-08-20T14:23:37Z")

    row = _run(name)
    assert row["result"] == "Succeeded"
    assert row["ended_at"] == "2026-08-20T13:05:00Z"


def test_the_collector_closes_a_run_its_runner_outlived(monkeypatch):
    """The wiring: a poll is what actually notices, without an operator."""
    import app as dash

    history.init()
    name = "orphan-test-4"
    history.open_run(name, None, "build-base / docker-build",
                     "2026-08-20T13:27:33Z")

    monkeypatch.setattr(dash.ops, "logs_since", lambda *a, **k: "")
    monkeypatch.setattr(dash.ops, "started_at",
                        lambda n: "2026-08-20T14:23:37Z")

    dash._record_history({"runners": [{"name": name, "state": "idle"}]})

    assert _run(name)["result"] == "Interrupted"


def test_a_poll_costs_no_extra_docker_call_when_nothing_is_open(monkeypatch):
    """Guard the 5s poll: inspect only runs when there is a row to resolve."""
    import app as dash

    history.init()
    calls = []
    monkeypatch.setattr(dash.ops, "logs_since", lambda *a, **k: "")
    monkeypatch.setattr(dash.ops, "started_at", lambda n: calls.append(n) or "")

    dash._record_history({"runners": [{"name": "orphan-test-5",
                                       "state": "idle"}]})

    assert calls == []
