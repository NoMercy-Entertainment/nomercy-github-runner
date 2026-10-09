"""What the fleet page says about who may use a GitHub fleet's runners, in
plain words, and whether the runners themselves refuse outside code.

The owner read "Fillz · all · public repositories allowed" and could not tell
what it meant or whether it was a problem. The heading now says it as a
sentence - "Runner group Fillz: every repository in NoMercy-Entertainment may
use these runners, public ones included" - and a second line says whether
the runners refuse a pull request from an outside fork.

That second line is true only when it is true of every runner in the fleet:
each GitHub unit reports which origin check its own job-started hook carries
(agent/origin_guard.py), because a hook is written at create and a runner made
before the check does not have it, whatever the agent or the image has now.

Green: every runner refuses outside code. Red: public repositories may use the
runners and not every runner refuses. Amber: anything else.
"""
import json

import pytest

import api_v2
import cards
import github_api
import runner_groups
from control import inventory as inv
from store import schema
from tests.fake_runtime import ALL_CELLS
from tests.test_controller_dashboard_integration import plane  # noqa: F401
from tests.test_generic_card import NODE, fleet_row, render
from tests.test_partial_failure import GH

ORG = "NoMercy-Entertainment"
FILLZ = {"id": 4, "name": "Fillz", "visibility": "all",
         "allows_public_repositories": True, "default": False, "inherited": False}
ENV = {"GH_TOKEN": "token", "GITHUB_ORG": ORG}

IN_PLACE = ("Outside pull requests are refused by the runner "
            "(only org members and trusted maintainers run code)")


@pytest.fixture(autouse=True)
def empty_cache():
    runner_groups.reset()
    yield
    runner_groups.reset()


# ---- in plain words ----------------------------------------------------------

class TestTheSentence:
    @pytest.mark.parametrize("group,says", [
        (FILLZ, f"every repository in {ORG} may use these runners, public ones included"),
        (dict(FILLZ, allows_public_repositories=False),
         f"every repository in {ORG} may use these runners, but no public one"),
        (dict(FILLZ, visibility="selected"),
         "only the repositories chosen for it on GitHub may use these runners, "
         "public ones included"),
        (dict(FILLZ, visibility="private", allows_public_repositories=False),
         f"only the private repositories in {ORG} may use these runners"),
        (dict(FILLZ, visibility=None),
         f"repositories in {ORG} may use these runners as GitHub's settings for it say, "
         "public ones included"),
    ])
    def test_who_may_use_the_runners(self, group, says):
        assert runner_groups.describe(group, ORG) == says

    def test_without_an_org_it_says_the_organisation(self):
        assert "every repository in the organisation" in runner_groups.describe(FILLZ, None)

    def test_the_fleets_policy_carries_the_sentence(self, monkeypatch):
        monkeypatch.setattr(github_api.GitHub, "runner_groups", lambda self: [FILLZ])
        runner_groups.refresh(ENV)
        policy = runner_groups.policy_for({"provider": "github", "runner_group": "Fillz"}, ENV)
        assert policy["says"] == (f"every repository in {ORG} may use these runners, "
                                  "public ones included")

    def test_settings_carries_it_for_each_group(self, monkeypatch):
        monkeypatch.setattr(github_api.GitHub, "runner_groups", lambda self: [FILLZ])
        runner_groups.refresh(ENV)
        (group,) = runner_groups.panel([], ENV)["groups"]
        assert group["says"].startswith(f"every repository in {ORG}")


# ---- what each runner reports, kept per runner -------------------------------

class TestEachRunnerSaysWhichCheckItCarries:
    @pytest.fixture
    def placed(self, tmp_path):
        from control.service import RunnerService
        from store.fleets import FleetStore
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed({})
        service = RunnerService(path, runtimes=dict(ALL_CELLS))
        service.inventory.register_worker("linux-1", inv.HYPERV_LINUX)
        rid = service.planned_ids(service.plan(GH, 1))[0]
        s = service.specs.get(rid)
        service.specs.update(rid, s["spec_version"], host_id="linux-1", actual_state="idle")
        return service, rid

    def beat(self, service, rid, **unit):
        service.inventory.accept_heartbeat("linux-1", {
            "host_id": "linux-1",
            "instances": [dict({"runner_id": rid, "state": "running"}, **unit)]})
        return cards.from_spec(service.specs.get(rid))["origin_guard"]

    def test_a_beat_records_it_and_the_card_carries_it(self, placed):
        service, rid = placed
        assert self.beat(service, rid, origin_guard=1) == 1
        assert self.beat(service, rid, origin_guard=0) == 0

    def test_a_beat_that_cannot_say_keeps_what_was_known(self, placed):
        """A stopped macOS guest cannot be read: its hooks did not change."""
        service, rid = placed
        self.beat(service, rid, origin_guard=1)
        assert self.beat(service, rid) == 1

    @pytest.mark.parametrize("garbage", [True, "1", -1, 1.5, {"v": 1}])
    def test_anything_but_a_whole_number_is_not_taken(self, placed, garbage):
        service, rid = placed
        assert self.beat(service, rid, origin_guard=garbage) is None

    def test_a_runner_never_reported_carries_none(self, placed):
        service, rid = placed
        assert cards.from_spec(service.specs.get(rid))["origin_guard"] is None


