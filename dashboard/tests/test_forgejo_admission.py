import json

import pytest

from forgejo_admission import ENVIRONMENT, configured_policy, policy
from providers import FORGEJO

INSTANCE = "https://forgejo.example"
DOCUMENT = {"version": 1, "instance": INSTANCE, "allowed_owner_ids": [1, 7],
            "trusted_source_owner_ids": []}


def test_policy_canonicalizes_without_accepting_task_or_github_names():
    result = policy(json.dumps(DOCUMENT, indent=2), INSTANCE + "/")
    assert "\n" not in result and json.loads(result) == DOCUMENT
    assert "RUNNER_TRUSTED_OWNERS" not in result


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"allowed_owner_ids": []},
    {"allowed_owner_ids": [1, 1]}, {"allowed_owner_ids": [True]},
    {"allowed_owner_ids": [2**53]}, {"trusted_source_owner_ids": None},
    {"allowed_owner_ids": list(range(1, 102))}, {"actor": "FiLL"},
    {"instance": "https://other.example"}, {"instance": "http://forgejo.example"},
    {"instance": "https://forgejo.example/path"}, {"instance": "https://forgejo.example?x=1"},
    {"instance": "https://user@forgejo.example"},
])
def test_invalid_or_ambiguous_policy_is_refused(changes):
    with pytest.raises(ValueError):
        policy(json.dumps(dict(DOCUMENT, **changes)), INSTANCE)


@pytest.mark.parametrize("raw", ["", "{}", "null", "[]", "{" * 16385,
                                    json.dumps(DOCUMENT) + "{}",
                                    json.dumps(DOCUMENT).replace('"version": 1', '"version": 1,"version": 1')])
def test_malformed_policy_is_refused(raw):
    with pytest.raises(ValueError):
        policy(raw, INSTANCE)


def test_global_registration_cannot_be_requested_without_policy(monkeypatch):
    import forgejo_api
    calls = []
    monkeypatch.setattr(forgejo_api.Forgejo, "registration_token", lambda self: calls.append(self.runners_path))
    env = {"FORGEJO_INSTANCE_URL": INSTANCE, "FORGEJO_API_TOKEN": "test",
           "FORGEJO_RUNNER_SCOPE": "instance", "RUNNER_TRUSTED_OWNERS": "FiLL"}
    with pytest.raises(ValueError, match="missing"):
        FORGEJO.forge_client(env)
    assert calls == []
    env[ENVIRONMENT] = json.dumps(DOCUMENT)
    assert FORGEJO.forge_client(env).runners_path == "/api/v1/admin/actions/runners"
    assert configured_policy({}) == ""


def test_protected_policy_reaches_only_forgejo_on_all_platforms():
    from control.agent_runtime import AgentRuntime, AgentWiring
    canonical = policy(json.dumps(DOCUMENT), INSTANCE)
    runtime = AgentRuntime(AgentWiring(None, forgejo_admission_policy=canonical,
                                     trusted_owners="github-owner"), "worker")
    for platform in ("linux", "windows", "macos"):
        spec = {"platform": platform, "provider": "forgejo", "runtime_template": "owned-template"}
        assert runtime.unit_spec(spec)["env"] == {ENVIRONMENT: canonical}
        spec["provider"] = "github"
        assert ENVIRONMENT not in runtime.unit_spec(spec)["env"]


@pytest.mark.parametrize("scope,authority", [("user", "user"), ("instance", "admin")])
def test_registration_observation_and_deletion_use_the_same_scope(scope, authority, monkeypatch):
    from forgejo_api import Forgejo
    client = Forgejo(INSTANCE, "test", scope)
    route = f"/api/v1/{authority}/actions/runners"
    reads, deletes = [], []
    def get(path, params=None):
        reads.append(path)
        return {"token": "registration"} if path.endswith("registration-token") else [{"id": 10, "uuid": "owned"}]
    client._get = get
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr("forgejo_api.urllib.request.urlopen", lambda req, timeout: deletes.append(req.full_url) or Reply())
    assert client.registration_token() == "registration"
    assert client.runner_ids() == {"owned": 10}
    assert client.runner_statuses()[0]["uuid"] == "owned"
    assert client.delete_runner(10)
    assert reads == [route + "/registration-token", route, route]
    assert deletes == [INSTANCE + route + "/10"]


def test_unknown_scope_is_refused():
    from forgejo_api import Forgejo
    with pytest.raises(ValueError):
        Forgejo(INSTANCE, "test", "organization")


def test_instance_registration_plan_keeps_its_guard_requirement_for_old_workers():
    from control.agent_client import AgentError
    from control.agent_runtime import AgentWiring, FlowAgent
    from providers import RegistrationPlan
    from runtime.base import ExecUnitKind, ExecUnitRef
    class OldWorker:
        def __init__(self): self.calls = []
        def call_and_wait(self, host, verb, body, **kwargs):
            self.calls.append(body)
            raise AgentError(400, "plan carries fields this verb does not take: ['forgejo_admission_policy']")
    client = OldWorker()
    plan = RegistrationPlan(INSTANCE, "registration", "runner", "linux-x64",
                            extra=(("forgejo_admission_policy", json.dumps(DOCUMENT)),))
    ref = ExecUnitRef(ExecUnitKind.LINUX_CONTAINER, "rnr-3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    with pytest.raises(AgentError):
        FlowAgent(AgentWiring(client)).register("worker", ref, plan)
    assert len(client.calls) == 1
    assert client.calls[0]["plan"]["forgejo_admission_policy"] == json.dumps(DOCUMENT)
