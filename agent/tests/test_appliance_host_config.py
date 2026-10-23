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
    "actions-runner-v2.338.0-macos-r20261008",
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


def test_console_network_survives_agent_config_loading():
    rendered = _rendered()
    rendered["appliance_pool"]["network"] = "macos_runner_default"
    parsed = config.parse(_agent_config(rendered), exists=lambda path: True)
    assert parsed.appliance_pool["network"] == "macos_runner_default"


def test_production_balloon_setting_is_accepted_by_the_agent_loader():
    rendered = _rendered()
    rendered["appliance_pool"].update(network="macos_runner_default", memory_balloon=True)
    parsed = config.parse(_agent_config(rendered), exists=lambda path: True)
    assert parsed.appliance_pool["memory_balloon"] is True


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
def test_memory_balloon_requires_an_explicit_boolean(value):
    rendered = _rendered()
    rendered["appliance_pool"]["memory_balloon"] = value
    with pytest.raises(config.ConfigError, match="memory_balloon"):
        config.parse(_agent_config(rendered), exists=lambda path: True)


@pytest.mark.parametrize("network", [None, 7, "", "--privileged", "host", "none", "bridge", "console\n"])
def test_pool_refuses_invalid_or_builtin_console_networks(network):
    rendered = _rendered()
    rendered["appliance_pool"]["network"] = network
    with pytest.raises(config.ConfigError, match="appliance_pool.network"):
        config.parse(_agent_config(rendered), exists=lambda path: True)


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


def test_production_fields_are_rendered_only_when_given():
    """The seed, console network, balloon and storage limit the production
    host runs with come through the renderer; left out, nothing is added."""
    plain = render(IMAGE, BASE_DISK, BASE_SYSTEM, DATA_ROOT, TEMPLATES)
    assert not ({"nvram_seed", "network", "memory_balloon", "runner_storage_limit"}
                & set(plain["appliance_pool"]))
    full = render(IMAGE, BASE_DISK, BASE_SYSTEM, DATA_ROOT, TEMPLATES,
                  nvram_seed="/var/lib/runner-appliances/base/seed.fd",
                  network="macos_runner_default", memory_balloon=True,
                  runner_storage_limit=100 * 1024 ** 3)
    assert full["appliance_pool"]["nvram_seed"] == "/var/lib/runner-appliances/base/seed.fd"
    assert full["appliance_pool"]["network"] == "macos_runner_default"
    assert full["appliance_pool"]["memory_balloon"] is True
    assert full["appliance_pool"]["runner_storage_limit"] == 100 * 1024 ** 3
    parsed = config.parse(_agent_config(full), exists=lambda path: True)
    assert parsed.appliance_pool["nvram_seed"].endswith("seed.fd")