class TestTheFleetSums:
    def card(self, fleet, guard):
        return {"fleet_id": fleet, "key": f"k{id(object())}", "origin_guard": guard}

    def test_every_runner_and_how_many_carry_the_check(self):
        members = [self.card("github-linux-x64", 1), self.card("github-linux-x64", 0),
                   self.card("github-linux-x64", None), self.card("github-windows-x64", 1)]
        assert api_v2.origin_guard_of("github", "github-linux-x64", members) == {
            "runners": 3, "guarded": 1}

    def test_forgejo_has_no_hooks_to_count(self):
        assert api_v2.origin_guard_of("forgejo", "forgejo-linux-x64", []) is None

    def test_the_fleet_page_carries_it(self, client, plane):  # noqa: F811
        for fleet in client.get("/api/v2/fleet").json["fleets"]:
            if fleet["provider"] == "github":
                assert fleet["origin_guard"]["runners"] == len(fleet["runners"])
            else:
                assert fleet["origin_guard"] is None


# ---- the heading -------------------------------------------------------------

def policy(**over):
    p = {"known": True, "group": "Fillz", "visibility": "all", "org": ORG,
         "allows_public_repositories": True, "default": False, "source": "fleet setting",
         "says": f"every repository in {ORG} may use these runners, public ones included"}
    p.update(over)
    return p


def head(runner_group_policy, origin_guard):
    return render("fleetHeadHTML", [fleet_row(runner_group_policy=runner_group_policy,
                                              origin_guard=origin_guard)])[0]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTheHeading:
    def test_public_and_every_runner_refuses_outside_code_is_green(self):
        html = head(policy(), {"runners": 3, "guarded": 3})
        assert 'class="chip safe rgroup"' in html
        assert (f"Runner group Fillz: every repository in {ORG} may use these runners, "
                "public ones included") in html
        assert IN_PLACE in html
        assert "·" not in html.split("rgroup")[1], "no more terse dots"

    def test_public_and_no_runner_checks_is_red(self):
        html = head(policy(), {"runners": 2, "guarded": 0})
        assert 'class="chip danger rgroup"' in html
        assert IN_PLACE not in html
        assert "not refused by these runners yet" in html and "recreate" in html

    def test_public_and_only_some_runners_check_is_red(self):
        html = head(policy(), {"runners": 3, "guarded": 1})
        assert 'class="chip danger rgroup"' in html
        assert "refused by 1 of 3 runners" in html

    def test_no_public_repositories_and_no_check_is_amber(self):
        html = head(policy(allows_public_repositories=False,
                           says=f"every repository in {ORG} may use these runners, "
                                "but no public one"), {"runners": 2, "guarded": 0})
        assert 'class="chip warn rgroup"' in html
        assert "but no public one" in html

    def test_no_public_repositories_and_every_runner_checks_is_green(self):
        html = head(policy(allows_public_repositories=False), {"runners": 1, "guarded": 1})
        assert 'class="chip safe rgroup"' in html

    def test_a_fleet_with_no_runners_is_amber_whatever_the_group(self):
        """Nothing runs there to protect, and nothing yet proves a new runner
        will carry the check."""
        html = head(policy(), {"runners": 0, "guarded": 0})
        assert 'class="chip warn rgroup"' in html
        assert "No runners to check yet" in html

    def test_a_group_that_could_not_be_read_is_grey_with_the_protection_still_said(self):
        unknown = {"known": False, "group": "Fillz", "why": "GitHub could not be read"}
        html = head(unknown, {"runners": 2, "guarded": 0})
        assert 'class="chip unknown rgroup"' in html
        assert "Runner group Fillz: who may use these runners could not be read" in html
        assert 'title="GitHub could not be read"' in html
        assert "not refused by these runners yet" in html

    def test_a_group_that_could_not_be_read_with_every_runner_checking_is_green(self):
        unknown = {"known": False, "group": None, "why": "no GH_TOKEN"}
        html = head(unknown, {"runners": 2, "guarded": 2})
        assert 'class="chip safe rgroup"' in html and IN_PLACE in html

    def test_a_forge_without_runner_groups_has_no_chip(self):
        assert "rgroup" not in head(None, None)

    def test_what_github_said_is_escaped(self):
        html = head(policy(group="<b>x</b>", says="<i>y</i>"), {"runners": 1, "guarded": 1})
        assert "<b>" not in html and "<i>" not in html and "&lt;b&gt;" in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_settings_says_it_in_plain_words():
    from tests.test_resource_limits import run_page_js
    panel = {"known": True, "error": None, "at": "2026-10-09T10:00:00Z",
             "refresh_seconds": 300,
             "groups": [dict(FILLZ, fleets=["github-linux-x64"],
                             says=f"every repository in {ORG} may use these runners, "
                                  "public ones included")]}
    html = run_page_js("settings_v2.html", "function field(", "async function load()",
                       "runnerGroupsPanel(" + json.dumps(panel) + ")")
    assert f"every repository in {ORG} may use these runners, public ones included" in html
    assert "refuse a pull request from a fork" in html
    assert "<form" not in html and "<button" not in html
