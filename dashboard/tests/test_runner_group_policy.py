"""Which GitHub runner group each fleet registers into, and whether public
repositories may send it jobs (GitHub #5).

A self-hosted runner reachable from a public repository runs whatever a
fork's pull request asks it to. Whether that is possible is a property of
the runner group, set on GitHub, and nothing on the dashboard said which
group a fleet's runners join or what it allows. These tests hold the read:
a background cache refreshed every 300 s, never a call from a page, and an
answer that is "unknown" - with why - rather than a guess when GitHub could
not be read.
"""
import pytest

import api_v2
import github_api
import runner_groups
from tests.test_controller_dashboard_integration import plane  # noqa: F401

DEFAULT = {"id": 1, "name": "Default", "visibility": "all",
           "allows_public_repositories": True, "default": True, "inherited": False}
PRIVATE = {"id": 3, "name": "Stoney", "visibility": "selected",
           "allows_public_repositories": False, "default": False, "inherited": False}
ENV = {"GH_TOKEN": "token", "GITHUB_ORG": "NoMercy-Entertainment"}


@pytest.fixture(autouse=True)
def empty_cache():
    runner_groups.reset()
    yield
    runner_groups.reset()


def answers(monkeypatch, groups):
    calls = []

    def runner_groups_call(self):
        calls.append(self.org)
        return groups
    monkeypatch.setattr(github_api.GitHub, "runner_groups", runner_groups_call)
    return calls


class TestTheGroupAFleetJoins:
    def test_the_fleets_own_group_first(self, monkeypatch):
        answers(monkeypatch, [DEFAULT, PRIVATE])
        runner_groups.refresh(dict(ENV, RUNNER_GROUP="Default"))
        policy = runner_groups.policy_for({"provider": "github", "runner_group": "Stoney"},
                                          dict(ENV, RUNNER_GROUP="Default"))
        assert policy == {"known": True, "group": "Stoney", "source": "fleet setting",
                          "visibility": "selected", "allows_public_repositories": False,
                          "default": False, "org": "NoMercy-Entertainment",
                          "says": "only the repositories chosen for it on GitHub may "
                                  "use these runners, but no public one"}

    def test_then_the_deployments(self, monkeypatch):
        answers(monkeypatch, [DEFAULT, PRIVATE])
        runner_groups.refresh(ENV)
        policy = runner_groups.policy_for({"provider": "github", "runner_group": None},
                                          dict(ENV, RUNNER_GROUP="stoney"))
        assert policy["group"] == "Stoney" and policy["source"] == "deployment RUNNER_GROUP"

    def test_then_the_orgs_default_group(self, monkeypatch):
        answers(monkeypatch, [PRIVATE, DEFAULT])
        runner_groups.refresh(ENV)
        policy = runner_groups.policy_for({"provider": "github"}, ENV)
        assert policy["group"] == "Default" and policy["source"] == "GitHub default group"
        assert policy["allows_public_repositories"] is True

    def test_a_group_github_does_not_have_is_unknown(self, monkeypatch):
        answers(monkeypatch, [DEFAULT])
        runner_groups.refresh(ENV)
        policy = runner_groups.policy_for({"provider": "github", "runner_group": "Gone"}, ENV)
        assert policy["known"] is False and "Gone" in policy["why"]

    def test_forgejo_has_no_runner_groups(self):
        assert runner_groups.policy_for({"provider": "forgejo"}, ENV) is None


class TestUnknownIsSaidNotGuessed:
    def test_before_the_first_read(self):
        policy = runner_groups.policy_for({"provider": "github"}, ENV)
        assert policy["known"] is False and "not been read" in policy["why"]

    def test_when_github_could_not_be_read(self, monkeypatch):
        answers(monkeypatch, [DEFAULT])
        runner_groups.refresh(ENV)
        answers(monkeypatch, None)
        runner_groups.refresh(ENV)
        policy = runner_groups.policy_for({"provider": "github"}, ENV)
        assert policy["known"] is False and "could not be read" in policy["why"]

    def test_without_a_token(self, monkeypatch):
        calls = answers(monkeypatch, [DEFAULT])
        runner_groups.refresh({})
        assert calls == []
        policy = runner_groups.policy_for({"provider": "github"}, {})
        assert policy["known"] is False and "GH_TOKEN" in policy["why"]


class TestTheCacheIsTheOnlyReader:
    def test_the_loop_refreshes_on_its_interval(self, monkeypatch):
        calls = answers(monkeypatch, [DEFAULT])
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                raise SystemExit
        with pytest.raises(SystemExit):
            runner_groups.run_forever(lambda: ENV, sleep=sleep)
        assert len(calls) == 3 and slept == [300, 300, 300]
        assert runner_groups.REFRESH_SECONDS == 300

    def test_a_failing_refresh_never_stops_the_loop(self, monkeypatch):
        def boom(self):
            raise RuntimeError("network down")
        monkeypatch.setattr(github_api.GitHub, "runner_groups", boom)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                raise SystemExit
        with pytest.raises(SystemExit):
            runner_groups.run_forever(lambda: ENV, sleep=sleep)
        assert runner_groups.snapshot()["error"]

    def test_the_fleet_page_reads_the_cache_and_never_github(self, client, plane, monkeypatch):
        answers(monkeypatch, [DEFAULT, PRIVATE])
        runner_groups.refresh(ENV)

        def must_not_be_called(self):
            raise AssertionError("a page read called GitHub")
        monkeypatch.setattr(github_api.GitHub, "runner_groups", must_not_be_called)
        fleets = {f["fleet_id"]: f for f in client.get("/api/v2/fleet").json["fleets"]}
        policy = fleets["github-linux-x64"]["runner_group_policy"]
        assert policy["group"] == "Default" and policy["allows_public_repositories"] is True
        assert fleets["forgejo-linux-x64"]["runner_group_policy"] is None
        settings = client.get("/api/v2/settings").json
        assert [g["name"] for g in settings["runner_groups"]["groups"]] == ["Default", "Stoney"]
        assert "github-linux-x64" in settings["runner_groups"]["groups"][0]["fleets"]

    def test_settings_lists_each_group_read_only(self):
        from tests.test_resource_limits import run_page_js
        import json
        panel = {"known": True, "error": None, "at": "2026-10-08T10:00:00Z",
                 "refresh_seconds": 300,
                 "groups": [dict(DEFAULT, fleets=["github-linux-x64"]),
                            dict(PRIVATE, fleets=[])]}
        html = run_page_js("settings_v2.html", "function field(", "async function load()",
                           "runnerGroupsPanel(" + json.dumps(panel) + ")")
        assert "GitHub runner groups" in html and "Read only." in html
        assert "Default (default)" in html and "github-linux-x64" in html
        assert '<td class="no">allowed</td>' in html
        assert "<form" not in html and "<button" not in html
        unknown = run_page_js("settings_v2.html", "function field(", "async function load()",
                              "runnerGroupsPanel(" + json.dumps(
                                  {"known": False, "error": "no GH_TOKEN"}) + ")")
        assert "Unknown: no GH_TOKEN" in unknown

    def test_every_github_fleet_carries_a_policy_even_unknown(self, client, plane):
        for fleet in client.get("/api/v2/fleet").json["fleets"]:
            if fleet["provider"] == "github":
                assert fleet["runner_group_policy"]["known"] is False
