"""T-0901: the GitHub adapter across Linux, Windows and macOS.

The six things design 10.4 lets a provider adapter do - registration,
deregistration, API status, tokens, labels, job information - asserted per
platform against recorded answers shaped like GitHub's own (the runners list
and the registration-token response of the REST API, 2022-11-28). What may
differ between platforms is the label defaults and nothing else; the last
class reads the adapter's source and fails on any other platform conditional.
"""
import ast
import inspect
import io
import json
import os
import urllib.error

import pytest

import github_api
import providers as P

GH = P.GITHUB
ENV = {"GH_TOKEN": "ghp_live-looking-token-000", "GITHUB_ORG": "NoMercy-Entertainment"}
CELLS = [(p, a) for p in P.PLATFORMS for a in (P.X64, P.ARM64)]

#: The org's runners list, one runner per platform, as GitHub returns it.
RECORDED_RUNNERS = {
    "total_count": 3,
    "runners": [
        {"id": 101, "name": "rnr-aaaa1111", "os": "linux", "status": "online",
         "busy": True, "version": "2.336.0",
         "labels": [{"id": 1, "name": "self-hosted", "type": "read-only"},
                    {"id": 2, "name": "Linux", "type": "read-only"},
                    {"id": 3, "name": "X64", "type": "read-only"}]},
        {"id": 102, "name": "rnr-bbbb2222", "os": "windows",
         "status": "online", "busy": False, "version": "2.336.0",
         "labels": [{"id": 1, "name": "self-hosted", "type": "read-only"},
                    {"id": 4, "name": "Windows", "type": "read-only"},
                    {"id": 3, "name": "X64", "type": "read-only"}]},
        {"id": 103, "name": "rnr-cccc3333", "os": "macos",
         "status": "offline", "busy": False, "version": "2.336.0",
         "labels": [{"id": 1, "name": "self-hosted", "type": "read-only"},
                    {"id": 5, "name": "macOS", "type": "read-only"},
                    {"id": 3, "name": "X64", "type": "read-only"}]},
    ],
}
RECORDED_TOKEN = {"token": "AABF3JGZDX3P5PMEXLND6TS6FCWO6",
                  "expires_at": "2026-09-18T12:00:00.000-00:00"}


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
    """GitHub's REST API as recorded: urlopen answers from the fixtures above
    and remembers every request it was asked."""
    seen = []
    state = {"delete_status": 204}

    def urlopen(req, timeout=None):
        method, url = req.get_method(), req.full_url
        seen.append((method, url))
        path = url.split("api.github.com", 1)[1].split("?")[0]
        if method == "POST" and path.endswith("/registration-token"):
            return Answer(201, json.dumps(RECORDED_TOKEN).encode())
        if method == "GET" and path.endswith("/actions/runners"):
            return Answer(200, json.dumps(RECORDED_RUNNERS).encode())
        if method == "GET" and path.endswith("/runner-groups"):
            return Answer(200, json.dumps({"total_count": 0,
                                           "runner_groups": []}).encode())
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

    monkeypatch.setattr(github_api.urllib.request, "urlopen", urlopen)
    return {"seen": seen, "state": state}


def spec_for(platform, arch=P.X64, **kw):
    return dict({"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                 "provider": "github", "platform": platform,
                 "architecture": arch}, **kw)


class TestLabels:
    @pytest.mark.parametrize("platform,arch", CELLS)
    def test_each_cell_defaults_to_githubs_own_three(self, platform, arch):
        os_label = {"linux": "Linux", "windows": "Windows",
                    "macos": "macOS"}[platform]
        assert GH.default_labels(platform, arch, {}) == \
            f"self-hosted,{os_label},{'X64' if arch == P.X64 else 'ARM64'}"

    @pytest.mark.parametrize("platform", [P.WINDOWS, P.MACOS])
    def test_the_linux_fleets_variable_is_not_inherited(self, platform):
        """RUNNER_LABELS is what the Linux fleet has always read. A Windows
        runner that picked it up would register as Linux and take Linux
        jobs."""
        env = {"RUNNER_LABELS": "self-hosted,Linux,X64,big"}
        assert "Linux" not in GH.default_labels(platform, P.X64, env)

    def test_each_platform_has_its_own_variable(self):
        env = {"RUNNER_LABELS": "a", "RUNNER_LABELS_WINDOWS": "b",
               "RUNNER_LABELS_MACOS": "c"}
        assert [GH.default_labels(p, P.X64, env) for p in P.PLATFORMS] == \
            ["a", "b", "c"]


