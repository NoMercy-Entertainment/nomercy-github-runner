"""T-1801: NFR-4 enforced centrally. A sentinel token never appears in an
API response, a websocket frame, a log line or an audit row, and a new field
that looks like a secret fails a test until it is declared one.

Redaction has two rules (control/redact.py): by field name, from
`providers.REDACTED_FIELDS`, and by value, for the deployment's own tokens
and every registration token the controller minted. These tests put a
sentinel where a careless route would leak it and look for it everywhere it
could come out.
"""
import ast
import io
import os
import re

import pytest

import api_v2
import providers
from control import audit, redact
from store import schema

SENTINEL = "ghp_SENTINEL-4c1e77d9ab-do-not-leak"
FORGE_SENTINEL = "fj_SENTINEL-8e21b0c3aa-do-not-leak"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def leaky(tmp_path, monkeypatch):
    """A deployment whose tokens are sentinels, and a fleet snapshot that
    carries them where no route should put them."""
    import app as dash
    env = tmp_path / ".env"
    env.write_text(f"GH_TOKEN={SENTINEL}\nFORGEJO_API_TOKEN={FORGE_SENTINEL}\n"
                   f"GITHUB_ORG=NoMercy-Entertainment\n")
    monkeypatch.setattr(dash, "ENV_PATH", str(env))
    snapshot = {
        "generated": "2026-09-18T05:00:00Z",
        "runners": [{"name": "github-runner-1", "provider": "github",
                     "state": "busy", "registration": "nomercy-x",
                     "job": f"deploy --token {SENTINEL}",
                     "token": SENTINEL, "uptime": "1h",
                     "cpu_percent": 1.0, "mem_used": "1GiB",
                     "mem_limit": "32GiB", "build_cache": "1GB"}],
        "elsewhere": [{"uuid": "u", "name": f"runner {FORGE_SENTINEL}",
                       "status": "idle", "labels": "windows"}],
        "providers_configured": {"github": True, "forgejo": True},
    }
    monkeypatch.setattr(dash, "_status", snapshot)
    monkeypatch.setattr(api_v2, "_db_path", lambda: str(tmp_path / "no.db"))
    return dash


class TestApiResponses:
    @pytest.mark.parametrize("path", ["/api/status", "/api/v1/status",
                                      "/api/v2/fleet", "/api/v2/runners",
                                      "/api/v2/fleets"])
    def test_no_route_lets_a_token_out(self, client, leaky, path):
        body = client.get(path).get_data(as_text=True)
        assert SENTINEL not in body and FORGE_SENTINEL not in body
        if path.endswith("status"):
            assert redact.MASK in body

    def test_a_field_named_as_a_secret_is_masked_by_name(self, client,
                                                         leaky):
        runner = client.get("/api/status").get_json()["runners"][0]
        assert runner["token"] == redact.MASK

    def test_a_non_secret_is_left_readable(self, client, leaky):
        runner = client.get("/api/status").get_json()["runners"][0]
        assert runner["registration"] == "nomercy-x"


class TestWebsocketFrames:
    def test_a_frame_goes_out_redacted(self, leaky):
        dash = leaky
        frame = next(dash.fleet_frames(lambda: True, lambda: None,
                                       lambda: dash._status))
        text = dash.encode_frame(frame)
        assert SENTINEL not in text and FORGE_SENTINEL not in text
        assert redact.MASK in text

    def test_the_view_sends_every_frame_through_it(self):
        with open(os.path.join(HERE, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        view = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "ws_fleet")
        sends = [ast.unparse(n) for n in ast.walk(view)
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "ws.send"]
        assert sends == ["ws.send(encode_frame(frame))"]


class TestLogLines:
    def test_a_log_line_carrying_a_token_goes_out_masked(self):
        out = io.StringIO()
        stream = redact.RedactingStream(out, lambda: [SENTINEL])
        stream.write(f"[github] POST failed with {SENTINEL}\n")
        assert SENTINEL not in out.getvalue()
        assert redact.MASK in out.getvalue()

    def test_a_broken_secret_source_never_loses_the_line(self):
        out = io.StringIO()

        def broken():
            raise OSError("no env file")
        redact.RedactingStream(out, broken).write("still logged\n")
        assert out.getvalue() == "still logged\n"

    def test_it_is_a_text_stream_to_whoever_asks(self):
        """click probes a stream by writing b"" to it; one that accepts it is
        taken for binary and sent bytes. Measured on the live dashboard: its
        banner came out as b'...'."""
        import click
        out = io.StringIO()
        stream = redact.RedactingStream(out, lambda: [SENTINEL])
        with pytest.raises(TypeError):
            stream.write(b"")
        click.echo(" * Serving Flask app 'app'", file=stream)
        assert out.getvalue() == " * Serving Flask app 'app'\n"

    def test_the_dashboard_installs_it_when_it_starts(self):
        with open(os.path.join(HERE, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        main = next(n for n in tree.body if isinstance(n, ast.If)
                    and "__main__" in ast.unparse(n.test))
        calls = [ast.unparse(n.func) for n in ast.walk(main)
                 if isinstance(n, ast.Call)]
        assert "_redact.install_log_redaction" in calls
        # and before anything else runs
        first = next(n for n in main.body if isinstance(n, ast.Expr))
        assert "install_log_redaction" in ast.unparse(first)


class TestAuditRows:
    def test_a_token_is_masked_by_name_and_by_value(self, tmp_path):
        path = str(tmp_path / "control.db")
        schema.init(path)
        redact.remember(SENTINEL)
        audit.record(path, "register", "accepted",
                     parameters={"token": SENTINEL,
                                 "note": f"retried with {SENTINEL}"})
        with schema.connect(path) as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM audit")]
        assert SENTINEL not in repr(rows)

    def test_a_minted_registration_token_is_remembered(self):
        """So a record written by code that never held it is masked too."""
        with open(os.path.join(HERE, "control", "provision.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        assert "remember(plan.token)" in src


# ---------------------------------------------------------------------------
# a new field carrying a token fails until it is listed
# ---------------------------------------------------------------------------

SECRETISH = re.compile(r"(?i)(token|secret|password|passwd|credential|"
                       r"api[_-]?key|private[_-]?key)")

#: Fields whose names look secret and are not: each says why.
NOT_SECRETS = {
    "token_mask": "the last four characters, for recognition; masked by "
                  "runner_detail.mask",
    "forgejo_token_mask": "as token_mask",
}


def secretish_names(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if SECRETISH.search(str(k)):
                found.add(str(k).lower())
            secretish_names(v, found)
    elif isinstance(value, list):
        for v in value:
            secretish_names(v, found)
    return found


def undeclared(names):
    declared = {n.lower() for n in providers.REDACTED_FIELDS}
    return sorted(n for n in names if n not in declared
                  and n not in NOT_SECRETS)


class TestNewSecretFields:
    def test_every_secret_looking_field_the_api_returns_is_declared(
            self, client, leaky):
        names = set()
        for path in ("/api/status", "/api/v2/fleet", "/api/v2/runners",
                     "/api/v2/fleets", "/api/version",
                     "/api/control/workers"):
            names |= secretish_names(client.get(path).get_json())
        assert undeclared(names) == []

    def test_the_check_fails_on_one_that_is_not(self):
        """A field added tomorrow called `api_key` is caught."""
        payload = {"runners": [{"name": "x", "api_key": "abc"}]}
        assert undeclared(secretish_names(payload)) == ["api_key"]
