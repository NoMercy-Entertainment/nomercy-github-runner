"""Registration and job state, per forge - what T-0003 named and did not build.

The audit of Phase 0 passed T-0003 on its definition of done, which was met.
Its symbol list was not: `registration(spec)` and `job_state(spec,
forge_status)` did not exist, and the dataclass that carries a registration
token called that field `token` while the redaction list only knew
`registration_token`. This file is what closes those three.

Every forge client below is a stand-in. Nothing here reaches GitHub or Forgejo;
minting a real token is FORGE-LIVE and belongs to phase 8 and 9.
"""
from dataclasses import asdict

import pytest

import providers as P

SENTINEL = "tok-SENTINEL-8f3a91c2"


class FakeGitHub:
    def __init__(self, token=SENTINEL, org="NoMercy-Entertainment"):
        self._token = token
        self.org = org
        self.minted = 0

    def registration_token(self):
        self.minted += 1
        return self._token


class FakeForgejo:
    def __init__(self, token=SENTINEL):
        self._token = token
        self.minted = 0

    def registration_token(self):
        self.minted += 1
        return self._token


@pytest.fixture
def github(monkeypatch):
    client = FakeGitHub()
    monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: client)
    return client


@pytest.fixture
def forgejo(monkeypatch):
    client = FakeForgejo()
    monkeypatch.setattr(P.FORGEJO, "forge_client", lambda env: client)
    return client


SPEC = {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "labels": ["self-hosted", "linux"], "runner_group": "beast"}
FORGEJO_ENV = {"FORGEJO_INSTANCE_URL": "https://git.example",
               "FORGEJO_RUNNER_LABELS": "docker:docker://node:20"}


class TestGitHubRegistration:
    def test_it_produces_a_plan(self, github):
        plan, error = P.GITHUB.registration(SPEC, {"GH_TOKEN": "x"})
        assert error is None
        assert plan.url == "https://github.com/NoMercy-Entertainment"
        assert plan.token == SENTINEL
        assert plan.labels == "self-hosted,linux"
        assert plan.runner_group == "beast"

    def test_it_mints_one_token_per_registration(self, github):
        P.GITHUB.registration(SPEC, {})
        assert github.minted == 1

    def test_no_client_is_a_reason_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: None)
        plan, error = P.GITHUB.registration(SPEC, {})
        assert plan is None
        assert "GH_TOKEN" in error

    def test_a_refused_mint_is_a_reason(self, monkeypatch):
        monkeypatch.setattr(P.GITHUB, "forge_client",
                            lambda env: FakeGitHub(token=None))
        plan, error = P.GITHUB.registration(SPEC, {})
        assert plan is None
        assert "no registration token" in error

    def test_labels_fall_back_to_the_deployment_default(self, github):
        plan, _ = P.GITHUB.registration({"runner_id": "r"},
                                        {"RUNNER_LABELS": "self-hosted,X64"})
        assert plan.labels == "self-hosted,X64"


class TestForgejoRegistration:
    def test_it_produces_a_plan(self, forgejo):
        plan, error = P.FORGEJO.registration(SPEC, FORGEJO_ENV)
        assert error is None
        assert plan.url == "https://git.example"
        assert plan.token == SENTINEL

    def test_every_offline_refusal_comes_before_a_token_is_minted(self,
                                                                   forgejo):
        """A registration that cannot succeed must not burn a token."""
        for env in ({}, {"FORGEJO_INSTANCE_URL": "https://git.example"}):
            plan, error = P.FORGEJO.registration({"runner_id": "r"}, env)
            assert plan is None and error
        assert forgejo.minted == 0

    def test_a_runner_with_no_labels_is_refused(self, forgejo):
        """It would register, look healthy, and never pick up a job."""
        plan, error = P.FORGEJO.registration(
            {"runner_id": "r"},
            {"FORGEJO_INSTANCE_URL": "https://git.example"})
        assert plan is None
        assert "never picks up a job" in error


