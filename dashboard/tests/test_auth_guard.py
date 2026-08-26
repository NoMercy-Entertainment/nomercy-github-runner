"""The guard: what a role may reach, and what a stale session may not.

The rule is one condition in one place - a POST from a viewer is 403 - because
every mutating route here is a POST and every read is a GET. Two reads are not
covered by that and are named explicitly: GET /settings and GET /users. Those
two have their own tests, because they are exactly the cases the general rule
would miss.
"""
import pytest

import oidc
import users


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
    # An admin already exists, so no test identity gets the bootstrap role by
    # accident. The bootstrap itself is tested by name further down.
    users.approve("sub-resident-admin", "admin")
    return users


@pytest.fixture
def as_role(store, monkeypatch):
    """A client signed in as a given role, via a real allowlist entry."""
    import app as dash

    dash.app.config["TESTING"] = True
    # Keep the fleet endpoints off docker in these tests.
    monkeypatch.setattr(dash.ops, "list_runner_names", lambda: [])

    def make(role, sub="sub-user"):
        if role:
            store.approve(sub, role)
        c = dash.app.test_client()
        with c.session_transaction() as s:
            s.clear()
            s["sub"] = sub
        return c
    return make


# ------------------------------------------------------------- the POST rule

def test_a_viewer_may_not_post(as_role):
    assert as_role("viewer").post("/api/prune-all").status_code == 403


def test_an_operator_may_post(as_role):
    assert as_role("operator").post("/api/prune-all").status_code != 403


def test_an_admin_may_post(as_role):
    assert as_role("admin").post("/api/prune-all").status_code != 403


def test_a_viewer_may_read_the_fleet(as_role):
    assert as_role("viewer").get("/api/status").status_code == 200


# ------------------------------------------- the two reads the rule misses

def test_a_viewer_may_not_read_settings(as_role):
    assert as_role("viewer").get("/settings").status_code == 403


def test_an_operator_may_read_settings(as_role):
    assert as_role("operator").get("/settings").status_code == 200


def test_only_an_admin_may_read_the_user_list(as_role):
    assert as_role("operator").get("/users").status_code == 403
    assert as_role("admin").get("/users").status_code == 200


def test_only_an_admin_may_change_access(as_role):
    r = as_role("operator").post("/api/users/approve",
                                 json={"sub": "sub-other", "role": "admin"})
    assert r.status_code == 403
    assert users.role_of("sub-other") is None


def test_an_admin_may_grant_access_including_admin(as_role):
    """Every admin can manage access, not just whoever signed in first."""
    c = as_role("admin")
    r = c.post("/api/users/approve", json={"sub": "sub-other", "role": "admin"})
    assert r.status_code == 200
    assert users.role_of("sub-other") == "admin"


# --------------------------------------------------------------- revocation

def test_a_revoked_session_is_refused_on_its_next_request(as_role, store):
    """Not at next sign-in: the cookie is valid for fourteen days."""
    c = as_role("operator")
    assert c.get("/api/status").status_code == 200

    store.revoke("sub-user")

    assert c.get("/api/status").status_code == 401


def test_a_role_downgrade_takes_effect_immediately(as_role, store):
    c = as_role("operator")
    assert c.post("/api/prune-all").status_code != 403

    store.approve("sub-user", "viewer")

    assert c.post("/api/prune-all").status_code == 403


def test_a_session_for_an_identity_that_was_never_approved_is_refused(as_role):
    assert as_role(None, sub="sub-ghost").get("/api/status").status_code == 401


# --------------------------------------------------------------- the callback

@pytest.fixture
def fake_idp(monkeypatch, store):
    """A stubbed OIDC client wired into the app."""
    import app as dash

    dash.app.config["TESTING"] = True

    class Fake:
        claims = {"sub": "sub-newcomer", "preferred_username": "newcomer",
                  "name": "A Newcomer"}

        def configured(self):
            return True

        def authorize_url(self):
            return "https://idp.invalid/auth?x=1", "the-state", "the-verifier"

        def exchange(self, code, verifier):
            if code == "bad-code":
                raise oidc.OIDCError("token exchange refused")
            return self.claims

    fake = Fake()
    monkeypatch.setattr(dash, "_oidc", lambda: fake)
    return dash, fake


