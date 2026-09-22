"""The installer's pool block, fed straight to the agent's own loader.

`appliance_pool_config.render` (infra/hyperv/appliance_pool_config.py) is what
`Install-ApplianceHost.ps1 -AppliancePool` actually runs, as a subprocess, to
build the `appliance_pool` and `capacity` blocks it merges into the
`$agentConfig` it writes. This proves `agent.config.parse` accepts exactly
what `render()` produces for this deployment's real values (README's
appliance-pool fragment: images/macos/pool/README.md) - and refuses it when
the base is not attested or the image is still a placeholder - without
needing PowerShell, a subprocess or a live host.
"""
import pytest

from agent import config
from infra.hyperv.appliance_pool_config import render

IMAGE = "sha256:ab06f62364694bc5c49b41ea9644d42da03f7af2b36d86acec008a725ae6e745"
BASE_DISK = "/var/lib/runner-appliances/bases/clean-base-2026-09-22.qcow2"
BASE_SYSTEM = "/var/lib/runner-appliances/bases/BaseSystem.img"
DATA_ROOT = "/var/lib/runner-appliances/instances"
TEMPLATES = (
    "actions-runner-v2.336.0-macos-r20260921",
    "forgejo-runner-v13.1.0-macos-r20260921",
)


def _rendered():
    return render(IMAGE, BASE_DISK, BASE_SYSTEM, DATA_ROOT, TEMPLATES)


def _agent_config(pool):
    """A minimal macOS-appliance agent config carrying the given pool block,
    every other field the loader requires filled with a stand-in value."""
    return {
        "host_id": "macos-appliance-1",
        "runtime": "macos-appliance",
        "listen": "10.77.0.30:8443",
        "controller": "https://10.77.0.10:8444",
        "tls": {"cert": "/etc/runner-agent/agent.crt",
                "key": "/etc/runner-agent/agent.key",
                "ca": "/etc/runner-agent/ca.pem"},
        "guest": {"host": "127.0.0.1", "user": "runner",
                  "key": "/etc/runner-agent/guest.key"},
        "tools": {"domain": "gui/501"},
        "appliance_pool": pool["appliance_pool"],
        "capacity": pool["capacity"],
    }


def test_render_matches_the_readmes_documented_pool_fragment():
    """images/macos/pool/README.md's example, with this deployment's real
    image id, base paths, data root and template names."""
    assert _rendered() == {
        "appliance_pool": {
            "image": IMAGE,
            "base_disk": BASE_DISK,
            "base_system": BASE_SYSTEM,
            "data_root": DATA_ROOT,
            "templates": list(TEMPLATES),
            "base_guests_disabled": True,
            "image_uid": 1000,
            "image_gid": 1000,
            "ssh_port_base": 51000,
            "boot_timeout": 600,
            "shutdown_timeout": 180,
        },
        "capacity": {"max_runners": 2, "memory_bytes": 21474836480},
    }


def test_the_agent_loader_accepts_the_rendered_pool():
    parsed = config.parse(_agent_config(_rendered()), exists=lambda path: True)
    assert parsed.appliance_pool["image"] == IMAGE
    assert parsed.appliance_pool["base_guests_disabled"] is True
    assert parsed.capacity == {"max_runners": 2, "memory_bytes": 21474836480}


def test_the_loader_refuses_a_base_that_is_not_attested():
    rendered = _rendered()
    rendered["appliance_pool"]["base_guests_disabled"] = False
    with pytest.raises(config.ConfigError, match="base_guests_disabled"):
        config.parse(_agent_config(rendered), exists=lambda path: True)


def test_the_loader_refuses_a_placeholder_image():
    rendered = render("sha256:REPLACE_WITH_64_HEX_IMAGE_ID", BASE_DISK,
                      BASE_SYSTEM, DATA_ROOT, TEMPLATES)
    with pytest.raises(config.ConfigError, match="pinned by sha256"):
        config.parse(_agent_config(rendered), exists=lambda path: True)
