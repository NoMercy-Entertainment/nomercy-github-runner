"""An alarm when a self-hosted runner is offline too long (GitHub #11).

On 2026-09-30 the org's only xcode runner, a machine this platform does not
manage, was offline for more than two hours and nobody noticed. These tests
hold the monitor that would have said so: every runner either forge lists,
managed or not, timed from the first poll that saw it offline - a time kept
in control.db, so a restart neither forgets it nor starts it again - raised
at ten minutes, resolved when the runner returns or is removed. A forge
that cannot be read says the monitor is blind; it never raises or resolves
an alarm about a runner it could not see.
"""
import pytest

import alarms
from control import audit
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "control.db")
    schema.init(p)
    return p


def book(path):
    return alarms.AlarmBook(path)


def cfg(**over):
    return alarms.settings(over)


def gh(rid, status="offline", name=None, labels=("self-hosted", "macOS", "xcode")):
    return {"id": rid, "name": name or f"runner-{rid}", "online": status != "offline",
            "labels": list(labels)}


def raised(path):
    return [a for a in book(path).rows() if a["raised_at"]]


class TestSettings:
    def test_defaults(self):
        c = alarms.settings({})
        assert (c["offline_minutes"], c["queue_minutes"], c["poll_seconds"],
                c["queue_poll_seconds"]) == (10, 10, 60, 300)

    def test_each_is_configurable_and_nonsense_is_the_default(self):
        c = alarms.settings({"ALARM_OFFLINE_MINUTES": "3", "ALARM_QUEUE_MINUTES": "x",
                             "ALARM_POLL_SECONDS": "-5", "ALARM_QUEUE_POLL_SECONDS": "600"})
        assert (c["offline_minutes"], c["queue_minutes"], c["poll_seconds"],
                c["queue_poll_seconds"]) == (3, 10, 60, 600)


class TestTheOfflineTimer:
    def test_not_raised_before_ten_minutes(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg())
        b.observe_runners("github", [gh(1)], T0 + 9 * MIN, cfg())
        assert raised(path) == []
        assert b.rows()[0]["since"] == alarms.iso(T0)

    def test_raised_at_ten_minutes(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1, name="nomercy-mac-mini")], T0, cfg())
        b.observe_runners("github", [gh(1, name="nomercy-mac-mini")], T0 + 10 * MIN, cfg())
        [alarm] = raised(path)
        assert alarm["kind"] == "runner_offline" and alarm["subject"] == "nomercy-mac-mini"
        assert alarm["raised_at"] == alarms.iso(T0 + 10 * MIN)
        assert "nomercy-mac-mini" in alarm["message"] and "xcode" in alarm["message"]

    def test_the_threshold_is_configurable(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg(ALARM_OFFLINE_MINUTES="2"))
        b.observe_runners("github", [gh(1)], T0 + 2 * MIN, cfg(ALARM_OFFLINE_MINUTES="2"))
        assert len(raised(path)) == 1

    def test_coming_back_resolves_it(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg())
        b.observe_runners("github", [gh(1)], T0 + 11 * MIN, cfg())
        b.observe_runners("github", [gh(1, "online")], T0 + 12 * MIN, cfg())
        assert b.rows() == []

    def test_a_blip_shorter_than_the_threshold_leaves_nothing(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg())
        b.observe_runners("github", [gh(1, "online")], T0 + 2 * MIN, cfg())
        b.observe_runners("github", [gh(1)], T0 + 3 * MIN, cfg())
        b.observe_runners("github", [gh(1)], T0 + 12 * MIN, cfg())
        assert raised(path) == [], "the timer starts again after the runner came back"
        assert audit.entries(path, verb="alarm") == []

    def test_a_runner_removed_from_the_forge_resolves(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1), gh(2, "online")], T0, cfg())
        b.observe_runners("github", [gh(1), gh(2, "online")], T0 + 10 * MIN, cfg())
        b.observe_runners("github", [gh(2, "online")], T0 + 11 * MIN, cfg())
        assert b.rows() == []
        resolved = audit.entries(path, verb="alarm", decision="resolved")
        assert "removed" in resolved[0]["outcome"]

    def test_the_forges_are_kept_apart(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg())
        b.observe_runners("forgejo", [{"id": "uuid-1", "name": "fj", "online": True,
                                       "labels": []}], T0 + MIN, cfg())
        assert [r["alarm_key"] for r in b.rows()] == ["runner:github:1"]

    def test_a_runner_stopped_on_purpose_is_not_an_alarm(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1)], T0, cfg(), suppressed={"1"})
        b.observe_runners("github", [gh(1)], T0 + 30 * MIN, cfg(), suppressed={"1"})
        assert b.rows() == []


