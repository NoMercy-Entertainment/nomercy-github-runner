"""Alarms shown where nobody can miss them (GitHub #11).

A red banner at the top of every page while any alarm is active, with no
way to close it - an alarm leaves when its condition does - and the same
answer at /api/v2/alarms for anyone signed in, viewers included. Both read
what the monitor last published; neither ever reaches a forge, so a page is
as fast with GitHub down as with GitHub up.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import alarms
from store import schema

T0 = 1_790_000_000.0
MIN = 60.0
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


class FakeForge:
    def __init__(self, records):
        self.records, self.asked = records, 0

    def all_runners(self):
        self.asked += 1
        return self.records


MAC = {"id": 5, "name": "nomercy-mac-mini", "status": "offline",
       "labels": ["self-hosted", "macOS", "xcode"]}


@pytest.fixture
def raised(tmp_path, monkeypatch):
    """A monitor that has seen the Mac offline for ten minutes."""
    path = str(tmp_path / "control.db")
    schema.init(path)
    forge = FakeForge([MAC])
    m = alarms.Monitor(clients=lambda f, env: forge if f == "github" else None)
    m.tick(path, {}, [], T0)
    m.tick(path, {}, [], T0 + 10 * MIN)
    monkeypatch.setattr(alarms, "MONITOR", m)
    return forge


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(alarms, "MONITOR", alarms.Monitor(clients=lambda f, env: None))


def as_role(role):
    import users
    users.approve("sub-test-admin", role)


class TestTheBanner:
    @pytest.mark.parametrize("page", ["/", "/history", "/settings", "/users"])
    def test_on_every_page(self, client, raised, page, tmp_path, monkeypatch):
        import history
        monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "history.db"))
        history.init()
        html = client.get(page).get_data(as_text=True)
        assert 'id="alarm-banner"' in html and 'role="alert"' in html
        banner = html[html.index('id="alarm-banner"'):]
        banner = banner[:banner.index("</section>")]
        assert "1 active alarm" in banner
        assert "nomercy-mac-mini" in banner and "xcode" in banner
        assert "offline since 2026-" in banner

    def test_it_cannot_be_dismissed(self, client, raised):
        as_role("viewer")
        html = client.get("/").get_data(as_text=True)
        banner = html[html.index('id="alarm-banner"'):]
        banner = banner[:banner.index("</section>")]
        assert "<button" not in banner and "close" not in banner.lower()
        assert "dismiss" not in banner.lower()

    def test_only_an_admin_is_offered_acknowledge(self, client, raised):
        html = client.get("/").get_data(as_text=True)
        banner = html[html.index('id="alarm-banner"'):]
        banner = banner[:banner.index("</section>")]
        assert 'data-ack="runner:github:5">Acknowledge</button>' in banner
        as_role("operator")
        html = client.get("/").get_data(as_text=True)
        banner = html[html.index('id="alarm-banner"'):]
        assert "Acknowledge</button>" not in banner[:banner.index("</section>")]

    def test_hidden_when_there_is_nothing(self, client, quiet):
        html = client.get("/").get_data(as_text=True)
        assert 'id="alarm-banner" class="alarm-banner quiet" role="alert" aria-label="Alarms" hidden' \
            in html

    def test_a_viewer_sees_it_too(self, client, raised):
        as_role("viewer")
        assert "nomercy-mac-mini" in client.get("/").get_data(as_text=True)

    def test_not_on_the_sign_in_page(self, anon_client, raised):
        assert 'id="alarm-banner"' not in anon_client.get("/login").get_data(as_text=True)

    def test_a_blind_monitor_is_in_the_banner(self, client, tmp_path, monkeypatch):
        path = str(tmp_path / "control.db")
        schema.init(path)
        m = alarms.Monitor(clients=lambda f, env: FakeForge(None) if f == "github" else None)
        m.tick(path, {}, [], T0)
        monkeypatch.setattr(alarms, "MONITOR", m)
        html = client.get("/").get_data(as_text=True)
        assert "alarm monitor cannot reach GitHub since" in html

    def test_a_page_never_reaches_a_forge(self, client, raised):
        before = raised.asked
        client.get("/")
        client.get("/api/v2/alarms")
        assert raised.asked == before

    def test_the_banner_script_draws_the_same(self, raised):
        node = shutil.which("node")
        if not node:
            pytest.skip("node runs the real page JavaScript")
        source = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        start = source.index("function alarmBanner(")
        end = source.index("// end alarmBanner")
        view = alarms.snapshot(T0 + 10 * MIN)
        script = source[start:end] + "\nconsole.log(JSON.stringify(alarmBanner(" \
            + json.dumps(view) + ")));"
        out = json.loads(subprocess.run([node, "-e", script], capture_output=True,
                                        text=True, encoding="utf-8", check=True).stdout)
        assert out["count"] == 1
        assert "nomercy-mac-mini" in out["html"] and "1 active alarm" in out["html"]
        hostile = dict(view, alarms=[dict(view["alarms"][0], subject="<img src=x>",
                                          message="<script>x</script>",
                                          detail={"url": "javascript:alert(1)"})])
        script = source[start:end] + "\nconsole.log(JSON.stringify(alarmBanner(" \
            + json.dumps(hostile) + ")));"
        out = json.loads(subprocess.run([node, "-e", script], capture_output=True,
                                        text=True, encoding="utf-8", check=True).stdout)
        assert "<script>" not in out["html"] and "<img" not in out["html"]
        assert "javascript:" not in out["html"]


class TestTheEndpoint:
    def test_a_viewer_may_read_it(self, client, raised):
        as_role("viewer")
        r = client.get("/api/v2/alarms")
        assert r.status_code == 200
        [alarm] = r.json["alarms"]
        assert alarm["alarm_key"] == "runner:github:5"
        assert alarm["since"] == alarms.iso(T0) and alarm["raised_at"]
        assert r.json["thresholds"]["offline_minutes"] == 10
        assert r.json["monitor"]["forgejo_queue"] == "not covered"

    def test_nobody_signed_in_may_not(self, anon_client, raised):
        assert anon_client.get("/api/v2/alarms").status_code == 401
