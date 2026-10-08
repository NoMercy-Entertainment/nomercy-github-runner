"""A GitHub runner on Windows or macOS is given the job hooks (GitHub #7) by
the runtime that makes it, from the agent's own copy, so a redeployed agent
and a recreated runner are all it takes. A Forgejo runner has no hooks to
give: forgejo-runner has no such mechanism.

GitHub runs a hook only when its path ends in .ps1, .sh or .js; any other
path fails the job's last step, and the job with it (2026-09-21, 83 of 83 on
Linux). Each path is checked against that rule.
"""
import json
import os
import ntpath
import plistlib
import posixpath
from pathlib import Path

import pytest

from agent.runtimes import macos_appliance, windows_process
from agent.runtimes.macos_appliance import MacApplianceRuntime
from agent.runtimes.windows_process import WindowsProcessRuntime

from .fake_macos import TEMPLATE as MAC_TEMPLATE, TOOLS as MAC_TOOLS, FakeMac
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

    def test_a_github_runner_gets_both_hooks_in_its_own_tree(self, runtime, host):
        """Not the agent's live folder: a redeploy swaps that folder away,
        and a job starting then, or after a deploy that died midway, would
        not find its hook and fail."""
        env = self.env(runtime, host, "github")
        hooks = ntpath.join(runtime.paths(RID)["reg"], "hooks")
        assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"] == ntpath.join(hooks, "job-started.js")
        assert env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"] == ntpath.join(hooks, "job-completed.js")
        for key in HOOK_KEYS:
            assert env[key].endswith(".js") and host.exists(env[key]), key

    def test_the_hooks_are_the_agents_own_copy(self, runtime, host):
        self.env(runtime, host, "github")
        hooks = ntpath.join(runtime.paths(RID)["reg"], "hooks")
        for name in windows_process.HOOK_FILES:
            source = (AGENT / "hooks" / "windows" / name).read_text(encoding="utf-8")
            assert host.read_text(ntpath.join(hooks, name)) == source, name
        assert "runner_disk.py" in windows_process.HOOK_FILES
        assert "run_hook.js" in windows_process.HOOK_FILES
        assert "runner_guard.py" in windows_process.HOOK_FILES

    def test_a_create_driven_again_puts_the_current_hooks_back(self, runtime, host):
        self.env(runtime, host, "github")
        started = ntpath.join(runtime.paths(RID)["reg"], "hooks", "job-started.js")
        host.write_text(started, "process.exitCode = 1;")
        runtime.create(RID, spec("github", WINDOWS_TEMPLATE))
        assert "run_hook.js" in host.read_text(started)

    def test_the_trusted_authors_reach_the_hooks(self, runtime, host):
        """The controller sends RUNNER_TRUSTED_AUTHORS in the spec's env; the
        job host gives unit.json's env to the runner, and the runner to its
        hooks."""
        unit = spec("github", WINDOWS_TEMPLATE)
        unit["env"]["RUNNER_TRUSTED_AUTHORS"] = "alice,bob"
        runtime.create(RID, unit)
        reg = runtime.paths(RID)["reg"]
        env = json.loads(host.read_text(ntpath.join(reg, "unit.json")))["env"]
        assert env["RUNNER_TRUSTED_AUTHORS"] == "alice,bob"

    def test_the_hooks_run_with_the_python_the_job_host_runs(self, runtime, host):
        env = self.env(runtime, host, "github")
        assert env["RUNNER_HOOK_PYTHON"] == WINDOWS_TOOLS["python"]

    @pytest.mark.parametrize("provider", ["forgejo", None])
    def test_a_runner_that_is_not_githubs_gets_no_hooks(self, runtime, host, provider):
        env = self.env(runtime, host, provider)
        assert not any(key in env for key in (*HOOK_KEYS, "RUNNER_HOOK_PYTHON"))
        assert not host.exists(ntpath.join(runtime.paths(RID)["reg"], "hooks"))

    def test_a_spec_cannot_point_the_hooks_elsewhere(self, runtime, host):
        unit = spec("github", WINDOWS_TEMPLATE)
        unit["env"]["ACTIONS_RUNNER_HOOK_JOB_STARTED"] = r"C:\elsewhere\job.js"
        runtime.create(RID, unit)
        reg = runtime.paths(RID)["reg"]
        env = json.loads(host.read_text(ntpath.join(reg, "unit.json")))["env"]
        assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"].startswith(reg)

    def test_every_hook_is_a_script_the_runner_accepts(self):
        for key in HOOK_KEYS:
            name = windows_process.HOOK_SCRIPTS[key]
            assert name.endswith(".js") and (AGENT / "hooks" / "windows" / name).is_file(), name