class TestRegistration:
    @pytest.mark.parametrize("platform,arch", CELLS)
    def test_one_shape_for_every_cell(self, api, platform, arch):
        plan, error = GH.registration(spec_for(platform, arch), ENV)
        assert error is None
        assert plan.url == "https://github.com/NoMercy-Entertainment"
        assert plan.token == RECORDED_TOKEN["token"]
        assert plan.name == "rnr-3f2504e0"
        assert plan.labels == GH.default_labels(platform, arch, ENV)

    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_the_fleets_labels_and_group_win(self, api, platform):
        plan, _ = GH.registration(spec_for(platform, labels=["self-hosted",
                                                             "gpu"],
                                           runner_group="Stoney"), ENV)
        assert plan.labels == "self-hosted,gpu"
        assert plan.runner_group == "Stoney"

    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_the_token_is_minted_at_the_org_whatever_the_platform(
            self, api, platform):
        GH.registration(spec_for(platform), ENV)
        assert ("POST", "https://api.github.com/orgs/NoMercy-Entertainment/"
                        "actions/runners/registration-token") in api["seen"]

    def test_no_credentials_is_a_reason_not_a_call(self, api):
        plan, error = GH.registration(spec_for(P.WINDOWS), {})
        assert plan is None and "GH_TOKEN" in error
        assert api["seen"] == []


class TestStatusAndJobs:
    def test_the_forge_records_are_the_orgs_runner_list(self, api):
        records = GH.forge_records(ENV)
        assert [r["id"] for r in records] == [101, 102, 103]

    def test_no_credentials_is_unknown_not_empty(self):
        assert GH.forge_records({}) is None

    @pytest.mark.parametrize("rid,platform,state", [
        ("101", P.LINUX, P.BUSY), ("102", P.WINDOWS, P.IDLE),
        ("103", P.MACOS, P.OFFLINE)])
    def test_job_state_reads_the_same_way_on_every_platform(
            self, api, rid, platform, state):
        records = GH.forge_records(ENV)
        assert GH.job_state(spec_for(platform, registration_id=rid),
                            records) == state

    @pytest.mark.parametrize("rid,platform", [
        ("101", P.LINUX), ("102", P.WINDOWS), ("103", P.MACOS)])
    def test_a_record_says_which_platform_it_is_on(self, api, rid, platform):
        record = next(r for r in GH.forge_records(ENV) if str(r["id"]) == rid)
        assert GH.platform_of(record) == platform

    def test_an_os_github_invents_later_is_not_guessed(self):
        assert GH.platform_of({"os": "freebsd"}) is None


class TestDeregistration:
    @pytest.mark.parametrize("platform", P.PLATFORMS)
    def test_the_same_plan_on_every_platform(self, platform):
        d = GH.deregistration(spec_for(platform, registration_id="102"))
        assert d == GH.deregistration(spec_for(P.LINUX,
                                               registration_id="102"))

    def test_a_record_can_be_deleted_by_id(self, api):
        assert GH.delete_record(ENV, "102") is True
        assert ("DELETE", "https://api.github.com/orgs/NoMercy-Entertainment/"
                          "actions/runners/102") in api["seen"]

    def test_already_gone_counts_as_deleted(self, api):
        """A retried delete must not read as a failure."""
        api["state"]["delete_status"] = 404
        assert GH.delete_record(ENV, "102") is True

    @pytest.mark.parametrize("status", [500, 422, None])
    def test_anything_else_is_not_deleted(self, api, status):
        api["state"]["delete_status"] = status
        assert GH.delete_record(ENV, "102") is False

    @pytest.mark.parametrize("bad", ["", None, "../102", "102;x"])
    def test_an_id_that_is_not_a_number_is_never_sent(self, api, bad):
        assert GH.delete_record(ENV, bad) is False
        assert not any(m == "DELETE" for m, _ in api["seen"])


class TestTheAdapterKnowsNothingElseAboutPlatforms:
    """T-0901's definition of done: no platform conditional beyond label
    defaults. The platform names may appear in the class's tables - that is
    data - but never in a comparison or a branch."""

    PLATFORM_WORDS = {"LINUX", "WINDOWS", "MACOS"}
    PLATFORM_STRINGS = {"linux", "windows", "macos"}

    def _class_node(self):
        tree = ast.parse(inspect.getsource(P))
        return next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "_GitHub")

    def test_no_branch_or_comparison_names_a_platform(self):
        offenders = []
        for node in ast.walk(self._class_node()):
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

    def test_the_check_would_catch_one(self):
        """A check that cannot fail proves nothing."""
        src = ("class _GitHub:\n    def f(self, p):\n"
               "        if p == WINDOWS:\n            return 1\n")
        node = ast.parse(src).body[0]
        found = [s for n in ast.walk(node) if isinstance(n, ast.If)
                 for s in ast.walk(n.test)
                 if isinstance(s, ast.Name) and s.id in self.PLATFORM_WORDS]
        assert found

    def test_the_api_client_imports_no_runtime(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "github_api.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"runtime", "docker_ops", "app", "control",
                               "agent"}


