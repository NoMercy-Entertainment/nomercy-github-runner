"""Maintenance awaits one completion; status fences still require exact False."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import providers
from control import provision, retry
from tests.test_provisioning_flow import platform, a_planned, provisioned  # noqa: F401


def registered(world, target):
    service, flow, agent, forges = world
    spec = provisioned(service, flow, a_planned(service, "forgejo", "linux"))
    values = flow.register(spec)
    service.specs.update(spec["runner_id"], spec["spec_version"], **values)
    spec = service.specs.get(spec["runner_id"])
    # Same collaborator protocol across platforms; no real worker is contacted.
    return dict(spec, platform=target[0], architecture=target[1])


def bounded_policies(monkeypatch):
    actual, policies = retry.call, []

    def measured(policy, function, *args, **kwargs):
        policies.append(policy)
        # Scale only test deadlines: real AGENT_SLOW remains 1800s/no retries.
        if policy is provision.AGENT_SLOW:
            scaled = retry.Policy("test-completion", 2, 0)
        elif policy.name.startswith("agent-fast"):
            scaled = retry.Policy("test-read", 0.02, policy.retries, (0,) * policy.retries)
        else:
            scaled = policy
        return actual(scaled, function, *args, **kwargs)

    monkeypatch.setattr(retry, "call", measured)
    return policies


@pytest.mark.parametrize("target", [("linux", "x64"), ("windows", "x64"), ("macos", "x64")])
def test_delayed_drain_waits_once_and_repeated_completion_is_idempotent(platform, monkeypatch, target):
    _, flow, agent, _ = platform
    spec = registered(platform, target)
    policies = bounded_policies(monkeypatch)
    entered, release, completed = threading.Event(), threading.Event(), set()
    original = agent.drain
    starts, overlap = [], []
    lock = threading.Lock()

    def delayed(host, ref):
        with lock:
            if ref.handle in completed:
                return
            if starts:
                overlap.append(True)
            starts.append(ref.handle)
        entered.set()
        assert release.wait(2)
        original(host, ref)
        completed.add(ref.handle)

    monkeypatch.setattr(agent, "drain", delayed)
    timer = threading.Timer(0.12, release.set)  # Longer than all short read attempts.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(flow.drain, spec)
        assert entered.wait(1)
        assert not future.done()
        timer.start()
        try:
            assert future.result(timeout=2) is True
            assert starts == [spec["exec_unit_ref"]] and overlap == []
            assert flow.drain(spec) is True
            assert len(starts) == 1
        finally:
            release.set()
            timer.cancel()
    agent_policies = [policy for policy in policies if policy.name.startswith("agent-")]
    assert agent_policies and all(policy is provision.AGENT_SLOW for policy in agent_policies)
    assert provision.AGENT_SLOW.timeout == 1800 and provision.AGENT_SLOW.retries == 0


@pytest.mark.parametrize("fence", ["drained", "quiescent"])
@pytest.mark.parametrize("target", [("windows", "x64"), ("macos", "x64")])
def test_delayed_running_confirmation_awaits_one_actual_reply(platform, monkeypatch, fence, target):
    _, flow, agent, _ = platform
    spec = registered(platform, target)
    policies = bounded_policies(monkeypatch)
    release, entered = threading.Event(), threading.Event()
    calls = []

    def running(host, ref):
        calls.append(ref.handle)
        entered.set()
        assert release.wait(2)
        return False

    monkeypatch.setattr(agent, "running", running)
    check = (lambda: flow._drained(spec, providers.FORGEJO, providers.IDLE)) if fence == "drained" else lambda: flow.quiescent(spec)
    timer = threading.Timer(0.12, release.set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(check)
        assert entered.wait(1) and not future.done()
        timer.start()
        try:
            assert future.result(timeout=2) is True
        finally:
            release.set()
            timer.cancel()
    assert calls == [spec["exec_unit_ref"]]
    assert policies == [provision.AGENT_SLOW]


@pytest.mark.parametrize("reply", [False, True, None, 0, "false"])
@pytest.mark.parametrize("fence", ["drained", "quiescent"])
def test_lifecycle_fence_requires_false_and_never_assumes_a_reply(platform, monkeypatch, fence, reply):
    _, flow, agent, _ = platform
    spec = registered(platform, ("macos", "x64"))
    calls = []
    monkeypatch.setattr(agent, "running", lambda host, ref: calls.append(ref.handle) or reply)
    observed = flow._drained(spec, providers.FORGEJO, providers.IDLE) if fence == "drained" else flow.quiescent(spec)
    assert observed is (reply is False)
    assert calls == [spec["exec_unit_ref"]]


def test_failed_async_completion_is_not_retried_or_confirmed_drained(platform, monkeypatch):
    _, flow, agent, _ = platform
    spec = registered(platform, ("macos", "x64"))
    policies = bounded_policies(monkeypatch)
    calls = []

    def failed(host, ref):
        calls.append(ref.handle)
        raise RuntimeError("owned maintenance completion failed")

    monkeypatch.setattr(agent, "drain", failed)
    monkeypatch.setattr(agent, "running", lambda *args: pytest.fail("failed completion was treated as drained"))
    with pytest.raises(RuntimeError, match="owned maintenance completion failed"):
        flow.drain(spec)
    assert calls == [spec["exec_unit_ref"]]
    assert policies == [provision.AGENT_SLOW]
