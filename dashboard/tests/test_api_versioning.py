"""The existing API, served a second time under /api/v1.

This exists so the new id-keyed API can land beside the name-keyed one instead
of replacing it in a single step. The old page keeps calling what it always
called; nothing has to change on the same day.

The part worth reading twice is the authorisation. The guard makes decisions on
`request.path`, and two of its rules match on a prefix: `/api/users/` is
admin-only, and `/settings` is closed to viewers. An alias at
`/api/v1/users/approve` does not start with `/api/users/`, so aliasing without
touching the guard would have quietly opened a second door to user management
for anyone who could log in. The fix is that the guard now judges a
version-stripped path, and the tests below are what keep it that way - a future
`/api/v2` alias inherits the same rules for free, and if it ever stops doing so
these fail.
"""
import pytest

import app as dash
import users


class TestPolicyPath:
    """The normalisation the guard depends on, tested directly, because the
    route tests below can only reach it through a live request."""

    def test_a_v1_alias_is_judged_as_the_unprefixed_path(self):
        assert dash.policy_path("/api/v1/users/approve") == "/api/users/approve"
        assert dash.policy_path("/api/v1/status") == "/api/status"

    def test_an_unversioned_path_is_unchanged(self):
        assert dash.policy_path("/api/users/approve") == "/api/users/approve"
        assert dash.policy_path("/settings") == "/settings"

    def test_a_path_that_merely_mentions_a_version_is_not_stripped(self):
        """`/api/v1status` is not a version prefix, and treating it as one
        would be a way past the rules rather than a convenience."""
        assert dash.policy_path("/api/v1status") == "/api/v1status"

    def test_only_declared_versions_are_stripped(self):
        assert dash.policy_path("/api/v9/users/x") == "/api/v9/users/x"


class TestTheAliasesExist:
    def _rules(self):
        return {r.rule for r in dash.app.url_map.iter_rules()}

    def test_every_api_route_has_a_v1_alias(self):
        rules = self._rules()
        unversioned = {
            r for r in rules
            if r.startswith("/api/")
            and not r.startswith(("/api/v1/", "/api/v2/"))
            and r != "/api/version"
        }
        assert unversioned, "sanity: there should be API routes to alias"
        missing = [r for r in unversioned
                   if "/api/v1" + r[len("/api"):] not in rules]
        assert missing == [], missing

    def test_v2_is_its_own_family_and_never_aliased_under_v1(self):
        """v2 is keyed by runner_id; serving it again under v1 would put an
        id-keyed route inside the name-keyed version."""
        rules = self._rules()
        assert any(r.startswith("/api/v2/") for r in rules)
        assert not any(r.startswith("/api/v1/v2/") for r in rules)

    def test_the_alias_reuses_the_same_view_function(self):
        """Two handlers would drift; one cannot."""
        by_rule = {r.rule: r.endpoint for r in dash.app.url_map.iter_rules()}
        endpoint = by_rule["/api/status"]
        alias_endpoint = by_rule["/api/v1/status"]
        assert (dash.app.view_functions[endpoint]
                is dash.app.view_functions[alias_endpoint])

    def test_version_discovery_is_not_itself_versioned(self):
        """A client asks this to find out which versions exist, so it cannot
        live inside one."""
        rules = self._rules()
        assert "/api/version" in rules
        assert "/api/v1/version" not in rules

    def test_the_alias_keeps_the_original_methods(self):
        by_rule = {r.rule: r for r in dash.app.url_map.iter_rules()}
        for rule in ("/api/recreate", "/api/runner/add"):
            original = by_rule[rule].methods - {"HEAD", "OPTIONS"}
            alias = by_rule["/api/v1" + rule[len("/api"):]].methods - {
                "HEAD", "OPTIONS"}
            assert original == alias, rule


