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
           memory_bytes=20 * 1024 ** 3, nvram_seed=None, network=None,
           memory_balloon=None, runner_storage_limit=None):
    """The `appliance_pool` and `capacity` blocks for one appliance host.

    Every value here is still checked whole by `agent.config.parse` when the
    agent starts; this only assembles what the operator supplied (or this
    deployment's fixed values) into the shape that loader accepts.

    The four optional fields are what the production host runs with
    (images/macos/pool/README.md, BASE-20260924.md): the NVRAM seed the
    debloated base needs, the console network, the balloon device and the
    per-runner storage limit. Left out, the rendered block is the README's
    documented fragment and nothing more.
    """
    pool = {
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
    }
    if nvram_seed is not None:
        pool["nvram_seed"] = nvram_seed
    if network is not None:
        pool["network"] = network
    if memory_balloon is not None:
        pool["memory_balloon"] = memory_balloon
    if runner_storage_limit is not None:
        pool["runner_storage_limit"] = runner_storage_limit
    return {
        "appliance_pool": pool,
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
    parser.add_argument("--nvram-seed", default=None,
                        help="absolute path to the base's OVMF variable seed")
    parser.add_argument("--network", default=None,
                        help="docker network the guest consoles join")
    parser.add_argument("--memory-balloon", action="store_true", default=False,
                        help="keep the balloon device and private QMP socket")
    parser.add_argument("--runner-storage-limit", type=int, default=None,
                        help="per-runner storage limit in bytes")
    parser.add_argument("--memory-bytes", type=int, default=20 * 1024 ** 3,
                        help="memory the host declares for its runners")
    args = parser.parse_args(argv)
    config = render(image=args.image, base_disk=args.base_disk,
                    base_system=args.base_system, data_root=DATA_ROOT,
                    templates=TEMPLATES, memory_bytes=args.memory_bytes,
                    nvram_seed=args.nvram_seed, network=args.network,
                    memory_balloon=True if args.memory_balloon else None,
                    runner_storage_limit=args.runner_storage_limit)
    print(json.dumps(config))


if __name__ == "__main__":
    main()
