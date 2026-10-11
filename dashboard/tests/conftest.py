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
def client(tmp_path, monkeypatch):
    """A Flask test client signed in as an admin.

    app.py guards every route with a before_request hook and reads the role
    from the allowlist on every request, so the client needs a real entry in
    a throwaway users.json and a session that carries its sub.
    """
    import app as dash
    import users

    monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
    users.approve("sub-test-admin", "admin")
    dash.app.config["TESTING"] = True
    with dash.app.test_client() as c:
        with c.session_transaction() as s:
            s["sub"] = "sub-test-admin"
        yield c


@pytest.fixture
def anon_client():
    """An unauthenticated client, for asserting the guard actually guards."""
    import app as dash

    dash.app.config["TESTING"] = True
    with dash.app.test_client() as c:
        yield c
