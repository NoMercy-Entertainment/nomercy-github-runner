"""The single source of the `appliance_pool` / `capacity` fields a macOS
appliance host's agent config needs for the per-runner pool runtime
(agent.runtimes.macos_pool, design 10.5).

`Install-ApplianceHost.ps1` shells out to this module's `__main__` and merges
its JSON straight into the `$agentConfig` it writes; nothing here duplicates
what `agent/config.py` already validates, and nothing in the installer
duplicates these field names. See images/macos/pool/README.md for what each
field means and why.
"""
import json

#: Fixed for this deployment: the pool's instance directory and the two
#: provider templates the base guest carries (images/macos/pool/README.md).
#: Only the image id and the two base image paths change per host, so only
#: those are exposed on the command line.
DATA_ROOT = "/var/lib/runner-appliances/instances"
TEMPLATES = (
    "actions-runner-v2.338.0-macos-r20261008",
    "forgejo-runner-v13.1.0-macos-r20260921",
)


def render(image, base_disk, base_system, data_root, templates,
           uid=1000, gid=1000, port_base=51000, max_runners=2,
           memory_bytes=20 * 1024 ** 3):
    """The `appliance_pool` and `capacity` blocks for one appliance host.

    Every value here is still checked whole by `agent.config.parse` when the
    agent starts; this only assembles what the operator supplied (or this
    deployment's fixed values) into the shape that loader accepts.
    """
    return {
        "appliance_pool": {
            "image": image,
            "base_disk": base_disk,
            "base_system": base_system,
            "data_root": data_root,
            "templates": list(templates),
            "base_guests_disabled": True,
            "image_uid": uid,
            "image_gid": gid,
            "ssh_port_base": port_base,
            "boot_timeout": 600,
            "shutdown_timeout": 180,
        },
        "capacity": {
            "max_runners": max_runners,
            "memory_bytes": memory_bytes,
        },
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True,
                        help="pinned appliance image, sha256:<64 hex>")
    parser.add_argument("--base-disk", required=True,
                        help="absolute path to the read-only base qcow2")
    parser.add_argument("--base-system", required=True,
                        help="absolute path to the read-only BaseSystem.img")
    args = parser.parse_args(argv)
    config = render(image=args.image, base_disk=args.base_disk,
                    base_system=args.base_system, data_root=DATA_ROOT,
                    templates=TEMPLATES)
    print(json.dumps(config))


if __name__ == "__main__":
    main()
