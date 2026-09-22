"""A job killed mid-flight must not stay "running" forever.

The closer keys on the runner's own "Job ... completed with result:" log line.
A job that is SIGTERMed, restarted or recreated out from under never writes
that line, so its row keeps ended_at NULL and the UI renders it as running for
good. Two such rows survived a month in production. The rule that resolves
them: a run that began before its container's current start cannot still be
running.

The caller that noticed - the local-engine collector - is gone (T-8). The
rule itself is not: it is history's, it is keyed on timestamps rather than
on anything Docker-shaped, and the rows it resolves are still in the
database.
"""
import history


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