class TestTheAliasBehavesLikeTheOriginal:
    def test_version_reports_what_is_served(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        body = r.get_json()
        # v1 is still served, and v2 now beside it (T-1401, api_v2.py).
        assert body["api"] == list(dash.SERVED_VERSIONS)
        assert body["api"] == ["v1", "v2"]
        assert body["schema"] == dash.FLEET_SCHEMA

    def test_an_alias_answers_like_the_unprefixed_route(self, client):
        """A real route, answered twice. The history schema is created first
        because the point is to compare two live responses, not to discover
        that an empty database raises the same way through both paths."""
        import history

        history.init()
        a = client.get("/api/history/summary")
        b = client.get("/api/v1/history/summary")
        assert a.status_code == 200
        assert a.status_code == b.status_code
        assert a.get_json() == b.get_json()


class TestTheAliasInheritsAuthorisation:
    """The hole that aliasing would have opened, kept closed."""

    def test_an_unauthenticated_call_is_refused_on_both(self, anon_client):
        for path in ("/api/status", "/api/v1/status"):
            r = anon_client.get(path)
            assert r.status_code == 401, path
            assert r.get_json()["error"] == "not authenticated"

    def test_user_management_stays_admin_only_through_the_alias(
            self, client, tmp_path, monkeypatch):
        """Without the version-stripped policy path this passes for a viewer,
        which is the whole reason policy_path exists."""
        monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
        users.approve("sub-viewer", "viewer")
        with client.session_transaction() as s:
            s["sub"] = "sub-viewer"

        direct = client.post("/api/users/approve", json={})
        aliased = client.post("/api/v1/users/approve", json={})
        assert direct.status_code == aliased.status_code, (
            "the alias must be judged exactly as the unprefixed path")
        assert aliased.status_code in (403, 400)
        assert direct.status_code != 200 and aliased.status_code != 200

    def test_a_viewer_cannot_post_through_an_alias(self, client, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(users, "PATH", str(tmp_path / "users.json"))
        users.approve("sub-viewer2", "viewer")
        with client.session_transaction() as s:
            s["sub"] = "sub-viewer2"
        r = client.post("/api/v1/recreate", json={"provider": "github"})
        assert r.status_code == 403


class TestTheWebsocketFrameCarriesItsSchema:
    """A socket outlives a deploy.

    A tab that connected before an upgrade keeps receiving frames written by
    the new code. Without a number in the frame its only way to notice a
    changed payload is to misread it.

    The number is stamped by the transport rather than by `fleet_frames`,
    because what changed and how it is framed are two decisions. That also
    leaves the diff generator's tests speaking only about diffs.
    """

    def test_a_snapshot_frame_says_which_schema_it_is(self):
        framed = dash.wire_frame({"type": "snapshot", "data": {"runners": []}})
        assert framed["schema"] == dash.FLEET_SCHEMA
        assert framed["type"] == "snapshot"
        assert framed["data"] == {"runners": []}

    def test_an_update_frame_carries_it_too(self):
        """Not only the snapshot: a frame must be readable on its own."""
        framed = dash.wire_frame({"type": "update", "data": {"disk": {}}})
        assert framed["schema"] == dash.FLEET_SCHEMA

    def test_the_original_frame_is_not_mutated(self):
        """fleet_frames yields dicts it may still hold; stamping must copy."""
        original = {"type": "snapshot", "data": {}}
        dash.wire_frame(original)
        assert "schema" not in original

    def test_the_number_matches_what_the_version_route_reports(self, client):
        """Two places naming the schema would drift; they must agree."""
        reported = client.get("/api/version").get_json()["schema"]
        assert dash.wire_frame({"type": "update"})["schema"] == reported

    def test_the_diff_generator_still_yields_bare_frames(self):
        """The separation itself. If the generator started stamping, the
        envelope and the diff would be one decision again."""
        gen = dash.fleet_frames(lambda: True, lambda: None,
                                lambda: {"runners": []})
        first = next(gen)
        assert set(first) == {"type", "data"}
