"""T-1001: the Forgejo adapter across Linux, Windows and macOS.

As T-0901 for GitHub: the six things a provider adapter does, per platform,
against recorded answers shaped like Forgejo's own (`/api/v1/user/actions/
runners`, a bare array; the registration-token object). What is particular to
Forgejo: two of its three platforms have no published runner, so a cell whose
self-built artefact is not configured must answer with its reason - as data,
never as a crash.
"""
import ast
import inspect
import io
import json
import urllib.error

import pytest

import forgejo_api
import providers as P

FJ = P.FORGEJO
BASE = {"FORGEJO_INSTANCE_URL": "https://git.nomercy.tv",
        "FORGEJO_API_TOKEN": "fj-live-looking-token-000",
        "FORGEJO_RUNNER_LABELS": "docker:docker://node:20",
        "FORGEJO_RUNNER_LABELS_WINDOWS": "windows:host",
        "FORGEJO_RUNNER_LABELS_MACOS": "macos:host"}
BUILT = dict(BASE, FORGEJO_RUNNER_ARTIFACT_WINDOWS="forgejo-runner.exe",
             FORGEJO_RUNNER_ARTIFACT_MACOS="forgejo-runner-darwin-amd64")

#: The user's runners, one per platform, as Forgejo returns them.
RECORDED_RUNNERS = [
    {"id": 11, "uuid": "u-linux", "name": "rnr-aaaa1111", "status": "active",
     "version": "v12.0.1", "labels": ["docker"]},
    {"id": 12, "uuid": "u-windows", "name": "rnr-bbbb2222", "status": "idle",
     "version": "dev", "labels": ["windows"]},
    {"id": 13, "uuid": "u-macos", "name": "rnr-cccc3333",
     "status": "offline", "version": "v12.0.1", "labels": ["macos"]},
]
RECORDED_TOKEN = {"token": "fjrt-3b7c1e0a9f"}


class Answer:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def api(monkeypatch):
    seen = []
    state = {"delete_status": 204}

    def urlopen(req, timeout=None):
        method, url = req.get_method(), req.full_url
        seen.append((method, url))
        path = url.split("git.nomercy.tv", 1)[1].split("?")[0]
        if path == "/api/v1/user/actions/runners/registration-token":
            return Answer(200, json.dumps(RECORDED_TOKEN).encode())
        if method == "GET" and path == "/api/v1/user/actions/runners":
            return Answer(200, json.dumps(RECORDED_RUNNERS).encode())
        if method == "DELETE":
            status = state["delete_status"]
            if status is None:
                raise OSError("connection reset")
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "x", {},
                                             io.BytesIO(b""))
            return Answer(status)
        raise urllib.error.HTTPError(url, 404, "Not Found", {},
                                     io.BytesIO(b""))

    monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", urlopen)
    return {"seen": seen, "state": state}


def spec_for(platform, arch=P.X64, **kw):
    return dict({"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                 "provider": "forgejo", "platform": platform,
                 "architecture": arch}, **kw)


class TestAnUnavailableCellIsData:
    @pytest.mark.parametrize("platform", [P.WINDOWS, P.MACOS])
    def test_without_its_artefact_it_answers_with_the_reason(self, api,
                                                             platform):
        plan, error = FJ.registration(spec_for(platform), BASE)
        assert plan is None
        assert "self-built" in error and "FORGEJO_RUNNER_ARTIFACT_" in error
        assert api["seen"] == [], "no token is minted for a cell that " \
                                  "does not exist"

    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_with_it_the_cell_registers(self, api, platform):
        plan, error = FJ.registration(spec_for(platform), BUILT)
        assert error is None
        assert plan.token == RECORDED_TOKEN["token"]
        assert plan.url == "https://git.nomercy.tv"

    def test_a_platform_that_does_not_exist_is_a_reason_too(self, api):
        plan, error = FJ.registration(spec_for("plan9"), BUILT)
        assert plan is None and "unknown platform" in error


