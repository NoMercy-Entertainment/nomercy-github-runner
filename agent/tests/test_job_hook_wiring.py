"""A GitHub runner on Windows or macOS is given the job hooks (GitHub #7) by
the runtime that makes it, from the agent's own copy, so a redeployed agent
and a recreated runner are all it takes. A Forgejo runner has no hooks to
give: forgejo-runner has no such mechanism.

GitHub runs a hook only when its path ends in .ps1, .sh or .js; any other
path fails the job's last step, and the job with it (2026-09-21, 83 of 83 on
Linux). Each path is checked against that rule.
"""
import json
import ntpath
from pathlib import Path

import pytest

from agent.runtimes import windows_process
from agent.runtimes.windows_process import WindowsProcessRuntime

from .fake_windows import TEMPLATE as WINDOWS_TEMPLATE, TOOLS as WINDOWS_TOOLS, FakeWindows

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
AGENT = Path(windows_process.__file__).resolve().parents[1]
HOOK_KEYS = ("ACTIONS_RUNNER_HOOK_JOB_STARTED", "ACTIONS_RUNNER_HOOK_JOB_COMPLETED")


def spec(provider, image):
    labels = {"nomercy.fleet": "fleet-1"}
    if provider:
        labels["nomercy.provider"] = provider
    return {"image": image, "memory": "8g", "cpus": "4", "labels": labels,
            "env": {"RUNNER_LABELS": "self-hosted"}}


class TestWindows:
    @pytest.fixture
    def host(self):
        return FakeWindows()

    @pytest.fixture
    def runtime(self, host):
        return WindowsProcessRuntime(run=host, fs=host, tools=WINDOWS_TOOLS)

    def env(self, runtime, host, provider):
        runtime.create(RID, spec(provider, WINDOWS_TEMPLATE))
        reg = runtime.paths(RID)["reg"]
        return json.loads(host.read_text(ntpath.join(reg, "unit.json")))["env"]

    def test_a_github_runner_gets_both_hooks_from_the_agents_own_copy(self, runtime, host):
        env = self.env(runtime, host, "github")
        assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"] == str(AGENT / "hooks" / "windows" / "job-started.ps1")
        assert env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"] == str(AGENT / "hooks" / "windows" / "job-completed.ps1")
        for key in HOOK_KEYS:
            assert env[key].endswith(".ps1") and Path(env[key]).is_file(), key

    def test_the_hooks_run_with_the_python_the_job_host_runs(self, runtime, host):
        env = self.env(runtime, host, "github")
        assert env["RUNNER_HOOK_PYTHON"] == WINDOWS_TOOLS["python"]

    @pytest.mark.parametrize("provider", ["forgejo", None])
    def test_a_runner_that_is_not_githubs_gets_no_hooks(self, runtime, host, provider):
        env = self.env(runtime, host, provider)
        assert not any(key in env for key in (*HOOK_KEYS, "RUNNER_HOOK_PYTHON"))

    def test_a_spec_cannot_point_the_hooks_elsewhere(self, runtime, host):
        unit = spec("github", WINDOWS_TEMPLATE)
        unit["env"]["ACTIONS_RUNNER_HOOK_JOB_STARTED"] = r"C:\elsewhere\job.ps1"
        runtime.create(RID, unit)
        reg = runtime.paths(RID)["reg"]
        env = json.loads(host.read_text(ntpath.join(reg, "unit.json")))["env"]
        assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"].startswith(str(AGENT))

    def test_every_hook_is_a_script_the_runner_accepts(self):
        for key in HOOK_KEYS:
            path = windows_process.HOOK_SCRIPTS[key]
            assert path.endswith(".ps1") and Path(path).is_file(), path