# ---------------------------------------------------------------------------
# T-0902: the fleet's labels and group reach the forge, and a drift shows
# ---------------------------------------------------------------------------

from tests.test_partial_failure import GH as GH_FLEET  # noqa: E402
from tests.test_partial_failure import passes, the_runner  # noqa: E402,F401
from tests.test_partial_failure import world  # noqa: E402,F401


class TestLabelDrift:
    def test_githubs_own_labels_are_not_drift(self):
        record = {"labels": ["self-hosted", "Linux", "X64", "gpu"]}
        assert GH.registration_drift({"labels": "gpu"}, record) is None

    def test_a_missing_label_is(self):
        record = {"labels": ["self-hosted", "Linux", "X64"]}
        assert "missing labels gpu" in GH.registration_drift(
            {"labels": "self-hosted,gpu"}, record)

    def test_an_extra_one_is(self):
        record = {"labels": ["self-hosted", "Linux", "X64", "windows-only"]}
        assert "unexpected labels windows-only" in GH.registration_drift(
            {"labels": "self-hosted"}, record)

    def test_a_different_group_is(self):
        record = {"labels": ["self-hosted"], "runner_group": "Default"}
        assert "runner group is 'Default'" in GH.registration_drift(
            {"labels": "self-hosted", "runner_group": "Stoney"}, record)

    def test_a_record_that_says_nothing_is_not_drift(self):
        """Unknown proves nothing either way."""
        assert GH.registration_drift({"labels": "gpu"}, {"id": 1}) is None
        assert GH.registration_drift({"labels": "gpu"}, None) is None

    def test_forgejo_compares_names_not_how_they_run(self):
        record = {"labels": ["docker", "ubuntu-latest"]}
        assert P.FORGEJO.registration_drift(
            {"labels": "docker:docker://node:20,ubuntu-latest:docker://x"},
            record) is None
        assert "missing labels arm" in P.FORGEJO.registration_drift(
            {"labels": "docker:docker://node:20,arm:host"}, record)


class TestADriftIsVisible:
    def _with_labels(self, forges, labels, group=None):
        plain = forges.records

        def records(provider):
            return [dict(r, labels=list(labels),
                         **({"runner_group": group} if group else {}))
                    for r in plain(provider)]
        forges.records = records

    def test_the_fleets_labels_reach_the_registration(self, world):
        service, flow, agent, forges, reconciler = world
        from store import schema as _schema
        with _schema.connect(service.specs.path) as c:
            c.execute("UPDATE fleets SET labels = ?, runner_group = ?"
                      " WHERE fleet_id = ?",
                      ('["self-hosted", "gpu"]', "Stoney", GH_FLEET))
        seen = []
        real = agent.register
        agent.register = lambda h, ref, plan: (
            seen.append((plan.labels, plan.runner_group)),
            real(h, ref, plan))[1]
        self._with_labels(forges, ["self-hosted", "Linux", "X64", "gpu"],
                          "Stoney")
        service.create(GH_FLEET)
        passes(service, reconciler)
        assert seen == [("self-hosted,gpu", "Stoney")]
        assert the_runner(service)["last_error"] is None

    def test_a_drift_is_recorded_on_the_runner_and_it_still_serves(
            self, world):
        service, flow, agent, forges, reconciler = world
        from store import schema as _schema
        with _schema.connect(service.specs.path) as c:
            c.execute("UPDATE fleets SET labels = ? WHERE fleet_id = ?",
                      ('["self-hosted", "gpu"]', GH_FLEET))
        self._with_labels(forges, ["self-hosted", "Linux", "X64"])
        service.create(GH_FLEET)
        passes(service, reconciler)
        runner = the_runner(service)
        assert runner["actual_state"] == "idle"
        # A note, not an error: the runner serves, it just takes different
        # jobs than the fleet asked for.
        assert "missing labels gpu" in runner["last_note"]
        assert runner["last_error"] is None