class TestARestartRemembers:
    def test_the_first_seen_time_survives(self, path):
        book(path).observe_runners("github", [gh(1)], T0, cfg())
        # A new process: nothing in memory, the same database.
        book(path).observe_runners("github", [gh(1)], T0 + 10 * MIN, cfg())
        [alarm] = raised(path)
        assert alarm["since"] == alarms.iso(T0)

    def test_a_raised_alarm_is_not_raised_twice(self, path):
        book(path).observe_runners("github", [gh(1)], T0, cfg())
        book(path).observe_runners("github", [gh(1)], T0 + 10 * MIN, cfg())
        book(path).observe_runners("github", [gh(1)], T0 + 20 * MIN, cfg())
        assert len(audit.entries(path, verb="alarm", decision="raised")) == 1
        assert raised(path)[0]["raised_at"] == alarms.iso(T0 + 10 * MIN)


class TestTheAuditLog:
    def test_raised_and_resolved_are_recorded(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1, name="nomercy-mac-mini")], T0, cfg())
        b.observe_runners("github", [gh(1, name="nomercy-mac-mini")], T0 + 10 * MIN, cfg())
        b.observe_runners("github", [gh(1, "online", name="nomercy-mac-mini")],
                          T0 + 140 * MIN, cfg())
        rows = audit.entries(path, verb="alarm")
        assert [r["decision"] for r in rows] == ["resolved", "raised"]
        assert all(r["actor"] == "alarm-monitor" for r in rows)
        assert "nomercy-mac-mini" in rows[1]["outcome"]
        assert "back online" in rows[0]["outcome"]


class TestABlindMonitorSaysSo:
    def test_a_forge_that_cannot_be_read_degrades_the_monitor(self, path):
        b = book(path)
        b.observe_runners("github", None, T0, cfg(), why="HTTP 502")
        [row] = b.rows()
        assert row["kind"] == "monitor" and row["forge"] == "github"
        assert "cannot reach GitHub" in row["message"]

    def test_it_raises_no_runner_alarm_and_resolves_none(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1), gh(2)], T0, cfg())
        b.observe_runners("github", [gh(1), gh(2)], T0 + 10 * MIN, cfg())
        b.observe_runners("github", None, T0 + 11 * MIN, cfg())
        keys = {r["alarm_key"] for r in raised(path)}
        assert {"runner:github:1", "runner:github:2"} <= keys, \
            "an unreadable list is not every runner removed"

    def test_reading_again_recovers(self, path):
        b = book(path)
        b.observe_runners("github", None, T0, cfg())
        b.observe_runners("github", [], T0 + MIN, cfg())
        assert b.rows() == []

    def test_blind_for_the_threshold_is_itself_an_alarm(self, path):
        b = book(path)
        b.observe_runners("github", None, T0, cfg())
        b.observe_runners("github", None, T0 + 10 * MIN, cfg())
        assert raised(path)[0]["kind"] == "monitor"


class TestTheLabelsEverSeen:
    def test_every_label_a_self_hosted_runner_had_is_kept(self, path):
        b = book(path)
        b.observe_runners("github", [gh(1, labels=("self-hosted", "XCode"))], T0, cfg())
        b.observe_runners("github", [], T0 + MIN, cfg())
        assert b.known_labels("github") == {"self-hosted", "xcode"}
