"""Discord hears every runner coming and going, and every job (2026-10-27).

The owner's ask: online and offline status for every runner - timely, but
not for a restart or a blip, and a stop or recreate by the controller says
so instead of raising an alarm - and a message for every job when it starts
and when it ends, with its real result, on both forges and all platforms.
Each message is sent once, also across restarts (the outbox is unique on
key, event and start), compactly, and a Discord 429 is waited out.
"""
import json

import pytest

import alarm_notify
import alarms
import history
import job_notify
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "control.db")
    schema.init(p)
    monkeypatch.setattr(alarms, "LISTENERS", [alarm_notify.enqueue])
    return p


def runner(name, online, rid=None, labels=("self-hosted", "linux")):
    return {"id": rid or name, "name": name, "online": online, "labels": list(labels)}


def outbox(path):
    with schema.connect(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM alarm_outbox ORDER BY id")]


def events(path):
    return [(r["event"], json.loads(r["payload"])) for r in outbox(path)]


# ---------------------------------------------------------------------------
# runners: online and offline
# ---------------------------------------------------------------------------

class TestRunnerStatus:
    def observe(self, path, runners, now, planned=None):
        alarms.AlarmBook(path).observe_runners("github", runners, now,
                                               alarms.settings({}), planned=planned or {})

    def test_the_debounce_is_two_minutes(self):
        assert alarms.settings({})["offline_minutes"] == 2

    def test_offline_two_minutes_is_said_and_back_online_too(self, path):
        self.observe(path, [runner("gh-1", False)], T0)
        self.observe(path, [runner("gh-1", False)], T0 + 2 * MIN)
        self.observe(path, [runner("gh-1", True)], T0 + 5 * MIN)
        got = [(e, p["kind"], p["subject"]) for e, p in events(path)]
        assert got == [("raised", "runner_offline", "gh-1"),
                       ("resolved", "runner_offline", "gh-1")]

    def test_a_blip_says_nothing(self, path):
        self.observe(path, [runner("gh-1", False)], T0)
        self.observe(path, [runner("gh-1", True)], T0 + 90)
        assert outbox(path) == []

    def test_a_recreate_says_recreating_not_an_alarm(self, path):
        """The old registration goes; the runner is absent from the forge
        while the controller rebuilds it; a new registration of the same
        name comes back online."""
        planned = {"gh-1": "recreating"}
        self.observe(path, [runner("gh-1", False, rid=11)], T0, planned)
        self.observe(path, [], T0 + 2 * MIN, dict(planned, **{"__expected__": ["gh-1"]}))
        self.observe(path, [runner("gh-1", True, rid=12)], T0 + 9 * MIN)
        got = [(e, p["kind"], p["detail"].get("why")) for e, p in events(path)]
        assert got == [("raised", "runner_planned", "recreating"),
                       ("resolved", "runner_planned", "recreating")]
        assert events(path)[1][1]["reason"] == "back online"

    def test_a_planned_stop_is_not_on_the_alarm_banner(self, path):
        self.observe(path, [runner("gh-1", False)], T0, {"gh-1": "stopped by operator"})
        self.observe(path, [runner("gh-1", False)], T0 + 3 * MIN, {"gh-1": "stopped by operator"})
        monitor = alarms.Monitor()
        monitor.publish(alarms.AlarmBook(path), alarms.settings({}))
        view = monitor.view(T0 + 3 * MIN)
        assert view["alarms"] == []
        assert [p["detail"]["why"] for _, p in events(path)] == ["stopped by operator"]

    def test_still_offline_after_the_operation_ended_is_an_alarm(self, path):
        planned = {"gh-1": "recreating"}
        self.observe(path, [runner("gh-1", False)], T0, planned)
        self.observe(path, [runner("gh-1", False)], T0 + 3 * MIN, planned)
        self.observe(path, [runner("gh-1", False)], T0 + 4 * MIN)
        self.observe(path, [runner("gh-1", False)], T0 + 6 * MIN)
        kinds = [(e, p["kind"]) for e, p in events(path)]
        assert kinds == [("raised", "runner_planned"), ("raised", "runner_offline")]

    def test_a_platform_runner_missing_from_the_forge_is_offline(self, path):
        expected = {"__expected__": ["gh-1"]}
        self.observe(path, [], T0, expected)
        self.observe(path, [], T0 + 2 * MIN, expected)
        assert [(e, p["kind"], p["subject"]) for e, p in events(path)] == [
            ("raised", "runner_offline", "gh-1")]

    def test_two_registrations_of_one_name_are_one_runner(self, path):
        self.observe(path, [runner("gh-1", False, rid=1), runner("gh-1", True, rid=2)], T0)
        self.observe(path, [runner("gh-1", False, rid=1), runner("gh-1", True, rid=2)], T0 + 5 * MIN)
        assert outbox(path) == []


class TestPlannedReasons:
    def spec(self, **kw):
        base = {"provider": "github", "display_name": "gh-1", "registration_id": "11",
                "desired_state": "running", "actual_state": "idle",
                "current_operation": None, "deleted_at": None}
        base.update(kw)
        return base

    def test_the_operation_names_the_reason(self):
        verbs = {"op-1": "recreate", "op-2": "stop", "op-3": "restart"}
        planned = alarms.planned("github", [
            self.spec(display_name="a", current_operation="op-1"),
            self.spec(display_name="b", current_operation="op-2"),
            self.spec(display_name="c", current_operation="op-3"),
            self.spec(display_name="d", desired_state="stopped"),
            self.spec(display_name="e")], verbs)
        assert planned["a"] == "recreating" and planned["b"] == "stopped by operator"
        assert planned["c"] == "restarting" and planned["d"] == "stopped by operator"
        assert "e" not in planned
        assert set(planned["__expected__"]) == {"a", "b", "c", "d", "e"}

    def test_a_retired_runner_is_not_expected(self):
        planned = alarms.planned("github", [self.spec(deleted_at="2026-10-27T00:00:00Z")], {})
        assert planned.get("__expected__") == []


# ---------------------------------------------------------------------------
# rendering and delivery
# ---------------------------------------------------------------------------

JOB = {"event": "finished", "key": "job:github:7", "kind": "job", "forge": "github",
       "repo": "NoMercy-Entertainment/app", "workflow": "CI", "job": "build",
       "runner": "github-linux-x64-3", "url": "https://github.com/x/actions/runs/1/job/2",
       "result": "Succeeded", "duration_s": 83, "started_at": "2026-10-27T08:00:00Z",
       "ended_at": "2026-10-27T08:01:23Z", "raised_at": "2026-10-27T08:00:00Z"}


class TestRendering:
    def test_a_planned_stop_is_grey_names_its_reason_and_tags_no_one(self):
        event = {"event": "raised", "key": "runner:github:gh-1", "kind": "runner_planned",
                 "forge": "github", "subject": "gh-1", "detail": {"why": "recreating"},
                 "since": "2026-10-27T08:00:00Z", "raised_at": "2026-10-27T08:02:00Z",
                 "message": "x"}
        env = {"ALARM_DISCORD_PEOPLE": "Fill=270700469943271424", "ALARM_DISCORD_DEFAULT": "Fill"}
        body = json.loads(alarm_notify.render("discord", event, env)[0])
        assert body["embeds"][0]["title"] == "Recreating: gh-1"
        assert body["embeds"][0]["color"] == alarm_notify.GREY
        assert "content" not in body

    @pytest.mark.parametrize("result,colour", [("Succeeded", "GREEN"), ("Failed", "RED"),
                                               ("Canceled", "GREY")])
    def test_a_finished_job_carries_result_duration_and_colour(self, result, colour):
        embed = alarm_notify.job_embed(dict(JOB, result=result))
        assert embed["title"] == f"{result}: build"
        assert embed["color"] == getattr(alarm_notify, colour)
        assert "NoMercy-Entertainment/app · CI" in embed["description"]
        assert "github-linux-x64-3" in embed["description"] and "1m 23s" in embed["description"]
        assert embed["url"] == JOB["url"]

    def test_a_started_job_says_where_and_links(self):
        embed = alarm_notify.job_embed(dict(JOB, event="started", result=None))
        assert embed["title"] == "Started: build"
        assert embed["color"] == alarm_notify.BLUE
        assert "GitHub · github-linux-x64-3" in embed["description"]

    def test_job_text_for_the_other_formats(self):
        assert alarm_notify.text_of(JOB).startswith("Succeeded: build")


class Post:
    def __init__(self, answers=None):
        self.sent, self.answers = [], list(answers or [])

    def __call__(self, url, body, headers):
        self.sent.append(json.loads(body))
        return self.answers.pop(0) if self.answers else (True, None)


HOOK = "https://discord.com/api/webhooks/1/abc"
LATER = alarms.epoch("2026-10-28T00:00:00Z")


class TestDelivery:
    def enqueue_jobs(self, path, n):
        for i in range(n):
            alarm_notify.enqueue(path, dict(JOB, key=f"job:github:{i}"))

    def test_job_messages_go_ten_to_a_post(self, path):
        self.enqueue_jobs(path, 12)
        post = Post()
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": HOOK}, LATER, post=post, pace=0)
        assert [len(p["embeds"]) for p in post.sent] == [10, 2]
        assert {r["state"] for r in outbox(path)} == {"sent"}

    def test_a_429_is_waited_out_and_is_not_a_failed_attempt(self, path):
        self.enqueue_jobs(path, 3)
        post = Post([(False, "HTTP 429", 7.5)])
        now = LATER
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": HOOK}, now, post=post, pace=0)
        rows = outbox(path)
        assert {r["state"] for r in rows} == {"pending"}
        assert {r["attempts"] for r in rows} == {0}
        assert all(alarms.epoch(r["next_at"]) >= now + 7 for r in rows)

    def test_runner_messages_can_be_turned_off(self, path):
        job_notify.set_settings(path, runners=False)
        alarm_notify.enqueue(path, {"event": "raised", "key": "runner:github:gh-1",
                                    "kind": "runner_offline", "forge": "github",
                                    "subject": "gh-1", "since": "2026-10-27T08:00:00Z",
                                    "raised_at": "2026-10-27T08:02:00Z", "message": "x"})
        assert outbox(path) == []


# ---------------------------------------------------------------------------
# jobs: from the run history to the outbox
# ---------------------------------------------------------------------------

@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()
    job_notify._details_cache.clear()      # run ids repeat across fresh databases

    def add(started, ended=None, result=None, provider="github", job="build",
            runner="github-linux-x64-3"):
        with history._lock, history._conn() as c:
            return c.execute(
                "INSERT INTO runs (runner, registration, job_name, started_at, ended_at,"
                " duration_s, result, provider, runner_id, gh_checked, gh_run_id, gh_repo,"
                " forge_task_id) VALUES (?,?,?,?,?,?,?,?,?,1,4242,'o/app',1535)",
                ("uuid-" + runner, runner, job, started, ended,
                 83 if ended else None, result, provider, "uuid-" + runner)).lastrowid

    def finish(rid, ended, result):
        with history._lock, history._conn() as c:
            c.execute("UPDATE runs SET ended_at=?, result=?, duration_s=83 WHERE id=?",
                      (ended, result, rid))
    add.finish = finish
    return add


NOW = alarms.epoch("2026-10-27T09:00:00Z")


class Details:
    def __init__(self):
        self.calls = 0

    def __call__(self, env, run):
        self.calls += 1
        return {"workflow": "CI", "job": "Build app", "url": "https://x/job/1"}


def scan(path, now=NOW, details=None):
    return job_notify.scan(path, {}, now, details=details or Details())


class TestJobMessages:
    def test_a_job_is_said_when_it_starts_and_when_its_result_is_known(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z")
        rid = runs("2026-10-27T08:59:00.5Z")
        scan(path)
        runs.finish(rid, "2026-10-27T09:00:20.1Z", None)
        scan(path, NOW + 30)                      # ended, result not known yet
        runs.finish(rid, "2026-10-27T09:00:20.1Z", "Failed")
        scan(path, NOW + 60)
        scan(path, NOW + 90)                      # and never twice
        got = [(e, p["job"], p.get("result")) for e, p in events(path)]
        assert got == [("started", "Build app", None), ("finished", "Build app", "Failed")]
        assert events(path)[1][1]["duration_s"] == 83

    def test_a_restart_does_not_post_again(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z")
        runs("2026-10-27T08:59:00.5Z", "2026-10-27T08:59:30.5Z", "Succeeded")
        scan(path)
        job_notify._details_cache.clear()        # a new process knows nothing
        scan(path, NOW + 30)
        assert [e for e, _ in events(path)] == ["started", "finished"]

    def test_jobs_before_the_messages_began_are_not_posted(self, path, runs):
        runs("2026-10-27T08:00:00Z", "2026-10-27T08:01:00Z", "Succeeded")
        scan(path)                                # first scan: starts from now
        assert outbox(path) == []

    def test_a_job_long_over_is_said_once_with_its_result(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T06:00:00Z")
        runs("2026-10-27T07:00:00.5Z", "2026-10-27T07:10:00.5Z", "Succeeded")
        scan(path)
        assert [e for e, _ in events(path)] == ["finished"]

    def test_failures_only(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z", jobs="failures")
        runs("2026-10-27T08:59:00.5Z", "2026-10-27T08:59:30.5Z", "Succeeded", job="a")
        runs("2026-10-27T08:59:01.5Z", "2026-10-27T08:59:30.5Z", "Failed", job="b")
        scan(path)
        sent = [(e, p["result"]) for e, p in events(path) if True]
        assert sent == [("finished", "Failed")]

    def test_off_posts_nothing(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z", jobs="off")
        runs("2026-10-27T08:59:00.5Z", "2026-10-27T08:59:30.5Z", "Failed")
        scan(path)
        assert outbox(path) == []

    def test_both_forges(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z")
        runs("2026-10-27T08:59:00.5Z", "2026-10-27T08:59:30.5Z", "Succeeded",
             provider="forgejo", runner="forgejo-windows-x64-1")
        scan(path)
        finished = [p for e, p in events(path) if e == "finished"][0]
        assert finished["forge"] == "forgejo" and finished["runner"] == "forgejo-windows-x64-1"

    def test_a_lookup_that_fails_still_posts_what_is_known(self, path, runs):
        job_notify.set_settings(path, since="2026-10-27T08:00:00Z")
        runs("2026-10-27T08:59:00.5Z")

        def broken(env, run):
            raise OSError("no network")
        job_notify.scan(path, {}, NOW, details=broken)
        started = events(path)[0][1]
        assert started["job"] == "build" and started["repo"] == "o/app"

    def test_the_defaults_are_all_on(self, path):
        assert job_notify.get_settings(path)["jobs"] == "all"
        assert job_notify.get_settings(path)["runners"] is True


# ---------------------------------------------------------------------------
# the Settings page
# ---------------------------------------------------------------------------

@pytest.fixture
def plane(path, monkeypatch):
    import api_v2
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    return path


class TestTheSettings:
    def test_an_admin_reads_and_sets_them(self, client, plane):
        assert client.get("/api/v2/notifications").json["jobs"] == "all"
        r = client.post("/api/v2/notifications", json={"jobs": "failures", "runners": False})
        assert r.status_code == 200
        assert job_notify.get_settings(plane)["jobs"] == "failures"
        assert job_notify.get_settings(plane)["runners"] is False

    def test_nonsense_is_refused(self, client, plane):
        assert client.post("/api/v2/notifications", json={"jobs": "some"}).status_code == 400

    @pytest.mark.parametrize("role", ["operator", "viewer"])
    def test_nobody_else_may_change_them(self, client, plane, role):
        import users
        users.approve("sub-test-admin", role)
        assert client.post("/api/v2/notifications", json={"jobs": "off"}).status_code == 403
        assert job_notify.get_settings(plane)["jobs"] == "all"

    def test_the_page_has_them(self):
        from pathlib import Path
        page = (Path(alarms.__file__).parent / "templates" / "settings_v2.html").read_text(
            encoding="utf-8")
        assert 'id="notify-jobs"' in page and 'id="notify-runners"' in page

    def test_the_dashboard_starts_the_job_thread(self):
        from pathlib import Path
        source = (Path(alarms.__file__).parent / "app.py").read_text(encoding="utf-8")
        assert "job_notify.run_forever" in source
