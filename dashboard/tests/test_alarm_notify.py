"""An alarm sent somewhere a person will see it (GitHub #11).

Optional: without ALARM_WEBHOOK_URL there is the banner and nothing else.
With it, each alarm is sent once when it is raised and once when it is
resolved - an outbox row per event, unique, so a retry or a restart never
sends twice - in the shape the receiver reads (ntfy, Discord, Slack or
plain JSON). A send that fails is tried again later, with a growing pause,
and the URL, which is a credential, is never written to a log.
"""
import json

import pytest

import alarm_notify
import alarms
from control.secrets import NAMES, SecretStore
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0
HOOK = "https://ntfy.example/very-secret-topic"


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "control.db")
    schema.init(p)
    monkeypatch.setattr(alarms, "LISTENERS", [alarm_notify.enqueue])
    return p


class Post:
    def __init__(self, answers=None):
        self.sent = []
        self.answers = list(answers or [])

    def __call__(self, url, body, headers):
        self.sent.append((url, body, headers))
        return self.answers.pop(0) if self.answers else (True, None)


def offline_for(path, minutes, now=T0, name="nomercy-mac-mini"):
    book = alarms.AlarmBook(path)
    runner = {"id": 5, "name": name, "online": False,
              "labels": ["self-hosted", "macOS", "xcode"]}
    book.observe_runners("github", [runner], now, alarms.settings({}))
    book.observe_runners("github", [runner], now + minutes * MIN, alarms.settings({}))
    return book


def back_online(path, now, name="nomercy-mac-mini"):
    alarms.AlarmBook(path).observe_runners(
        "github", [{"id": 5, "name": name, "online": True, "labels": []}], now,
        alarms.settings({}))


EVENT = {"event": "raised", "key": "runner:github:5", "kind": "runner_offline",
         "forge": "github", "subject": "nomercy-mac-mini",
         "detail": {"labels": ["self-hosted", "xcode"]},
         "since": "2026-09-30T10:00:00Z", "raised_at": "2026-09-30T10:10:00Z",
         "resolved_at": None, "reason": None,
         "message": "GitHub runner nomercy-mac-mini has been offline since "
                    "2026-09-30T10:00:00Z (labels: self-hosted, xcode)"}


