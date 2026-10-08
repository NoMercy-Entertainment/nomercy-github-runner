"""T-1901, built: the forge tokens in a store that is set and never read back.

Done when no API response can return a token value. These set a sentinel
through the API and look for it in every answer the API gives, in the
database file itself, and in `.env` - which the store must leave exactly as
it was: moving the values out of it is the task's NEVER-AUTO run step, not
this build.
"""
import os

import pytest

import api_v2
from control.secrets import NAMES, SecretRefused, SecretStore
from store import schema

SENTINEL = "ghp_STORE-SENTINEL-77aa19c3e0"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    monkeypatch.setattr(schema, "DB_PATH", path)
    return path


def set_it(client, name="GH_TOKEN", value=SENTINEL):
    return client.post(f"/api/v2/secrets/{name}", json={"value": value})


class TestSetNeverReadBack:
    def test_it_is_set_and_the_answer_does_not_echo_it(self, client, plane):
        r = set_it(client)
        assert r.status_code == 200
        assert SENTINEL not in r.get_data(as_text=True)

    def test_its_status_says_set_by_whom_and_when_and_no_more(self, client,
                                                              plane):
        set_it(client)
        body = client.get("/api/v2/secrets").get_data(as_text=True)
        assert SENTINEL not in body
        gh = next(s for s in client.get("/api/v2/secrets").get_json()[
            "secrets"] if s["name"] == "GH_TOKEN")
        assert gh["set"] is True and gh["set_at"] and gh["fingerprint"]
        assert set(gh) == {"name", "set", "fingerprint", "set_at", "set_by"}

    def test_no_api_answer_carries_it(self, client, plane):
        set_it(client)
        import app as dash
        import history
        history.init()
        for rule in dash.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/") or "GET" not in \
                    rule.methods or "<" in rule.rule:
                continue
            body = client.get(rule.rule).get_data(as_text=True)
            assert SENTINEL not in body, rule.rule

    def test_the_controller_can_still_use_it(self, client, plane):
        set_it(client)
        env = SecretStore(plane).overlay({"GH_TOKEN": "from-dotenv",
                                          "GITHUB_ORG": "NoMercy"})
        assert env == {"GH_TOKEN": SENTINEL, "GITHUB_ORG": "NoMercy"}

    def test_the_fingerprint_tells_two_apart(self, client, plane):
        set_it(client)
        first = SecretStore(plane).status()[0]["fingerprint"]
        set_it(client, value=SENTINEL + "-rotated")
        assert SecretStore(plane).status()[0]["fingerprint"] != first


class TestAtRest:
    def test_the_database_holds_it_sealed(self, client, plane):
        set_it(client)
        with open(plane, "rb") as fh:
            assert SENTINEL.encode() not in fh.read()

    def test_the_key_is_its_own_file_readable_by_its_owner(self, client,
                                                           plane):
        set_it(client)
        key = os.path.join(os.path.dirname(plane), "secrets.key")
        assert os.path.exists(key)
        if os.name == "posix":
            assert os.stat(key).st_mode & 0o077 == 0


class TestWhoMayTouchIt:
    @pytest.mark.parametrize("role", ["operator", "viewer"])
    def test_only_an_admin(self, client, plane, role):
        import users
        users.approve("sub-test-admin", role)
        assert set_it(client).status_code == 403
        assert client.get("/api/v2/secrets").status_code == 403

    def test_setting_it_is_audited_without_the_value(self, client, plane):
        from control import audit
        set_it(client)
        (row,) = audit.entries(plane, verb="set_secret")
        assert row["decision"] == "accepted"
        assert SENTINEL not in repr(row)


class TestWhatItRefuses:
    @pytest.mark.parametrize("name", ["PATH", "OIDC_ISSUER", "../x"])
    def test_a_name_it_does_not_hold(self, client, plane, name):
        assert set_it(client, name=name).status_code in (400, 404)

    @pytest.mark.parametrize("value", ["", "   ", "two\nlines", None])
    def test_a_value_that_is_not_one_token(self, plane, value):
        with pytest.raises(SecretRefused):
            SecretStore(plane).set("GH_TOKEN", value, "someone")

    def test_it_holds_the_two_forge_tokens_and_the_alarm_webhook(self):
        assert NAMES == ("GH_TOKEN", "FORGEJO_API_TOKEN", "ALARM_WEBHOOK_URL")


class TestDotEnvIsLeftAlone:
    def test_setting_a_token_does_not_touch_the_environment_file(
            self, client, plane, tmp_path, monkeypatch):
        import app as dash
        env = tmp_path / ".env"
        env.write_text("GH_TOKEN=the-old-one-123456\n")
        monkeypatch.setattr(dash, "ENV_PATH", str(env))
        before = (env.read_bytes(), os.stat(env).st_mtime_ns)
        set_it(client)
        SecretStore(plane).clear("GH_TOKEN")
        assert (env.read_bytes(), os.stat(env).st_mtime_ns) == before

    def test_the_module_never_opens_it(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "control", "secrets.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith(("#", '"')))
        assert "ENV_PATH" not in code and "write_env" not in code