class TestMacOS:
    """The guest has none of the agent's files, so the agent puts the hooks
    into the runner's reg directory at every create, beside the template it
    copies there, and the launchd job's environment points at them."""

    @pytest.fixture
    def guest(self):
        return FakeMac()

    @pytest.fixture
    def runtime(self, guest):
        return MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance,
                                   tools=dict(MAC_TOOLS, runner_user="runner"))

    def env(self, runtime, guest, provider):
        runtime.create(RID, spec(provider, MAC_TEMPLATE))
        plist = plistlib.loads(guest.read_text(runtime.paths(RID)["plist"]).encode())
        return plist["EnvironmentVariables"]

    def test_a_github_runner_gets_both_hooks_in_its_own_tree(self, runtime, guest):
        env = self.env(runtime, guest, "github")
        hooks = posixpath.join(runtime.paths(RID)["reg"], "hooks")
        assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"] == posixpath.join(hooks, "job-started.sh")
        assert env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"] == posixpath.join(hooks, "job-completed.sh")
        for key in HOOK_KEYS:
            assert env[key].endswith(".sh") and guest.exists(env[key]), key

    def test_the_hooks_are_the_agents_own_copy(self, runtime, guest):
        self.env(runtime, guest, "github")
        hooks = posixpath.join(runtime.paths(RID)["reg"], "hooks")
        for name in ("job-started.sh", "job-completed.sh", "lib.sh"):
            source = (AGENT / "hooks" / "macos" / name).read_text(encoding="utf-8")
            assert guest.read_text(posixpath.join(hooks, name)) == source.replace("\r\n", "\n")
        assert guest.modes[posixpath.join(hooks, "job-started.sh")] == 0o700
        assert guest.modes[posixpath.join(hooks, "job-completed.sh")] == 0o700

    def test_the_trusted_authors_reach_the_hooks(self, runtime, guest):
        unit = spec("github", MAC_TEMPLATE)
        unit["env"]["RUNNER_TRUSTED_AUTHORS"] = "alice,bob"
        runtime.create(RID, unit)
        plist = plistlib.loads(guest.read_text(runtime.paths(RID)["plist"]).encode())
        assert plist["EnvironmentVariables"]["RUNNER_TRUSTED_AUTHORS"] == "alice,bob"

    def test_the_hooks_know_the_accounts_own_home(self, runtime, guest):
        """Where Xcode keeps DerivedData: the job's HOME is the runner's own,
        under its work directory."""
        env = self.env(runtime, guest, "github")
        assert env["RUNNER_HOOK_USER_HOME"] == "/Users/runner"

    def test_an_agent_outside_the_guest_does_not_guess_the_home(self, guest):
        """On the appliance host the agent's own home is a Linux one, not the
        guest account's. Without a runner_user it is left unset, and the
        hooks leave the account's DerivedData alone."""
        remote = MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance,
                                     tools=dict(MAC_TOOLS, domain="gui/501"), remote=True)
        env = self.env(remote, guest, "github")
        assert "RUNNER_HOOK_USER_HOME" not in env
        assert all(key in env for key in HOOK_KEYS)

    def test_an_agent_outside_the_guest_names_the_runner_users_home(self, guest):
        remote = MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance,
                                     tools=dict(MAC_TOOLS, domain="gui/501", runner_user="ci"),
                                     remote=True)
        assert self.env(remote, guest, "github")["RUNNER_HOOK_USER_HOME"] == "/Users/ci"

    def test_an_agent_inside_the_guest_runs_as_the_account(self, guest):
        local = MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance,
                                    tools=dict(MAC_TOOLS, domain="gui/501"))
        home = self.env(local, guest, "github")["RUNNER_HOOK_USER_HOME"]
        assert home == os.path.expanduser("~")

    def test_a_create_driven_again_puts_the_current_hooks_back(self, runtime, guest):
        self.env(runtime, guest, "github")
        started = posixpath.join(runtime.paths(RID)["reg"], "hooks", "job-started.sh")
        guest.write_text(started, "#!/bin/bash\nexit 1\n")
        runtime.create(RID, spec("github", MAC_TEMPLATE))
        assert "lib.sh" in guest.read_text(started)

    @pytest.mark.parametrize("provider", ["forgejo", None])
    def test_a_runner_that_is_not_githubs_gets_no_hooks(self, runtime, guest, provider):
        env = self.env(runtime, guest, provider)
        assert not any(key in env for key in (*HOOK_KEYS, "RUNNER_HOOK_USER_HOME"))
        assert not guest.exists(posixpath.join(runtime.paths(RID)["reg"], "hooks"))

    def test_a_spec_cannot_point_the_hooks_elsewhere(self, runtime, guest):
        unit = spec("github", MAC_TEMPLATE)
        unit["env"]["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"] = "/tmp/elsewhere.sh"
        runtime.create(RID, unit)
        env = plistlib.loads(guest.read_text(runtime.paths(RID)["plist"]).encode())["EnvironmentVariables"]
        assert env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"].startswith(runtime.paths(RID)["reg"])

    def test_every_hook_is_a_script_the_runner_accepts(self):
        for name in macos_appliance.HOOK_SCRIPTS.values():
            assert name.endswith(".sh") and (AGENT / "hooks" / "macos" / name).is_file(), name