class TestFormats:
    def test_ntfy_is_the_text_with_a_title_and_priority(self):
        body, headers = alarm_notify.render("ntfy", EVENT)
        assert body.decode() == "ALARM: " + EVENT["message"]
        assert headers["Title"] == "Runner alarm: nomercy-mac-mini"
        assert headers["Priority"] == "urgent" and headers["Tags"] == "rotating_light"
        resolved = dict(EVENT, event="resolved", reason="back online")
        body, headers = alarm_notify.render("ntfy", resolved)
        assert body.decode().startswith("RESOLVED: ")
        assert headers["Priority"] == "default" and headers["Tags"] == "white_check_mark"

    def test_ntfy_headers_survive_a_name_http_cannot_carry(self):
        _, headers = alarm_notify.render("ntfy", dict(EVENT, subject="ジョブ"))
        headers["Title"].encode("latin-1")

    def test_a_job_link_is_the_click_target(self):
        event = dict(EVENT, kind="job_queued", detail={"url": "https://github.com/x/y/job/7"})
        assert alarm_notify.render("ntfy", event)[1]["Click"] == "https://github.com/x/y/job/7"
        assert "https://github.com/x/y/job/7" in json.loads(
            alarm_notify.render("slack", event)[0])["text"]

    def test_discord_is_an_embed_like_the_journal_posts_and_mentions_no_one(self):
        """The owner wants alarms laid out as the "Shipping in the Dark" posts
        are: a coloured card with a title, a sentence and named fields, the
        time shown by Discord in the reader's own time zone."""
        body, headers = alarm_notify.render("discord", dict(EVENT, subject="@everyone"))
        data = json.loads(body)
        assert headers["Content-Type"] == "application/json"
        assert data["allowed_mentions"] == {"parse": []}
        [embed] = data["embeds"]
        assert embed["title"] == "Runner offline: @everyone"
        assert embed["color"] == alarm_notify.RED
        assert embed["timestamp"] == "2026-09-30T10:00:00Z"
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Forge"] == "GitHub"
        assert fields["Labels"] == "self-hosted, xcode"
        assert fields["Offline since"] == "30-09-2026 12:00 CEST"
        assert embed["footer"]["text"].startswith("NoMercy runners")

    def test_discord_resolve_is_green_and_says_how_long(self):
        event = dict(EVENT, event="resolved", resolved_at="2026-09-30T12:15:00Z",
                     reason="back online")
        [embed] = json.loads(alarm_notify.render("discord", event)[0])["embeds"]
        assert embed["title"] == "Back online: nomercy-mac-mini"
        assert embed["color"] == alarm_notify.GREEN
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Down for"] == "2 h 15 min"
        assert fields["Back since"] == "30-09-2026 14:15 CEST"

    def test_discord_queued_job_links_to_the_run(self):
        event = dict(EVENT, kind="job_queued", key="job:github:1",
                     subject="nomercy-app-kmp / ci / android",
                     detail={"labels": ["self-hosted", "xcode"],
                             "url": "https://github.com/o/r/actions/runs/1"})
        [embed] = json.loads(alarm_notify.render("discord", event)[0])["embeds"]
        assert embed["title"] == "Job waiting: nomercy-app-kmp / ci / android"
        assert embed["url"] == "https://github.com/o/r/actions/runs/1"
        assert {f["name"] for f in embed["fields"]} >= {"Labels", "Queued since"}

    def test_discord_fields_fit_discord_limits(self):
        long = dict(EVENT, subject="x" * 400, detail={"labels": ["l" * 50] * 40})
        [embed] = json.loads(alarm_notify.render("discord", long)[0])["embeds"]
        assert len(embed["title"]) <= 256 and len(embed["description"]) <= 4096
        assert all(len(f["value"]) <= 1024 for f in embed["fields"])

    def test_slack_text(self):
        assert json.loads(alarm_notify.render("slack", EVENT)[0]) == {
            "text": "ALARM: " + EVENT["message"]}

    @pytest.mark.parametrize("mention", ["<!channel>", "<!here>", "<!everyone>",
                                         "<@U024BE7LH>", "<!subteam^SAZ94GDB8>"])
    def test_slack_mentions_are_text(self, mention):
        event = dict(EVENT, message=f"runner {mention} & co has been offline")
        text = json.loads(alarm_notify.render("slack", event)[0])["text"]
        assert "<" not in text and ">" not in text
        assert "&lt;" in text and "&amp; co" in text

    def test_json_carries_the_whole_alarm(self):
        data = json.loads(alarm_notify.render("json", EVENT)[0])
        assert data["event"] == "raised"
        assert data["alarm"]["key"] == "runner:github:5"
        assert data["alarm"]["since"] == "2026-09-30T10:00:00Z"
        assert data["alarm"]["detail"]["labels"] == ["self-hosted", "xcode"]

    @pytest.mark.parametrize("url,fmt", [
        ("https://discord.com/api/webhooks/1/abc", "discord"),
        ("https://hooks.slack.com/services/T/B/x", "slack"),
        ("https://ntfy.sh/topic", "ntfy"),
        ("https://example.com/hook", "json"),
    ])
    def test_the_format_follows_the_url_unless_named(self, url, fmt):
        assert alarm_notify.format_of({}, url) == fmt
        assert alarm_notify.format_of({"ALARM_WEBHOOK_FORMAT": "Slack"}, url) == "slack"
        assert alarm_notify.format_of({"ALARM_WEBHOOK_FORMAT": "telegram"}, url) == fmt


