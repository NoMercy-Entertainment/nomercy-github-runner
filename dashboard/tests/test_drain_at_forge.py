"""OPEN-7: a GitHub runner is drained at GitHub, and put back there.

The GitHub runner cancels its job on SIGTERM, so it cannot be drained on its
worker at all. GitHub is told instead to stop giving it jobs: by moving it into
a runner group no repository may use, when the deployment names one, or by
taking its custom labels off, which keeps away every job that asks for one of
them. These drive the provider against a stand-in for GitHub's API, and the
API client against a stand-in for the network.
"""
import io
import json
import urllib.error

import pytest

import github_api
import providers as P

ENV = {"GH_TOKEN": "t", "GITHUB_ORG": "NoMercy-Entertainment"}
SPEC = {"registration_id": "41", "platform": "linux", "architecture": "x64",
        "labels": "self-hosted,Linux,X64,beast-unit"}
READ_ONLY = [("self-hosted", "read-only"), ("Linux", "read-only"),
             ("X64", "read-only")]


class FakeGitHub:
    """The runner-editing half of GitHub's API, for one org."""

    org = "NoMercy-Entertainment"

    def __init__(self):
        self.labels = {"41": READ_ONLY + [("beast-unit", "custom")]}
        self.groups = {"41": "Default"}
        self.known_groups = {"Default", "Stoney", "drained"}
        self.calls = []
        self.fail = set()

    def runner_labels(self, rid):
        self.calls.append(("labels", rid))
        if "labels" in self.fail:
            return None
        return list(self.labels.get(rid, []))

    def remove_custom_labels(self, rid):
        self.calls.append(("remove", rid))
        if "remove" in self.fail:
            return None
        self.labels[rid] = [x for x in self.labels[rid]
                            if x[1] == "read-only"]
        return list(self.labels[rid])

    def set_custom_labels(self, rid, labels):
        self.calls.append(("set", rid, tuple(labels)))
        if "set" in self.fail:
            return None
        self.labels[rid] = [x for x in self.labels[rid]
                            if x[1] == "read-only"] + [
            (n, "custom") for n in labels]
        return list(self.labels[rid])

    def move_runner_to_group(self, group, rid):
        self.calls.append(("move", group, rid))
        if "move" in self.fail or group not in self.known_groups:
            return False
        self.groups[rid] = group
        return True

    def custom(self, rid="41"):
        return sorted(n for n, kind in self.labels[rid] if kind == "custom")


@pytest.fixture
def github(monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: gh)
    return gh


class TestTheTwoForgesDrainInOppositePlaces:
    def test_github_at_the_forge(self):
        assert P.GITHUB.drain_plan(SPEC).via_forge is True

    def test_forgejo_on_its_worker(self):
        assert P.FORGEJO.drain_plan(SPEC).via_forge is False

    def test_forgejo_is_never_drained_at_its_forge(self):
        with pytest.raises(NotImplementedError, match="worker"):
            P.FORGEJO.drain_at_forge(ENV, SPEC)


class TestByLabels:
    def test_the_custom_labels_come_off_and_the_others_stay(self, github):
        P.GITHUB.drain_at_forge(ENV, SPEC)
        assert github.custom() == []
        assert [n for n, _ in github.labels["41"]] == ["self-hosted",
                                                       "Linux", "X64"]

    def test_draining_twice_is_draining_once(self, github):
        """Asked again on every pass until the runner is drained."""
        P.GITHUB.drain_at_forge(ENV, SPEC)
        P.GITHUB.drain_at_forge(ENV, SPEC)
        assert github.custom() == []

    def test_cancelling_puts_the_fleets_labels_back(self, github):
        P.GITHUB.drain_at_forge(ENV, SPEC)
        P.GITHUB.undrain_at_forge(ENV, SPEC)
        assert github.custom() == ["beast-unit"]

    def test_a_runner_that_was_never_drained_is_left_alone(self, github):
        """What a start does, so it must be harmless."""
        P.GITHUB.undrain_at_forge(ENV, SPEC)
        assert not [c for c in github.calls if c[0] == "set"]
        assert github.custom() == ["beast-unit"]

    def test_labels_are_compared_as_github_does_without_case(self, github):
        github.labels["41"] = READ_ONLY + [("Beast-Unit", "custom")]
        P.GITHUB.undrain_at_forge(ENV, SPEC)
        assert not [c for c in github.calls if c[0] == "set"]

    def test_a_fleet_with_no_custom_label_is_refused(self, github):
        """Taking nothing off keeps no job away. Pretending otherwise would
        call a runner drained that GitHub still sends every job."""
        spec = dict(SPEC, labels="self-hosted,Linux,X64")
        with pytest.raises(RuntimeError, match="GITHUB_DRAIN_GROUP"):
            P.GITHUB.drain_at_forge(ENV, spec)
        assert not [c for c in github.calls if c[0] == "remove"]

    @pytest.mark.parametrize("step", ["labels", "remove"])
    def test_a_drain_github_did_not_confirm_raises(self, github, step):
        github.fail.add(step)
        with pytest.raises(RuntimeError, match="41"):
            P.GITHUB.drain_at_forge(ENV, SPEC)

    def test_a_custom_label_still_there_afterwards_is_not_a_drain(
            self, github, monkeypatch):
        monkeypatch.setattr(github, "remove_custom_labels",
                            lambda rid: READ_ONLY + [("beast-unit",
                                                      "custom")])
        with pytest.raises(RuntimeError, match="not confirm"):
            P.GITHUB.drain_at_forge(ENV, SPEC)

    def test_a_put_back_github_did_not_confirm_raises(self, github):
        P.GITHUB.drain_at_forge(ENV, SPEC)
        github.fail.add("set")
        with pytest.raises(RuntimeError, match="put back"):
            P.GITHUB.undrain_at_forge(ENV, SPEC)