# ---- which origin guard each runner's hooks carry ---------------------------
#
# The dashboard shows a fleet as protected from outside pull requests only when
# every one of its runners runs a job-started hook with the check. Hooks are
# written at create, so an agent that has the check says nothing about a runner
# made before it: each runner's own hook file is what is read.

def _guard_version():
    import re
    text = (AGENT / "hooks" / "windows" / "runner_guard.py").read_text(encoding="utf-8")
    return int(re.search(r"^GUARD_VERSION = (\d+)$", text, re.M).group(1))


class TestWhatEachRunnerSaysOfItsGuard:
    def test_windows_reads_the_runners_own_copy(self):
        host = FakeWindows()
        runtime = WindowsProcessRuntime(run=host, fs=host, tools=WINDOWS_TOOLS)
        runtime.create(RID, spec("github", WINDOWS_TEMPLATE))
        (unit,) = runtime.instances()
        assert unit["origin_guard"] == _guard_version() >= 1
        guard = ntpath.join(runtime.paths(RID)["reg"], "hooks", "runner_guard.py")
        host.remove(guard)
        (unit,) = runtime.instances()
        assert unit["origin_guard"] == 0, "hooks from before the check"

    @pytest.mark.parametrize("provider", ["forgejo", None])
    def test_windows_without_hooks_says_nothing(self, provider):
        host = FakeWindows()
        runtime = WindowsProcessRuntime(run=host, fs=host, tools=WINDOWS_TOOLS)
        runtime.create(RID, spec(provider, WINDOWS_TEMPLATE))
        (unit,) = runtime.instances()
        assert "origin_guard" not in unit

    def _mac(self, guest):
        return MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance,
                                   tools=dict(MAC_TOOLS, runner_user="runner"))

    def test_macos_reads_the_runners_own_copy_once(self):
        guest = FakeMac()
        runtime = self._mac(guest)
        runtime.create(RID, spec("github", MAC_TEMPLATE))
        (unit,) = runtime.instances()
        assert unit["origin_guard"] == _guard_version()
        lib = posixpath.join(runtime.paths(RID)["reg"], "hooks", "lib.sh")
        guest.write_text(lib, "# hooks from before the check\n")
        # Known since this agent wrote them: no read over the guest's SSH
        # every beat. An agent started afterwards reads the file once.
        assert runtime.instances()[0]["origin_guard"] == _guard_version()
        assert self._mac(guest).instances()[0]["origin_guard"] == 0

    @pytest.mark.parametrize("provider", ["forgejo", None])
    def test_macos_without_hooks_says_nothing(self, provider):
        guest = FakeMac()
        runtime = self._mac(guest)
        runtime.create(RID, spec(provider, MAC_TEMPLATE))
        (unit,) = runtime.instances()
        assert "origin_guard" not in unit

    def test_a_beat_carries_it_for_each_unit(self):
        from agent import heartbeat

        class Runtime:
            def capabilities(self):
                return {}

            def instances(self):
                return [{"runner_id": RID, "state": "stopped", "origin_guard": 1},
                        {"runner_id": RID[:-1] + "2", "state": "stopped", "origin_guard": True},
                        {"runner_id": RID[:-1] + "3", "state": "stopped"}]

        class Agent:
            host_id, version, permitted, runtime = "w-1", "1", frozenset(), Runtime()

        units = heartbeat.build(Agent())["instances"]
        assert [u.get("origin_guard") for u in units] == [1, None, None]
