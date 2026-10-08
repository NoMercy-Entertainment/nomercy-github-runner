"""The allowlist: authenticating proves who you are and grants nothing.

Keycloak has no groups or roles claim for this realm, and org-style "anyone in
the directory" was rejected during design. Access is this file and nothing
else.
"""
import pytest

import users


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A brand-new allowlist: empty, so the next sign-in becomes admin."""
    monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
    return users


@pytest.fixture
def store(fresh):
    """An allowlist that already has its admin, so nobody else gets a free role."""
    fresh.sign_in("sub-admin", "admin", "The Admin")
    return fresh


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


# ----------------------------------------------------------- admin bootstrap

def test_the_first_sign_in_on_an_empty_list_becomes_admin(fresh):
    assert fresh.sign_in("sub-phill", "phill", "Phil") == "admin"
    assert fresh.role_of("sub-phill") == "admin"
    assert fresh.pending() == []


def test_the_second_sign_in_does_not(fresh):
    fresh.sign_in("sub-phill", "phill", "Phil")
    assert fresh.sign_in("sub-stoney", "stoney", "Stoney") is None
    assert [p["sub"] for p in fresh.pending()] == ["sub-stoney"]


def test_admin_is_keyed_on_sub_not_on_the_username(fresh):
    """A renamed or re-registered username must not inherit the account."""
    fresh.sign_in("sub-phill", "phill", "Phil")
    assert fresh.sign_in("sub-impostor", "phill", "Not Phil") is None
    assert fresh.role_of("sub-impostor") is None


def test_non_admin_members_do_not_close_the_bootstrap(fresh):
    """A list with people on it but no admin would otherwise be unmanageable."""
    fresh.approve("sub-viewer", "viewer")
    assert fresh.sign_in("sub-phill", "phill", "Phil") == "admin"


def test_once_the_last_admin_is_revoked_the_next_sign_in_becomes_admin(fresh):
    fresh.sign_in("sub-phill", "phill", "Phil")
    fresh.revoke("sub-phill")
    assert fresh.sign_in("sub-stoney", "stoney", "Stoney") == "admin"


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


# ------------------------------------------------- a damaged list fails closed
#
# Only a missing file is an empty list. One that is there but cannot be read
# used to read as empty too, so the next person to sign in became admin and
# the real list was written over (audit AUD-9312).

@pytest.fixture
def damaged(store):
    """A list with its admin, then broken; each test breaks it its own way."""
    path = store.PATH

    def damage(content):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        with open(path, "rb") as fh:
            return fh.read()
    return store, damage


@pytest.mark.parametrize("content", [
    '{"users": {"sub-admin": {"role": "admin"',     # truncated
    "[]",                                            # not an object
    '{"users": [], "pending": {}}',                  # wrong shape inside
    "",                                              # emptied
])
def test_a_damaged_list_makes_nobody_admin_and_is_not_written_over(damaged, content):
    store, damage = damaged
    before = damage(content)
    assert store.sign_in("sub-stranger", "stranger", "A Stranger") is None
    assert store.role_of("sub-stranger") is None
    with open(store.PATH, "rb") as fh:
        assert fh.read() == before


def test_a_damaged_list_grants_nobody_anything(damaged):
    store, damage = damaged
    damage('{"users": {')
    assert store.role_of("sub-admin") is None


def test_changes_to_a_damaged_list_are_refused(damaged):
    store, damage = damaged
    before = damage("{not json")
    for change in (lambda: store.approve("sub-x", "viewer"),
                   lambda: store.deny("sub-x"),
                   lambda: store.revoke("sub-admin"),
                   store.pending, store.list_users):
        with pytest.raises(store.Unreadable):
            change()
    with open(store.PATH, "rb") as fh:
        assert fh.read() == before


def test_a_list_that_cannot_be_opened_fails_closed(fresh, tmp_path):
    """A directory where the file should be stands in for a permission or
    I/O error: it is there, and it cannot be read."""
    import os
    os.mkdir(fresh.PATH)
    assert fresh.sign_in("sub-stranger", "stranger", "A Stranger") is None
    assert os.path.isdir(fresh.PATH)


def test_a_missing_list_is_still_the_bootstrap(fresh):
    import os
    assert not os.path.exists(fresh.PATH)
    assert fresh.sign_in("sub-phill", "phill", "Phil") == "admin"
