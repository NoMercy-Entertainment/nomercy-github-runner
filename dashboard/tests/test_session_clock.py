"""Sessions must survive the host clock moving backwards.

This dashboard runs in a WSL distro whose clock is periodically corrected
backwards by ~12 seconds. Flask signs the session cookie with a timestamp and
itsdangerous rejects any cookie dated after the current clock, so one backward
jump silently invalidates every session at once and logs everyone out. These
tests pin that down: a backward jump must not log anyone out, while a genuine
expiry and a forged cookie must both still be refused.
"""
import pytest


@pytest.fixture
def clock_shift(monkeypatch):
    """Move the one system clock, the way a real correction moves it.

    time.time() is what itsdangerous stamps cookies with and what the session
    interface measures age against, so patching it here shifts both together -
    a faithful stand-in for the distro's clock being pulled backwards.
    """
    import time

    real = time.time

    def shift(seconds):
        monkeypatch.setattr(time, "time", lambda: real() + seconds)
    return shift


def test_a_backward_clock_jump_does_not_log_you_out(client, clock_shift):
    """The exact production failure: -12s, then every request is a 401."""
    assert client.get("/api/status").status_code == 200
    clock_shift(-12)
    assert client.get("/api/status").status_code == 200


def test_a_genuinely_expired_session_is_still_refused(client, clock_shift):
    import app as dash

    lifetime = int(dash.app.permanent_session_lifetime.total_seconds())
    clock_shift(lifetime + 60)
    assert client.get("/api/status").status_code == 401


def test_a_tampered_cookie_is_still_refused(client):
    cookie = client.get_cookie("session")
    client.set_cookie("session", cookie.value[:-4] + "AAAA")
    assert client.get("/api/status").status_code == 401
