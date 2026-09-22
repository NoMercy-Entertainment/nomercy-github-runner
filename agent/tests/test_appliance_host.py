import json

import pytest

from agent.runtimes.appliance_host import (
    BOOT_CLEANUP_LABEL, BOOT_ENTRYPOINT, DockerApplianceHost)


class Host:
    def __init__(self):
        self.calls = []
        self.data = {"Name": "/macos-sequoia", "State": {"Status": "exited", "Running": False},
                     "Config": {"Labels": {BOOT_CLEANUP_LABEL: "true"},
                                "Entrypoint": [BOOT_ENTRYPOINT], "Cmd": ["/bin/bash", "-c", "original boot"]}}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "inspect":
            return True, json.dumps(self.data), ""
        if argv[1] == "start":
            assert argv == ["docker", "start", "macos-sequoia"]
            self.data["State"] = {"Status": "running", "Running": True}
            return True, "macos-sequoia", ""
        raise AssertionError(argv)


def test_boot_only_the_named_appliance_and_wait_for_darwin():
    host = Host()
    guest_calls = []
    def guest(args, **kwargs):
        guest_calls.append(args)
        return True, "Darwin\n", ""
    appliance = DockerApplianceHost("macos-sequoia", guest, run=host)
    assert appliance.state() == "stopped"
    appliance.boot()
    assert appliance.state() == "running"
    assert guest_calls == [["/usr/bin/uname", "-s"]]
    assert not any(arg == "rm" for call in host.calls for arg in call)


@pytest.mark.parametrize("missing", ["Labels", "Entrypoint", "Cmd"])
def test_unverified_boot_image_is_never_started(missing):
    host = Host()
    host.data["Config"].pop(missing)
    appliance = DockerApplianceHost("macos-sequoia", lambda *a, **k: None, run=host)
    with pytest.raises(RuntimeError):
        appliance.boot()
    assert not any(call[1] == "start" for call in host.calls)


def test_unknown_power_is_never_started():
    host = Host()
    host.data["State"] = {"Status": "restarting", "Running": True, "Restarting": True}
    appliance = DockerApplianceHost("macos-sequoia", lambda *a, **k: None, run=host)
    with pytest.raises(RuntimeError, match="unknown"):
        appliance.boot()
    assert not any(call[1] == "start" for call in host.calls)


def test_guest_readiness_deadline_is_bounded():
    now = [0]
    def sleep(seconds):
        now[0] += seconds
    appliance = DockerApplianceHost("macos-sequoia", lambda *a, **k: (False, "", "down"),
                                    run=Host(), boot_timeout=10,
                                    clock=lambda: now[0], sleep=sleep)
    with pytest.raises(RuntimeError, match="within 10s"):
        appliance.boot()
    assert now[0] == 10
