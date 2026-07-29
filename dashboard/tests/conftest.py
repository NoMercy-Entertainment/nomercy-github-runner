"""Test fixtures.

The dashboard is written to run as a container with /data mounted. On the host
there is no /data, and importing app.py writes a secret key at import time, so
DASH_DATA is redirected to a temp dir BEFORE any dashboard module is imported.
"""
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # make dashboard/ importable

_TMP = tempfile.mkdtemp(prefix="dash-test-")
os.environ.setdefault("DASH_DATA", _TMP)
os.environ.setdefault("ENV_PATH", os.path.join(_TMP, ".env"))


@pytest.fixture
def client():
    """A logged-in Flask test client.

    app.py guards every route with a before_request hook, so an unauthenticated
    client gets 401 or a redirect. Setting session['ok'] is what login() does.
    """
    import app as dash

    dash.app.config["TESTING"] = True
    if not dash.password_is_set():
        dash.set_password("test-password-123")
    with dash.app.test_client() as c:
        with c.session_transaction() as s:
            s["ok"] = True
        yield c


@pytest.fixture
def anon_client():
    """An unauthenticated client, for asserting the guard actually guards."""
    import app as dash

    dash.app.config["TESTING"] = True
    if not dash.password_is_set():
        dash.set_password("test-password-123")
    with dash.app.test_client() as c:
        yield c