def test_auth_start_redirects_to_the_idp_and_remembers_the_state(fake_idp):
    dash, _ = fake_idp
    c = dash.app.test_client()
    r = c.get("/auth/start")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://idp.invalid/auth")
    with c.session_transaction() as s:
        assert s["oidc_state"] == "the-state"
        assert s["oidc_verifier"] == "the-verifier"


def test_a_callback_with_the_wrong_state_is_refused(fake_idp):
    """CSRF: a code delivered without the state we issued is not our login."""
    dash, _ = fake_idp
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    r = c.get("/callback?code=abc&state=someone-elses")

    assert r.status_code == 400
    with c.session_transaction() as s:
        assert "sub" not in s


def test_a_callback_with_no_state_in_session_is_refused(fake_idp):
    dash, _ = fake_idp
    c = dash.app.test_client()
    r = c.get("/callback?code=abc&state=the-state")
    assert r.status_code == 400


def test_an_unknown_identity_lands_in_pending_and_gets_no_session(fake_idp):
    dash, _ = fake_idp
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    r = c.get("/callback?code=abc&state=the-state")

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/auth/pending")
    with c.session_transaction() as s:
        assert "sub" not in s
    assert [p["sub"] for p in users.pending()] == ["sub-newcomer"]


def test_an_approved_identity_gets_a_session(fake_idp, store):
    dash, _ = fake_idp
    store.approve("sub-newcomer", "operator")
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    r = c.get("/callback?code=abc&state=the-state")

    assert r.status_code == 302
    with c.session_transaction() as s:
        assert s["sub"] == "sub-newcomer"


def test_the_first_sign_in_ever_becomes_admin_and_gets_a_session(
        fake_idp, store, tmp_path, monkeypatch):
    """An empty allowlist has nobody to approve anyone: the first in is admin."""
    dash, _ = fake_idp
    monkeypatch.setattr(store, "PATH", str(tmp_path / "empty.json"))
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    r = c.get("/callback?code=abc&state=the-state")

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/")
    assert users.role_of("sub-newcomer") == "admin"
    assert c.get("/users").status_code == 200


def test_a_failed_exchange_does_not_sign_anyone_in(fake_idp):
    dash, _ = fake_idp
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    r = c.get("/callback?code=bad-code&state=the-state")

    assert r.status_code == 400
    with c.session_transaction() as s:
        assert "sub" not in s


def test_the_state_is_single_use(fake_idp, store):
    """A replayed callback must not work a second time."""
    dash, _ = fake_idp
    store.approve("sub-newcomer", "operator")
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["oidc_state"] = "the-state"
        s["oidc_verifier"] = "the-verifier"

    c.get("/callback?code=abc&state=the-state")
    with c.session_transaction() as s:
        s.pop("sub", None)

    assert c.get("/callback?code=abc&state=the-state").status_code == 400


# ------------------------------------------------- admin cannot strand itself

def test_an_admin_cannot_revoke_their_own_access(as_role):
    """An account that hands out access must not be able to delete itself."""
    c = as_role("admin", sub="sub-boss")
    r = c.post("/api/users/revoke", json={"sub": "sub-boss"})
    assert r.status_code == 400
    assert users.role_of("sub-boss") == "admin"


def test_an_admin_cannot_demote_themselves(as_role):
    c = as_role("admin", sub="sub-boss")
    r = c.post("/api/users/approve", json={"sub": "sub-boss", "role": "viewer"})
    assert r.status_code == 400
    assert users.role_of("sub-boss") == "admin"


# --------------------------------------------------------- no password path

def test_there_is_no_password_login_any_more(anon_client):
    assert anon_client.post("/login", data={"password": "x"}).status_code == 405
    assert anon_client.get("/setup").status_code in (302, 401, 404)


def test_a_legacy_password_session_is_worth_nothing(anon_client):
    """Cookies from before the cutover carried ok=True and no sub."""
    with anon_client.session_transaction() as s:
        s["ok"] = True
    assert anon_client.get("/api/status").status_code == 401
