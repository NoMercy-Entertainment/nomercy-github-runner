import hashlib
from types import SimpleNamespace

import pytest

from agent.verbs import Agent, Refused, prepare

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
POLICY = '{"version":1}'


@pytest.mark.parametrize("guard", [None, {}, {"version": 1},
    {"version": 1, "provider": "github", "protected_control": True, "verified_binary": True},
    {"version": 1, "provider": "forgejo", "protected_control": True, "verified_binary": True,
     "policy_sha256": "different-policy"}])
def test_global_registration_never_reaches_forge_without_actual_policy_and_binary_proof(guard):
    calls = []
    runtime = SimpleNamespace(origin_guard_report=lambda rid: guard)
    registrar = SimpleNamespace(register=lambda rid, plan: calls.append(plan))
    agent = Agent("worker", runtime, registrar)
    body = {"runner_id": RID, "plan": {"url": "https://forgejo.example", "token": "registration",
                                      "forgejo_admission_policy": POLICY}}
    work = prepare(agent, "runner.register", body)
    for _ in range(2):
        with pytest.raises(Refused, match="verified protected"):
            work()
    assert not calls


def test_compatible_old_worker_without_guard_reports_refuses_global_registration():
    calls = []
    agent = Agent("worker", SimpleNamespace(),
                  SimpleNamespace(register=lambda *args: calls.append(args)))
    work = prepare(agent, "runner.register", {"runner_id": RID,
        "plan": {"url": "https://forgejo.example", "token": "registration", "forgejo_admission_policy": POLICY}})
    with pytest.raises(Refused):
        work()
    assert not calls


def test_verified_guard_does_not_pass_control_policy_to_runner_registration_command():
    guard = {"version": 1, "provider": "forgejo", "protected_control": True,
             "verified_binary": True, "policy_sha256": hashlib.sha256(POLICY.encode()).hexdigest()}
    calls = []
    agent = Agent("worker", SimpleNamespace(origin_guard_report=lambda rid: guard),
        SimpleNamespace(register=lambda rid, plan: calls.append(plan) or {"registration_id": 10}))
    work = prepare(agent, "runner.register", {"runner_id": RID,
        "plan": {"url": "https://forgejo.example", "token": "registration", "forgejo_admission_policy": POLICY}})
    assert work()["registration_id"] == "10"
    assert "forgejo_admission_policy" not in calls[0]
