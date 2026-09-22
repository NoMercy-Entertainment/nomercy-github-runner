import plistlib

import pytest

from agent import naming
from agent.runtimes.macos_appliance import MacApplianceRuntime
from agent.runtimes.windows_process import WindowsProcessRuntime
from .fake_macos import FakeMac, TOOLS as MAC_TOOLS, TEMPLATE as MAC_TEMPLATE
from .fake_windows import FakeWindows, TOOLS as WIN_TOOLS, TEMPLATE as WIN_TEMPLATE

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_windows_stop_and_drain_disable_boot_start_and_start_restores_it():
    host = FakeWindows()
    runtime = WindowsProcessRuntime(run=host, fs=host, tools=WIN_TOOLS)
    runtime.create(RID, {"image": WIN_TEMPLATE})
    name = naming.unit_name(RID)
    for action in (runtime.stop, runtime.drain):
        action(RID)
        assert host.services[name]["start"] == "demand"
        runtime.start(RID)
        assert host.services[name]["start"] == "auto"


def test_macos_stop_disables_before_bootout_and_start_reenables():
    host = FakeMac()
    runtime = MacApplianceRuntime(run=host, fs=host, tools=MAC_TOOLS)
    runtime.create(RID, {"image": MAC_TEMPLATE})
    host.calls.clear()
    runtime.stop(RID)
    verbs = [call[1] for call in host.calls if call[0] == MAC_TOOLS["launchctl"]]
    assert verbs == ["disable", "bootout"]
    assert runtime.label(RID) in host.disabled
    runtime.start(RID)
    assert runtime.label(RID) not in host.disabled
    assert runtime.status(RID)["running"] is True


def test_macos_unsafe_keepalive_refuses_drain_without_signalling():
    host = FakeMac()
    runtime = MacApplianceRuntime(run=host, fs=host, tools=MAC_TOOLS)
    runtime.create(RID, {"image": MAC_TEMPLATE})
    path = runtime.paths(RID)["plist"]
    definition = plistlib.loads(host.read_text(path).encode())
    definition["KeepAlive"] = True
    host.write_text(path, plistlib.dumps(definition).decode())
    host.calls.clear()
    with pytest.raises(RuntimeError, match="KeepAlive"):
        runtime.drain(RID)
    assert not any(call[1] == "kill" for call in host.calls)
    assert runtime.status(RID)["running"] is True


def test_macos_system_plist_has_explicit_user_and_is_installed_by_root():
    host = FakeMac()
    installs = []
    def run(args, **kwargs):
        if args[:3] == ["/usr/bin/sudo", "-n", "/usr/bin/install"]:
            installs.append(args)
            host.write_text(args[-1], host.read_text(args[-2]))
            return True, "", ""
        return host(args, **kwargs)
    runtime = MacApplianceRuntime(run=run, fs=host,
        tools=dict(MAC_TOOLS, domain="system", runner_user="runner"))
    runtime.create(RID, {"image": MAC_TEMPLATE})
    definition = plistlib.loads(host.read_text(runtime.paths(RID)["plist"]).encode())
    assert definition["UserName"] == "runner"
    assert installs[0][3:9] == ["-o", "root", "-g", "wheel", "-m", "0644"]
