import pytest

from agent.runtimes.linux_container import LinuxContainerRuntime
from agent.runtimes.macos_appliance import MacApplianceRuntime
from .fake_macos import FakeMac, TOOLS, TEMPLATE

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_mutable_linux_worker_never_advertises_or_creates_lent_hooks():
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        return True, "curated:guard", ""
    runtime = LinuxContainerRuntime(run=docker, readonly_root=False)
    assert runtime.capabilities()["federation_fork_guard"] is False
    assert runtime.capabilities()["federation_templates"] == []
    with pytest.raises(RuntimeError, match="mutable ordinary"):
        runtime.create(RID, {"image": "curated:guard", "env": {"RUNNER_REFUSE_ALL_FORKS": "1"}})
    assert not any(args[0] in ("run", "volume") for args in calls)


def test_system_macos_without_job_privilege_proof_cannot_lend():
    guest = FakeMac()
    runtime = MacApplianceRuntime(run=guest, fs=guest, tools=TOOLS)
    assert runtime.capabilities()["federation_fork_guard"] is False
    assert runtime.capabilities()["federation_templates"] == []
    with pytest.raises(RuntimeError, match="unprivileged"):
        runtime.create(RID, {"image": TEMPLATE, "env": {"RUNNER_REFUSE_ALL_FORKS": "1"}})
