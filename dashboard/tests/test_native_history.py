"""Durable projection carries native exact results without forge enrichment."""
from datetime import datetime, timezone

import history
import pytest

from native_history import NativeHistory

RID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def projector(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "history.db"))
    history.init()
    return NativeHistory(str(tmp_path / "control.db"))


def spec(provider="github"):
    return {"runner_id": RID, "display_name": "github-test", "provider": provider}


def event(sequence, state="started", **values):
    return {"sequence": sequence, "runner_id": RID, "provider": "github", "source": "worker",
            "received_at": datetime.now(timezone.utc).isoformat(), "state": state,
            "at": "2026-10-12T01:00:00+00:00", "job": "verify", "repo": "owner/repo",
            "commit": "a" * 40, "run_id": "123", "run_attempt": "1", "job_id": None,
            "result": None, **values}


def test_native_history_replay_is_idempotent_and_survives_reopen(projector):
    projector.record(spec(), event(1))
    projector.record(spec(), event(1))
    reopened = NativeHistory(projector.path)
    assert reopened.sequence() == 1
    reopened.record(spec(), event(2, "finished", result="success", at="2026-10-12T01:02:00+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 1
    assert runs[0]["result"] == "Succeeded"
    assert runs[0]["duration_s"] == 120
    assert runs[0]["gh_repo"] == "owner/repo"
    assert not history.pending_enrichment()


def test_signed_job_binds_hook_identity_and_preserves_exact_result(projector):
    projector.record(spec(), event(1))
    projector.record(spec(), event(2, source="forge", job="CI / verify (Windows)", job_id="456"))
    projector.record(spec(), event(3, "finished", source="forge", job="CI / verify (Windows)", job_id="456",
                                   result="failure", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(4, "finished", at="2026-10-12T01:02:01+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 1
    assert runs[0]["job_name"] == "CI / verify (Windows)"
    assert runs[0]["result"] == "Failed"
    assert runs[0]["gh_conclusion"] == "failure"


def test_delayed_signed_start_binds_already_completed_local_hook(projector):
    projector.record(spec(), event(1))
    projector.record(spec(), event(2, "finished", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(3, source="forge", job="CI / verify", job_id="456"))
    projector.record(spec(), event(4, "finished", source="forge", job="CI / verify", job_id="456",
                                   result="cancelled", at="2026-10-12T01:02:00+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 1
    assert runs[0]["result"] == "Canceled"


def test_forgejo_native_result_avoids_result_api_enrichment(projector):
    projector.record(spec("forgejo"), event(1, provider="forgejo", job_id="99"))
    projector.record(spec("forgejo"), event(2, "finished", provider="forgejo", job_id="99", result="failure"))
    run = history.list_runs(runner_id=RID)[0]
    assert run["forge_task_id"] == 99
    assert run["result"] == "Failed"
    assert not history.pending_enrichment()


def test_future_worker_event_does_not_poison_history_but_cursor_advances(projector):
    projector.record(spec(), event(1, at="9999-01-01T00:00:00+00:00"))
    assert not history.list_runs(runner_id=RID)
    assert projector.sequence() == 1
    projector.record(spec(), event(2))
    assert len(history.list_runs(runner_id=RID)) == 1


def test_incomplete_worker_evidence_does_not_create_phantom_history(projector):
    projector.record(spec(), event(1, source="forge", job_id="456"))
    projector.record(spec(), event(2, job=None, run_id=None, job_id="hook:" + "a" * 32))
    projector.record(spec(), event(3, "finished", job=None, run_id=None, job_id="hook:" + "a" * 32))
    assert projector.sequence() == 3
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 1 and runs[0]["job_name"] == "verify"
    assert not runs[0]["ended_at"]
    projector.record(spec(), event(4, "finished", source="forge", job_id="456", result="success"))
    assert history.list_runs(runner_id=RID)[0]["result"] == "Succeeded"


def test_deleted_runner_identity_is_retained_when_projection_replays(projector):
    projector.record(None, event(1, "finished", source="forge", result="success"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 1
    assert runs[0]["runner_id"] == RID
    assert runs[0]["result"] == "Succeeded"


def test_distinct_numeric_jobs_with_same_name_run_and_attempt_stay_separate(projector):
    projector.record(spec(), event(1, source="forge", job_id="456", job="matrix"))
    projector.record(spec(), event(2, "finished", source="forge", job_id="456", job="matrix", result="success",
                                   at="2026-10-12T01:00:00.100000+00:00"))
    projector.record(spec(), event(3, source="forge", job_id="789", job="matrix",
                                   at="2026-10-12T01:00:00.200000+00:00"))
    projector.record(spec(), event(4, "finished", source="forge", job_id="789", job="matrix", result="failure",
                                   at="2026-10-12T01:00:00.300000+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 2  # even two same-name starts in the same second
    assert {run["result"] for run in runs} == {"Succeeded", "Failed"}


def test_sequential_same_hook_slug_binds_distinct_signed_jobs(projector):
    projector.record(spec(), event(1, job_id="hook:first"))
    projector.record(spec(), event(2, source="forge", job_id="456", job="matrix (one)"))
    projector.record(spec(), event(3, "finished", job_id="hook:first", at="2026-10-12T01:01:00+00:00"))
    projector.record(spec(), event(4, "finished", source="forge", job_id="456", job="matrix (one)", result="success",
                                   at="2026-10-12T01:01:00+00:00"))
    projector.record(spec(), event(5, job_id="hook:second", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(6, source="forge", job_id="789", job="matrix (two)", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(7, "finished", job_id="hook:second", at="2026-10-12T01:03:00+00:00"))
    projector.record(spec(), event(8, "finished", source="forge", job_id="789", job="matrix (two)", result="failure",
                                   at="2026-10-12T01:03:00+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 2
    assert {run["job_name"]: run["result"] for run in runs} == {"matrix (one)": "Succeeded", "matrix (two)": "Failed"}


def test_delayed_signed_events_bind_completed_hooks_by_execution_interval(projector):
    projector.record(spec(), event(1, job_id="hook:first"))
    projector.record(spec(), event(2, "finished", job_id="hook:first", at="2026-10-12T01:01:00+00:00"))
    projector.record(spec(), event(3, job_id="hook:second", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(4, "finished", job_id="hook:second", at="2026-10-12T01:03:00+00:00"))
    projector.record(spec(), event(5, source="forge", job_id="456", job="matrix (one)"))
    projector.record(spec(), event(6, "finished", source="forge", job_id="456", job="matrix (one)", result="success",
                                   at="2026-10-12T01:01:00+00:00"))
    projector.record(spec(), event(7, source="forge", job_id="789", job="matrix (two)", at="2026-10-12T01:02:00+00:00"))
    projector.record(spec(), event(8, "finished", source="forge", job_id="789", job="matrix (two)", result="failure",
                                   at="2026-10-12T01:03:00+00:00"))
    runs = history.list_runs(runner_id=RID)
    assert len(runs) == 2
    assert {run["job_name"]: (run["result"], run["duration_s"]) for run in runs} == {
        "matrix (one)": ("Succeeded", 60), "matrix (two)": ("Failed", 60)}
    assert all(run["started_at"] < run["ended_at"] for run in runs)


def test_ambiguous_cross_kind_alias_is_retained_as_separate_unknown_history(projector):
    projector.record(spec(), event(1, job_id="hook:first", at="2026-10-12T01:00:10+00:00"))
    projector.record(spec(), event(2, "finished", job_id="hook:first", at="2026-10-12T01:00:11+00:00"))
    projector.record(spec(), event(3, job_id="hook:second", at="2026-10-12T01:00:20+00:00"))
    projector.record(spec(), event(4, "finished", job_id="hook:second", at="2026-10-12T01:00:21+00:00"))
    projector.record(spec(), event(5, source="forge", job_id="456", job="matrix"))
    assert len(history.list_runs(runner_id=RID)) == 3
    with history._conn() as connection:
        rows = connection.execute("SELECT history_id FROM native_job_ids WHERE job_id='456'").fetchall()
        assert len(rows) == 1
        aliases = {row[0] for row in connection.execute("SELECT job_id FROM native_job_ids WHERE history_id=?", (rows[0][0],))}
        assert aliases == {"456"}  # neither opaque execution was guessed