class TestByGroup:
    ENV = dict(ENV, GITHUB_DRAIN_GROUP="drained")

    def test_the_runner_is_moved_and_its_labels_left(self, github):
        P.GITHUB.drain_at_forge(self.ENV, SPEC)
        assert github.groups["41"] == "drained"
        assert github.custom() == ["beast-unit"]

    def test_cancelling_moves_it_back_to_its_fleets_group(self, github):
        P.GITHUB.drain_at_forge(self.ENV, SPEC)
        P.GITHUB.undrain_at_forge(self.ENV, dict(SPEC, runner_group="Stoney"))
        assert github.groups["41"] == "Stoney"

    def test_or_to_the_default_group_when_the_fleet_names_none(self, github):
        P.GITHUB.drain_at_forge(self.ENV, SPEC)
        P.GITHUB.undrain_at_forge(self.ENV, SPEC)
        assert github.groups["41"] == "Default"

    def test_a_group_that_does_not_exist_is_a_failed_drain(self, github):
        with pytest.raises(RuntimeError, match="drain group"):
            P.GITHUB.drain_at_forge(dict(ENV, GITHUB_DRAIN_GROUP="nope"),
                                    SPEC)


class TestWhatIsNeeded:
    def test_no_token_no_drain(self, monkeypatch):
        monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: None)
        with pytest.raises(RuntimeError, match="GH_TOKEN"):
            P.GITHUB.drain_at_forge({}, SPEC)

    def test_no_runner_id_no_drain(self, github):
        with pytest.raises(RuntimeError, match="nothing identifies"):
            P.GITHUB.drain_at_forge(ENV, dict(SPEC, registration_id=None))
        assert github.calls == []


# ---------------------------------------------------------------------------
# the API client, against a stand-in for the network
# ---------------------------------------------------------------------------

class Answer(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if body is not None
                         else b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Every request the client makes, and the answer each gets."""
    sent, answers = [], []

    def urlopen(req, timeout=None):
        sent.append((req.get_method(), req.full_url,
                     json.loads(req.data) if req.data else None))
        status, body = answers.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "no", {},
                                         io.BytesIO(b""))
        return Answer(status, body)

    monkeypatch.setattr(github_api.urllib.request, "urlopen", urlopen)
    return sent, answers


BASE = "https://api.github.com/orgs/NoMercy-Entertainment/actions"
LEFT = {"total_count": 3, "labels": [
    {"id": 1, "name": "self-hosted", "type": "read-only"},
    {"id": 2, "name": "Linux", "type": "read-only"},
    {"id": 3, "name": "X64", "type": "read-only"}]}


class TestTheClient:
    def gh(self):
        return github_api.GitHub("t", "NoMercy-Entertainment")

    def test_custom_labels_are_removed_with_one_delete(self, wire):
        sent, answers = wire
        answers.append((200, LEFT))
        left = self.gh().remove_custom_labels(41)
        assert sent == [("DELETE", f"{BASE}/runners/41/labels", None)]
        assert left == [("self-hosted", "read-only"), ("Linux", "read-only"),
                        ("X64", "read-only")]

    def test_labels_are_set_with_one_put(self, wire):
        sent, answers = wire
        answers.append((200, LEFT))
        self.gh().set_custom_labels("41", ["beast-unit"])
        assert sent == [("PUT", f"{BASE}/runners/41/labels",
                         {"labels": ["beast-unit"]})]

    @pytest.mark.parametrize("status", [404, 422, 500])
    def test_anything_but_ok_is_not_confirmed(self, wire, status):
        wire[1].append((status, None))
        assert self.gh().remove_custom_labels("41") is None

    def test_no_answer_is_not_confirmed(self, monkeypatch):
        def down(req, timeout=None):
            raise OSError("unreachable")
        monkeypatch.setattr(github_api.urllib.request, "urlopen", down)
        assert self.gh().set_custom_labels("41", ["x"]) is None

    def test_a_runner_id_that_is_not_a_number_sends_nothing(self, wire):
        assert self.gh().remove_custom_labels("41/../../x") is None
        assert wire[0] == []

    def test_moving_to_a_group_finds_it_by_name_and_puts(self, wire):
        sent, answers = wire
        answers.extend([
            (200, {"runner_groups": [{"id": 1, "name": "Default"},
                                     {"id": 7, "name": "drained"}]}),
            (204, None)])
        assert self.gh().move_runner_to_group("Drained", "41") is True
        assert sent[1] == ("PUT", f"{BASE}/runner-groups/7/runners/41", None)

    def test_a_group_that_is_not_there_moves_nothing(self, wire):
        sent, answers = wire
        answers.append((200, {"runner_groups": [{"id": 1,
                                                 "name": "Default"}]}))
        assert self.gh().move_runner_to_group("drained", "41") is False
        assert len(sent) == 1