class TestWhoseRegistrationIsAuthoritative:
    """The controller's, not the unit's.

    A unit keeps its registration files on its own volume and cannot be made
    to drop them - `deregister` leaves them deliberately. So a unit whose
    record the controller has since deleted still answers every later
    registration with the id of a record that no longer exists. When the
    controller holds no registration for a runner, the plan says so, and the
    unit registers rather than answering from its volume (2026-09-20).
    """

    def test_a_runner_the_controller_has_no_record_of_replaces(self, github):
        plan, _ = P.GITHUB.registration(SPEC, {"GH_TOKEN": "x"})
        assert plan.replace is True

    def test_one_it_does_keeps_what_it_has(self, github):
        plan, _ = P.GITHUB.registration(dict(SPEC, registration_id="2007"),
                                        {"GH_TOKEN": "x"})
        assert plan.replace is False

    def test_the_same_holds_at_the_other_forge(self, forgejo):
        plan, _ = P.FORGEJO.registration(SPEC, FORGEJO_ENV)
        assert plan.replace is True
        kept, _ = P.FORGEJO.registration(dict(SPEC, registration_id="9"),
                                         FORGEJO_ENV)
        assert kept.replace is False


class TestTheForgeNameIsNeverAnIdentity:
    def test_the_display_name_when_there_is_one(self, github):
        plan, _ = P.GITHUB.registration(
            dict(SPEC, display_name="github-runner-4"), {})
        assert plan.name == "github-runner-4"

    def test_otherwise_one_built_from_the_runner_id(self, github):
        plan, _ = P.GITHUB.registration(SPEC, {})
        assert plan.name == "rnr-3f2504e0"


class TestTheTokenIsRedacted:
    """The third open item: the field is `token`, the list said
    `registration_token`."""

    def test_the_plans_token_field_is_on_the_redaction_list(self):
        assert "token" in P.REDACTED_FIELDS

    def test_every_field_that_holds_the_token_is_redacted(self, github):
        """Found by value rather than by name, so a field renamed later cannot
        slip past the list the way this one did."""
        plan, _ = P.GITHUB.registration(SPEC, {})
        holding = [k for k, v in asdict(plan).items() if v == SENTINEL]
        assert holding, "sanity: the plan should carry the token somewhere"
        for field in holding:
            assert field in P.REDACTED_FIELDS, field

    def test_the_runner_detail_page_masks_it_too(self):
        """The list is the single source for runner_detail's masking."""
        import runner_detail
        assert "token" in runner_detail.SECRET_KEYS

    def test_no_error_message_contains_the_token(self, monkeypatch):
        for provider, env in ((P.GITHUB, {}), (P.FORGEJO, FORGEJO_ENV)):
            monkeypatch.setattr(provider, "forge_client",
                                lambda env: FakeGitHub(token=None))
            _, error = provider.registration(SPEC, env)
            assert SENTINEL not in (error or "")


class TestGitHubJobState:
    RECORDS = [
        {"id": 11, "status": "online", "busy": True},
        {"id": 12, "status": "online", "busy": False},
        {"id": 13, "status": "offline", "busy": False},
        {"id": 14, "status": "rebooting", "busy": False},
    ]

    def state(self, rid, records=RECORDS):
        return P.GITHUB.job_state({"registration_id": rid}, records)

    def test_busy_comes_straight_from_github(self):
        """Nothing inferred from a log line that may have scrolled away."""
        assert self.state("11") == P.BUSY

    def test_online_and_not_busy_is_idle(self):
        assert self.state("12") == P.IDLE

    def test_offline_stays_offline(self):
        """Not folded into idle: a process that is up while the forge says
        offline is a failure the platform must be able to see."""
        assert self.state("13") == P.OFFLINE

    def test_an_unrecognised_status_is_unknown(self):
        assert self.state("14") == P.UNKNOWN

    def test_a_runner_github_has_never_heard_of_is_unknown(self):
        assert self.state("99") == P.UNKNOWN

    def test_a_forge_that_could_not_be_asked_is_unknown_never_idle(self):
        """A wrong "idle" is what lets a cache clear act on a working
        runner."""
        assert self.state("12", records=None) == P.UNKNOWN

    def test_a_runner_with_no_registration_is_unknown(self):
        assert P.GITHUB.job_state({}, self.RECORDS) == P.UNKNOWN

    def test_the_id_matches_whatever_type_it_was_stored_as(self):
        """The store keeps registration_id as text; GitHub sends a number."""
        assert self.state(12) == P.IDLE


