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
from datetime import datetime, timedelta, timezone

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

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def guard(version=1, note=None, last="allowed", minutes_ago=1):
    return {"version": version, "note": note, "at": stamp(minutes_ago),
            "last": None if last is None else {"result": last, "at": stamp(30)}}


class TestEachRunnerSaysWhetherItsHookRefusesOutsideCode:
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

    def test_a_beat_records_the_report_and_the_card_carries_it(self, placed):
        service, rid = placed
        report = guard()
        assert self.beat(service, rid, origin_guard=report) == report

    def test_a_beat_that_cannot_say_keeps_what_was_known_with_its_time(self, placed):
        """The dashboard decides when that is too old (origin_guard_of)."""
        service, rid = placed
        report = guard()
        self.beat(service, rid, origin_guard=report)
        assert self.beat(service, rid) == report

    @pytest.mark.parametrize("garbage", [
        1, True, "1", {"version": "1", "at": stamp(1)}, {"version": 1},
        {"version": -1, "at": stamp(1)}, {"version": 1, "at": "yesterday"},
        {"version": 1, "at": stamp(1), "last": {"result": "maybe", "at": stamp(1)}},
        {"version": 1, "at": stamp(1), "note": "x" * 500}])
    def test_anything_but_a_well_formed_report_is_not_taken(self, placed, garbage):
        service, rid = placed
        assert self.beat(service, rid, origin_guard=garbage) is None

    def test_a_runner_never_reported_carries_none(self, placed):
        service, rid = placed
        assert cards.from_spec(service.specs.get(rid))["origin_guard"] is None


class TestTheFleetSums:
    def summary(self, *reports):
        members = [{"fleet_id": "github-linux-x64", "origin_guard": r} for r in reports]
        members.append({"fleet_id": "github-windows-x64", "origin_guard": guard()})
        return api_v2.origin_guard_of("github", "github-linux-x64", members, now=NOW)

    def test_each_runner_counts_once_in_what_it_proves(self):
        got = self.summary(guard(), guard(last="refused"), guard(last=None),
                           guard(last="unread"), guard(last="failed"),
                           guard(version=0, note="hooks from before the check"),
                           guard(minutes_ago=45), None)
        assert {k: got[k] for k in ("runners", "proven", "unproven", "unread",
                                    "missing", "unknown")} == {
            "runners": 8, "proven": 2, "unproven": 1, "unread": 2, "missing": 1, "unknown": 2}
        assert "hooks from before the check" in got["notes"]

    def test_a_report_older_than_four_deep_passes_is_unknown(self):
        assert self.summary(guard(minutes_ago=19))["proven"] == 1
        assert self.summary(guard(minutes_ago=21))["unknown"] == 1

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


def counts(runners, proven=0, unproven=0, unread=0, missing=0, unknown=0, notes=()):
    return {"runners": runners, "proven": proven, "unproven": unproven, "unread": unread,
            "missing": missing, "unknown": unknown, "notes": list(notes)}


def head(runner_group_policy, origin_guard):
    return render("fleetHeadHTML", [fleet_row(runner_group_policy=runner_group_policy,
                                              origin_guard=origin_guard)])[0]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTheHeading:
    def test_green_only_when_every_runner_has_proven_the_check(self):
        html = head(policy(), counts(3, proven=3))
        assert 'class="chip safe rgroup"' in html
        assert (f"Runner group Fillz: every repository in {ORG} may use these runners, "
                "public ones included") in html
        assert IN_PLACE in html
        assert "·" not in html.split("rgroup")[1], "no more terse dots"

    def test_public_and_no_runner_carries_the_check_is_red(self):
        html = head(policy(), counts(2, missing=2, notes=["hooks from before the check"]))
        assert 'class="chip danger rgroup"' in html
        assert IN_PLACE not in html
        assert "not refused by these runners yet" in html and "recreate" in html
        assert 'title="hooks from before the check"' in html

    def test_public_and_only_some_runners_carry_it_is_red(self):
        html = head(policy(), counts(3, proven=1, missing=2))
        assert 'class="chip danger rgroup"' in html
        assert "refused by 1 of 3 runners" in html

    def test_a_runner_that_could_not_be_checked_lately_is_not_counted_as_safe(self):
        html = head(policy(), counts(2, proven=1, unknown=1))
        assert 'class="chip danger rgroup"' in html
        assert "1 could not be checked lately" in html

    def test_every_runner_carries_it_but_one_has_not_run_a_job_yet_is_amber(self):
        html = head(policy(), counts(2, proven=1, unproven=1))
        assert 'class="chip warn rgroup"' in html
        assert "1 of 2 have not run a job with it yet" in html

    def test_a_last_job_whose_event_could_not_be_read_is_amber_and_says_so(self):
        html = head(policy(), counts(2, proven=1, unread=1))
        assert 'class="chip warn rgroup"' in html
        assert "the last job on 1 of 2 could not read its event" in html

    def test_no_public_repositories_and_no_check_is_amber(self):
        html = head(policy(allows_public_repositories=False,
                           says=f"every repository in {ORG} may use these runners, "
                                "but no public one"), counts(2, missing=2))
        assert 'class="chip warn rgroup"' in html
        assert "but no public one" in html

    def test_a_fleet_with_no_runners_is_amber_whatever_the_group(self):
        html = head(policy(), counts(0))
        assert 'class="chip warn rgroup"' in html
        assert "No runners to check yet" in html

    def test_a_group_that_could_not_be_read_is_grey_with_the_protection_still_said(self):
        unknown = {"known": False, "group": "Fillz", "why": "GitHub could not be read"}
        html = head(unknown, counts(2, missing=2))
        assert 'class="chip unknown rgroup"' in html
        assert "Runner group Fillz: who may use these runners could not be read" in html
        assert 'title="GitHub could not be read"' in html
        assert "not refused by these runners yet" in html

    def test_a_group_that_could_not_be_read_with_every_runner_proven_is_green(self):
        html = head({"known": False, "group": None, "why": "no GH_TOKEN"}, counts(2, proven=2))
        assert 'class="chip safe rgroup"' in html and IN_PLACE in html

    def test_a_forge_without_runner_groups_has_no_chip(self):
        assert "rgroup" not in head(None, None)

    def test_what_github_or_a_runner_said_is_escaped(self):
        html = head(policy(group="<b>x</b>", says="<i>y</i>"),
                    counts(1, missing=1, notes=['<script>"']))
        assert "<b>" not in html and "<i>" not in html and "<script>" not in html
        assert "&lt;b&gt;" in html



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