class TestOnceEach:
    def test_raised_once_and_resolved_once(self, path):
        post = Post()
        env = {"ALARM_WEBHOOK_URL": HOOK}
        offline_for(path, 10)
        offline_for(path, 20)
        alarm_notify.deliver_due(path, env, T0 + 20 * MIN, post=post)
        alarm_notify.deliver_due(path, env, T0 + 21 * MIN, post=post)
        assert len(post.sent) == 1 and post.sent[0][1].decode().startswith("ALARM: ")
        back_online(path, T0 + 30 * MIN)
        alarm_notify.deliver_due(path, env, T0 + 30 * MIN, post=post)
        alarm_notify.deliver_due(path, env, T0 + 31 * MIN, post=post)
        assert len(post.sent) == 2 and post.sent[1][1].decode().startswith("RESOLVED: ")
        assert "back online" in post.sent[1][1].decode()

    def test_the_same_event_enqueued_twice_is_one_row(self, path):
        alarm_notify.enqueue(path, EVENT)
        alarm_notify.enqueue(path, EVENT)
        with schema.connect(path) as c:
            assert c.execute("SELECT COUNT(*) FROM alarm_outbox").fetchone()[0] == 1

    def test_a_condition_that_never_raised_sends_nothing(self, path):
        post = Post()
        offline_for(path, 3)
        back_online(path, T0 + 4 * MIN)
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": HOOK}, T0 + 5 * MIN, post=post)
        assert post.sent == []


class TestRetry:
    def test_a_failed_send_waits_longer_each_time_then_gives_up(self, path):
        post = Post([(False, "HTTP 502")] * 20)
        env = {"ALARM_WEBHOOK_URL": HOOK}
        alarm_notify.enqueue(path, EVENT, now=T0)
        alarm_notify.deliver_due(path, env, T0, post=post)
        assert len(post.sent) == 1
        alarm_notify.deliver_due(path, env, T0 + 10, post=post)
        assert len(post.sent) == 1, "not before its pause"
        alarm_notify.deliver_due(path, env, T0 + 31, post=post)
        assert len(post.sent) == 2
        alarm_notify.deliver_due(path, env, T0 + 31 + 59, post=post)
        assert len(post.sent) == 2, "the second pause is longer"
        now = T0 + 31 + 61
        for _ in range(20):
            alarm_notify.deliver_due(path, env, now, post=post)
            now += 3600
        assert len(post.sent) == alarm_notify.MAX_ATTEMPTS
        with schema.connect(path) as c:
            row = c.execute("SELECT state, last_error FROM alarm_outbox").fetchone()
        assert tuple(row) == ("failed", "HTTP 502")

    def test_a_send_that_succeeds_on_retry_is_sent(self, path):
        post = Post([(False, "URLError")])
        env = {"ALARM_WEBHOOK_URL": HOOK}
        alarm_notify.enqueue(path, EVENT, now=T0)
        alarm_notify.deliver_due(path, env, T0, post=post)
        alarm_notify.deliver_due(path, env, T0 + 60, post=post)
        alarm_notify.deliver_due(path, env, T0 + 3600, post=post)
        assert len(post.sent) == 2

    def test_the_url_is_never_logged(self, path, capsys, monkeypatch):
        import urllib.error
        import urllib.request

        def urlopen(req, timeout=None):
            raise urllib.error.URLError(f"cannot reach {req.full_url}")
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        alarm_notify.enqueue(path, EVENT, now=T0)
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": HOOK}, T0)
        out = capsys.readouterr()
        assert "very-secret-topic" not in out.out + out.err
        with schema.connect(path) as c:
            assert "very-secret-topic" not in (
                c.execute("SELECT last_error FROM alarm_outbox").fetchone()[0])


class TestDeliveryHasItsOwnThread:
    """Up to fifty sends of ten seconds each must never sit between two
    runner checks - nor trip "the monitor has stopped checking"."""

    def test_the_runner_thread_sends_nothing(self, path, monkeypatch):
        assert not hasattr(alarms, "TICK_LISTENERS")
        called = []
        monkeypatch.setattr(alarm_notify, "deliver_due", lambda *a, **k: called.append(a))
        m = alarms.Monitor(clients=lambda forge, env: None)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            raise SystemExit
        with pytest.raises(SystemExit):
            alarms.run_forever(lambda: (path, {}, []), monitor=m, sleep=sleep,
                               clock=lambda: T0)
        assert called == [] and m.checked_at == T0

    def test_the_delivery_loop_sends_and_survives_a_failure(self, path, monkeypatch):
        calls = []

        def deliver(p, env, now, post=None):
            calls.append((p, env, now))
            if len(calls) == 1:
                raise RuntimeError("receiver hung up")
        monkeypatch.setattr(alarm_notify, "deliver_due", deliver)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                raise SystemExit
        env = {"ALARM_WEBHOOK_URL": HOOK}
        with pytest.raises(SystemExit):
            alarm_notify.run_forever(lambda: (path, env, []), sleep=sleep, clock=lambda: T0)
        assert calls == [(path, env, T0), (path, env, T0)]
        assert slept == [alarm_notify.DELIVERY_SECONDS] * 2

    def test_the_dashboard_starts_it(self):
        from pathlib import Path
        source = (Path(alarm_notify.__file__).parent / "app.py").read_text(encoding="utf-8")
        assert "target=alarm_notify.run_forever" in source