class TestForgejoJobState:
    RECORDS = [
        {"uuid": "a", "status": "active"},
        {"uuid": "b", "status": "idle"},
        {"uuid": "c", "status": "offline"},
        {"uuid": "d", "status": "something-new"},
    ]

    def state(self, uuid, records=RECORDS):
        return P.FORGEJO.job_state({"registration_uuid": uuid}, records)

    def test_active_is_busy(self):
        assert self.state("a") == P.BUSY

    def test_idle_is_idle(self):
        assert self.state("b") == P.IDLE

    def test_offline_stays_offline(self):
        """Today's docker_ops folds offline into idle for its busy/idle
        question. The platform keeps them apart, because ready means the
        forge shows the runner online."""
        assert self.state("c") == P.OFFLINE

    def test_a_word_from_a_future_release_is_unknown(self):
        assert self.state("d") == P.UNKNOWN

    def test_a_forge_that_could_not_be_asked_is_unknown(self):
        assert self.state("b", records=None) == P.UNKNOWN

    def test_a_runner_forgejo_has_never_heard_of_is_unknown(self):
        """A runner mid-registration has no record yet; calling it idle would
        let a cache clear act on a runner about to take work."""
        assert self.state("zzz") == P.UNKNOWN


class TestEveryProviderAnswers:
    def test_both_forges_implement_both_methods(self):
        for provider in P.ALL:
            assert type(provider).registration is not P.Provider.registration
            assert type(provider).job_state is not P.Provider.job_state

    def test_job_state_only_ever_answers_one_of_four_words(self):
        records = [{"id": 1, "uuid": "u", "status": "x", "busy": False}]
        for provider in P.ALL:
            for spec in ({}, {"registration_id": "1",
                              "registration_uuid": "u"}):
                for recs in (None, [], records):
                    assert provider.job_state(spec, recs) in (
                        P.BUSY, P.IDLE, P.OFFLINE, P.UNKNOWN)


class TestTheGitHubClientMintsATokenByPost:
    """The one POST the GitHub client makes, and it mints a credential."""

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_it_posts_to_the_orgs_registration_endpoint(self, monkeypatch):
        import github_api
        seen = {}

        def urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return self.Response(b'{"token": "%s"}' % SENTINEL.encode())

        monkeypatch.setattr(github_api.urllib.request, "urlopen", urlopen)
        token = github_api.GitHub("pat", "NoMercy-Entertainment") \
            .registration_token()

        assert token == SENTINEL
        assert seen["method"] == "POST"
        assert seen["url"].endswith(
            "/orgs/NoMercy-Entertainment/actions/runners/registration-token")
        assert seen["timeout"], "a call with no deadline can hang for ever"

    def test_a_failure_is_none_and_logs_no_body(self, monkeypatch, capsys):
        """The response body of a failed mint could carry the token; only the
        error's type is printed."""
        import github_api

        def urlopen(req, timeout=None):
            raise OSError(f"connection reset while reading {SENTINEL}")

        monkeypatch.setattr(github_api.urllib.request, "urlopen", urlopen)
        assert github_api.GitHub("pat", "o").registration_token() is None
        assert SENTINEL not in capsys.readouterr().out
