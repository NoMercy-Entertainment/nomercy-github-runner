"""Acknowledging an alarm (GitHub #11).

A runner nobody here manages can be off on purpose for days - someone's
Mac on holiday - and its alarm would stay red the whole time. An admin can
acknowledge it: it stays listed, greyed and folded away with who and when,
nothing more is sent about it, and the acknowledgement goes with the alarm
when it resolves - the next outage of that runner is a new alarm.
"""
import pytest

import alarm_notify
import alarms
from control import audit
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0
KEY = "runner:github:5"


def runner(online=False):
    return {"id": 5, "name": "nomercy-mac-mini", "online": online,
            "labels": ["self-hosted", "macOS", "xcode"]}


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "control.db")
    schema.init(p)
    monkeypatch.setattr(alarms, "LISTENERS", [alarm_notify.enqueue])
    return p


def raise_it(path, at=T0):
    book = alarms.AlarmBook(path)
    book.observe_runners("github", [runner()], at, alarms.settings({}))
    book.observe_runners("github", [runner()], at + 10 * MIN, alarms.settings({}))
    return book


class TestTheBook:
    def test_acknowledged_with_who_and_when(self, path):
        book = raise_it(path)
        assert book.acknowledge(KEY, "phil@example", T0 + 20 * MIN)
        [row] = book.rows()
        assert row["acked_by"] == "phil@example"
        assert row["acked_at"] == alarms.iso(T0 + 20 * MIN)
        [entry] = audit.entries(path, verb="alarm", decision="acknowledged")
        assert entry["actor"] == "phil@example" and "nomercy-mac-mini" in entry["outcome"]

    def test_an_alarm_that_is_not_there_is_not_acknowledged(self, path):
        assert not alarms.AlarmBook(path).acknowledge("runner:github:404", "phil", T0)

    def test_it_clears_when_the_alarm_resolves(self, path):
        book = raise_it(path)
        book.acknowledge(KEY, "phil", T0 + 20 * MIN)
        book.observe_runners("github", [runner(online=True)], T0 + 30 * MIN,
                             alarms.settings({}))
        raise_it(path, T0 + 60 * MIN)
        [row] = book.rows()
        assert row["raised_at"] and row["acked_at"] is None, "a new outage is a new alarm"

    def test_only_its_resolve_is_still_sent(self, path):
        """The owner's choice (2026-10-08): acknowledging quiets an alarm, but
        "it is back" is still worth hearing."""
        sent = []

        def post(url, body, headers):
            sent.append(body)
            return True, None
        env = {"ALARM_WEBHOOK_URL": "https://ntfy.example/t"}
        book = raise_it(path)
        alarm_notify.deliver_due(path, env, T0 + 10 * MIN, post=post)
        book.acknowledge(KEY, "phil", T0 + 20 * MIN)
        book.observe_runners("github", [runner(online=True)], T0 + 30 * MIN,
                             alarms.settings({}))
        alarm_notify.deliver_due(path, env, T0 + 31 * MIN, post=post)
        assert len(sent) == 2 and sent[0].decode().startswith("ALARM")
        assert sent[1].decode().startswith("RESOLVED")

    def test_a_raise_still_waiting_to_be_sent_is_not_sent(self, path):
        sent = []
        book = raise_it(path)
        book.acknowledge(KEY, "phil", T0 + 10 * MIN)
        alarm_notify.deliver_due(path, {"ALARM_WEBHOOK_URL": "https://ntfy.example/t"},
                                 T0 + 11 * MIN, post=lambda *a: sent.append(a) or (True, None))
        assert sent == []


@pytest.fixture
def raised(path, monkeypatch):
    import api_v2
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    m = alarms.Monitor(clients=lambda f, env: None)
    raise_it(path)
    m.publish(alarms.AlarmBook(path), alarms.settings({}))
    monkeypatch.setattr(alarms, "MONITOR", m)
    return path


def as_role(role):
    import users
    users.approve("sub-test-admin", role)


class TestTheAction:
    def test_an_admin_may(self, client, raised):
        r = client.post(f"/api/v2/alarms/{KEY}/ack", json={})
        assert r.status_code == 200 and r.json["ok"]
        view = client.get("/api/v2/alarms").json
        assert view["alarms"] == []
        [acked] = view["acknowledged"]
        assert acked["alarm_key"] == KEY and acked["acked_by"]

    @pytest.mark.parametrize("role", ["operator", "viewer"])
    def test_nobody_else_may(self, client, raised, role):
        as_role(role)
        assert client.post(f"/api/v2/alarms/{KEY}/ack", json={}).status_code == 403
        assert audit.entries(raised, verb="alarm", decision="acknowledged") == []

    def test_an_unknown_alarm_is_404(self, client, raised):
        assert client.post("/api/v2/alarms/runner:github:404/ack", json={}).status_code == 404

    def test_the_banner_script_folds_it_away_too(self, client, raised):
        import json
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            pytest.skip("node runs the real page JavaScript")
        client.post(f"/api/v2/alarms/{KEY}/ack", json={})
        source = (Path(alarms.__file__).parent / "templates" / "base.html").read_text(
            encoding="utf-8")
        script = (source[source.index("function alarmBanner("):source.index("// end alarmBanner")]
                  + "\nconsole.log(JSON.stringify(alarmBanner("
                  + json.dumps(client.get("/api/v2/alarms").json) + ", true)));")
        out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                        encoding="utf-8", check=True).stdout)
        assert out["count"] == 0 and out["acked"] == 1
        assert "<details><summary>1 acknowledged</summary>" in out["html"]
        assert "Acknowledge</button>" not in out["html"]

    def test_the_banner_folds_it_away_grey(self, client, raised):
        client.post(f"/api/v2/alarms/{KEY}/ack", json={})
        html = client.get("/").get_data(as_text=True)
        banner = html[html.index('id="alarm-banner"'):]
        banner = banner[:banner.index("</section>")]
        assert "alarm-banner quiet" in banner
        assert "<details" in banner and "1 acknowledged" in banner
        assert "nomercy-mac-mini" in banner and "acknowledged by" in banner