class TestLabels:
    def test_each_platform_reads_its_own_default(self):
        assert [FJ.default_labels(p, P.X64, BUILT) for p in P.PLATFORMS] == \
            ["docker:docker://node:20", "windows:host", "macos:host"]

    @pytest.mark.parametrize("platform", [P.WINDOWS, P.MACOS])
    def test_the_linux_fleets_labels_are_not_inherited(self, platform):
        """A docker:// label on a runner with no engine sends it jobs it
        cannot run."""
        env = {"FORGEJO_RUNNER_LABELS": "docker:docker://node:20"}
        assert FJ.default_labels(platform, P.X64, env) == ""

    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_no_labels_at_all_is_refused_before_a_token_is_minted(
            self, api, platform):
        env = {k: v for k, v in BUILT.items()
               if not k.startswith("FORGEJO_RUNNER_LABELS")}
        plan, error = FJ.registration(spec_for(platform), env)
        assert plan is None and "never picks up a job" in error
        assert api["seen"] == []

    def test_the_fleets_labels_win(self, api):
        plan, _ = FJ.registration(spec_for(P.MACOS, labels=["xcode:host"]),
                                  BUILT)
        assert plan.labels == "xcode:host"


class TestStatusAndJobs:
    def test_the_forge_records_are_the_users_runner_list(self, api):
        assert [r["uuid"] for r in FJ.forge_records(BUILT)] == \
            ["u-linux", "u-windows", "u-macos"]

    def test_no_token_is_unknown_not_empty(self):
        assert FJ.forge_records({}) is None

    @pytest.mark.parametrize("uuid,platform,state", [
        ("u-linux", P.LINUX, P.BUSY), ("u-windows", P.WINDOWS, P.IDLE),
        ("u-macos", P.MACOS, P.OFFLINE)])
    def test_job_state_reads_the_same_way_on_every_platform(
            self, api, uuid, platform, state):
        records = FJ.forge_records(BUILT)
        assert FJ.job_state(spec_for(platform, registration_uuid=uuid),
                            records) == state


class TestDeregistration:
    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_the_same_plan_on_every_platform_and_always_the_api(self,
                                                                platform):
        d = FJ.deregistration(spec_for(platform, registration_id="12"))
        assert d.via_api is True
        assert d == FJ.deregistration(spec_for(P.LINUX,
                                               registration_id="12"))

    def test_a_record_is_deleted_by_id(self, api):
        assert FJ.delete_record(BUILT, "12") is True
        assert ("DELETE", "https://git.nomercy.tv/api/v1/user/actions/"
                          "runners/12") in api["seen"]

    def test_already_gone_counts_as_deleted(self, api):
        api["state"]["delete_status"] = 404
        assert FJ.delete_record(BUILT, "12") is True

    @pytest.mark.parametrize("status", [500, 403, None])
    def test_anything_else_is_not(self, api, status):
        api["state"]["delete_status"] = status
        assert FJ.delete_record(BUILT, "12") is False

    @pytest.mark.parametrize("bad", ["", None, "../12", "12?x=1"])
    def test_an_id_that_is_not_a_number_is_never_sent(self, api, bad):
        assert FJ.delete_record(BUILT, bad) is False
        assert not any(m == "DELETE" for m, _ in api["seen"])


class TestTheAdapterKnowsNothingElseAboutPlatforms:
    PLATFORM_WORDS = {"LINUX", "WINDOWS", "MACOS"}
    PLATFORM_STRINGS = {"linux", "windows", "macos"}

    def test_no_branch_or_comparison_names_a_platform(self):
        tree = ast.parse(inspect.getsource(P))
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "_Forgejo")
        offenders = []
        for node in ast.walk(cls):
            tests = []
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                tests.append(node.test)
            if isinstance(node, ast.Compare):
                tests.append(node)
            for t in tests:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name) and sub.id in \
                            self.PLATFORM_WORDS:
                        offenders.append(f"line {sub.lineno}: {sub.id}")
                    if isinstance(sub, ast.Constant) and \
                            str(sub.value).lower() in self.PLATFORM_STRINGS:
                        offenders.append(f"line {sub.lineno}: {sub.value!r}")
        assert offenders == []
