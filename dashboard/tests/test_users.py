"""The allowlist: authenticating proves who you are and grants nothing.

Keycloak has no groups or roles claim for this realm, and org-style "anyone in
the directory" was rejected during design. Access is this file and nothing
else.
"""
import pytest

import users


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh allowlist per test, with no owner seeded unless asked."""
    monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
    monkeypatch.delenv("DASH_OWNER", raising=False)
    return users


@pytest.fixture
def owned(store, monkeypatch):
    """An allowlist seeded with an owner hint, nothing bound yet."""
    monkeypatch.setenv("DASH_OWNER", "oidc:preferred_username:phill")
    return store


# --------------------------------------------------------------------- basics

def test_an_unknown_identity_gets_nothing(store):
    assert store.sign_in("sub-stranger", "stranger", "A Stranger") is None
    assert store.role_of("sub-stranger") is None


def test_an_unknown_identity_is_recorded_as_pending(store):
    store.sign_in("sub-stranger", "stranger", "A Stranger")
    subs = [p["sub"] for p in store.pending()]
    assert subs == ["sub-stranger"]


def test_signing_in_twice_does_not_duplicate_the_request(store):
    store.sign_in("sub-stranger", "stranger", "A Stranger")
    store.sign_in("sub-stranger", "stranger", "A Stranger")
    assert len(store.pending()) == 1


# ----------------------------------------------------------- owner bootstrap

def test_the_seeded_owner_is_bound_on_first_sign_in(owned):
    assert owned.sign_in("sub-phill", "phill", "Phil") == "owner"
    assert owned.role_of("sub-phill") == "owner"


def test_the_owner_is_keyed_on_sub_not_on_the_username(owned):
    """A renamed or re-registered username must not inherit the account."""
    owned.sign_in("sub-phill", "phill", "Phil")
    assert owned.sign_in("sub-impostor", "phill", "Not Phil") is None
    assert owned.role_of("sub-impostor") is None


def test_the_hint_is_ignored_once_an_owner_exists(owned):
    owned.sign_in("sub-phill", "phill", "Phil")
    owned.sign_in("sub-other", "phill", "Someone")
    owners = [u["sub"] for u in owned.list_users() if u["role"] == "owner"]
    assert owners == ["sub-phill"]


def test_without_a_hint_nobody_becomes_owner_by_signing_in_first(store):
    """The race that would hand over the fleet."""
    assert store.sign_in("sub-first", "first", "First") is None
    assert store.list_users() == []


# ------------------------------------------------------------------ approvals

def test_approving_grants_the_role_and_clears_the_request(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.approve("sub-mate", "viewer")
    assert store.role_of("sub-mate") == "viewer"
    assert store.pending() == []


def test_denying_clears_the_request_without_granting(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.deny("sub-mate")
    assert store.role_of("sub-mate") is None
    assert store.pending() == []


def test_revoking_takes_the_role_away(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.approve("sub-mate", "operator")
    store.revoke("sub-mate")
    assert store.role_of("sub-mate") is None


def test_a_role_can_be_changed(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.approve("sub-mate", "viewer")
    store.approve("sub-mate", "operator")
    assert store.role_of("sub-mate") == "operator"


def test_an_invented_role_is_refused(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    with pytest.raises(ValueError):
        store.approve("sub-mate", "root")
    assert store.role_of("sub-mate") is None


def test_an_approved_user_signing_in_again_keeps_their_role(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.approve("sub-mate", "operator")
    assert store.sign_in("sub-mate", "mate", "A Mate") == "operator"


def test_a_revoked_user_signing_in_again_does_not_regain_access(store):
    store.sign_in("sub-mate", "mate", "A Mate")
    store.approve("sub-mate", "operator")
    store.revoke("sub-mate")
    assert store.sign_in("sub-mate", "mate", "A Mate") is None