class TestUnsetIsBannerOnly:
    def test_nothing_is_sent_and_nothing_waits_to_be(self, path):
        post = Post()
        offline_for(path, 10)
        alarm_notify.deliver_due(path, {}, T0 + 10 * MIN, post=post)
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": HOOK}, T0 + 11 * MIN, post=post)
        assert post.sent == [], "an alarm raised with no webhook is not sent late"


class TestTheSecret:
    def test_the_url_is_a_secret_the_store_holds(self, path):
        assert "ALARM_WEBHOOK_URL" in NAMES
        store = SecretStore(path)
        store.set("ALARM_WEBHOOK_URL", HOOK, "admin")
        assert store.overlay({})["ALARM_WEBHOOK_URL"] == HOOK
        assert [s for s in store.status() if s["name"] == "ALARM_WEBHOOK_URL"][0]["set"]

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x",
                                     "javascript:alert(1)", "ntfy.sh/topic", "https://"])
    def test_only_an_http_url_is_saved(self, path, url):
        from control.secrets import SecretRefused
        with pytest.raises(SecretRefused):
            SecretStore(path).set("ALARM_WEBHOOK_URL", url, "admin")

    def test_only_an_http_url_is_sent_to(self, path, monkeypatch, capsys):
        import urllib.request
        opened = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))
        alarm_notify.enqueue(path, EVENT, now=T0)
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": "file:///etc/secret-hook"}, T0)
        assert opened == []
        with schema.connect(path) as c:
            row = c.execute("SELECT state, last_error FROM alarm_outbox").fetchone()
        assert row["state"] == "skipped" and "http" in row["last_error"]
        assert "secret-hook" not in capsys.readouterr().out

    def test_its_value_is_masked_like_a_token(self):
        from control import redact
        assert redact.secret_values({"ALARM_WEBHOOK_URL": HOOK}) == [HOOK]


class TestLocalTime:
    """Alarms read in the owner's own time, Europe/Amsterdam: CEST (UTC+2) in
    summer, CET (UTC+1) in winter - not UTC, which read two hours behind."""

    @pytest.mark.parametrize("utc,local", [
        ("2026-10-09T00:31:41Z", "09-10-2026 02:31 CEST"),
        ("2026-12-01T12:00:00Z", "01-12-2026 13:00 CET"),
        ("2026-10-25T00:59:00Z", "25-10-2026 02:59 CEST"),
        ("2026-10-25T01:00:00Z", "25-10-2026 02:00 CET"),
        ("2026-03-29T00:59:00Z", "29-03-2026 01:59 CET"),
        ("2026-03-29T01:00:00Z", "29-03-2026 03:00 CEST"),
    ])
    def test_amsterdam_with_and_without_a_time_zone_database(self, utc, local, monkeypatch):
        assert alarms.local_time(utc) == local
        monkeypatch.setattr(alarms, "_zone", lambda name: None)
        assert alarms.local_time(utc) == local

    def test_the_message_reads_in_local_time(self):
        text = alarms.message("runner_offline", "github", "github-windows-arm64-1",
                              {"labels": ["self-hosted", "ARM64"]}, "2026-10-09T00:31:41Z")
        assert "09-10-2026 02:31 CEST" in text and "Z" not in text.split("since", 1)[1]

    def test_an_unreadable_time_is_shown_as_it_came(self):
        assert alarms.local_time("not a time") == "not a time"
