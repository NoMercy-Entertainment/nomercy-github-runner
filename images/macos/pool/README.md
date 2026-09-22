# Per-runner macOS appliances

`agent.runtimes.macos_pool.MacAppliancePoolRuntime` implements design 10.5:
each runner UUID owns a complete QEMU guest, a qcow2 overlay and a localhost SSH
port. The Linux host agent serves the existing uniform protocol. Guest launchd,
registration, cache and log operations use the existing Mac runtime over SSH.

This is explicitly opt-in. A worker without `appliance_pool` keeps its existing
single-appliance or native-guest behavior. Enabling a pool does not migrate an
existing shared guest automatically. Keep platform maintenance enabled throughout
migration, keep both forge listeners stopped, and retain the old guest as backup.

## Worker preparation

Prepare a standalone qcow2 base while its source guest is cleanly powered off.
Never use a live writable OS disk as a backing file. Install and verify both
provider templates, each containing executable `run`, `register`, `deregister`:

* `actions-runner-v2.336.0-macos-r20260921`
* `forgejo-runner-v13.1.0-macos-r20260921`

The base must contain **no enabled runner listeners** or registrations belonging
to the old shared instance. Verify launchd's persistent disabled settings and
auto-start locations before setting `base_guests_disabled: true`. This field is
an operator attestation, not a claim that the agent can inspect a powered-off
guest. Give the configured SSH user the existing launchd/install privileges and
passwordless permission for the fixed `/sbin/shutdown -h now` command. Keep SSH
credentials in root-readable files and the pool data root private (0700).

Build the approved Docker-OSX wrapper with label
`nomercy.appliance_boot_cleanup=true` and entrypoint
`/usr/local/bin/nomercy-appliance-start`. Preserve the original nonempty QEMU
`Cmd`, headless settings and boot configuration. Pin its **image ID or digest**,
not a mutable tag. The runtime verifies this contract before creating units.
Only `/dev/kvm` is passed through; privileged mode is not used.
The approved image runs as `arch` (UID/GID1000). Explicit `image_uid`/`image_gid`
are trusted worker settings: only each overlay is chowned to that identity with
mode0600. Host metadata remains root-owned and private. Both read-only base
files must be readable by the image user (the prepared base uses mode0644).

The immutable base is mounted read-only at its same absolute path so the qcow2
backing reference resolves inside the guest host unit. The writable overlay is
mounted at `/home/arch/OSX-KVM/mac_hdd_ng.img`; BaseSystem is read-only at
`/home/arch/OSX-KVM/BaseSystem.img`. Neither request parameters nor provider
template names can override the QEMU image, mounts, command or SSH endpoint.

Example fragment, alongside the existing `guest`, `tools`, mTLS and host identity:

```json
{
  "runtime": "macos-appliance",
  "guest": {
    "host": "127.0.0.1",
    "user": "runner",
    "key": "/etc/runner-agent/guest.key"
  },
  "tools": {"domain": "gui/501"},
  "capacity": {"max_runners": 2, "memory_bytes": 25769803776},
  "appliance_pool": {
    "image": "sha256:REPLACE_WITH_64_HEX_IMAGE_ID",
    "base_disk": "/var/lib/runner-appliances/base/macos.qcow2",
    "base_system": "/var/lib/runner-appliances/base/BaseSystem.img",
    "data_root": "/var/lib/runner-appliances/instances",
    "templates": [
      "actions-runner-v2.336.0-macos-r20260921",
      "forgejo-runner-v13.1.0-macos-r20260921"
    ],
    "base_guests_disabled": true,
    "image_uid": 1000,
    "image_gid": 1000,
    "ssh_port_base": 51000,
    "boot_timeout": 600,
    "shutdown_timeout": 180
  }
}
```

The image placeholder intentionally fails validation until replaced. Pool
defaults are 4 vCPUs and 8 GiB guest RAM, with 2 GiB additional bounded QEMU
overhead per instance. Docker CPU quota equals vCPU count; `RAM`, `SMP`, `CORES`
are set explicitly. Host memory and total RAM+swap are both capped at guest RAM
plus overhead, so the host allocation cannot grow through swap. Placement must
reserve the advertised `per_runner_memory_overhead_bytes` in addition to guest
RAM. Pool config defaults to a 24 GiB host budget and two runners.

The fixed virtual guest disk size is reported as `guest_disk_virtual_bytes`.
It is not a host-filesystem quota: qcow2 metadata, logs and the shared base also
occupy host space. Cache scopes remain workspace/toolcache/temp, and there are
no nested job containers or automatic native cache-budget enforcement.

## Lifecycle and crash recovery

Each `instances/<UUID>/instance.json` records its UUID, assigned port, pinned
image, absolute base path, guest template, CPU/RAM, initialization state,
persistent listener-disabled proof and retained-disk state. It contains no
registration token. The file is atomically replaced with mode0600; its parent is
0700. The Linux agent holds an exclusive pool-owner lock to prevent a second
agent from allocating the same port. Every mutating operation is serialized per
UUID. SSH control sockets also have one path per UUID.

Creation reserves metadata before creating storage, then creates a temporary
overlay and atomically publishes it. Existing disks are never replaced to recover
from a failed request. A retry verifies the same base reference and same owned
Docker unit, mounts, resource caps and SSH port. A conflicting unit or port fails
closed. Images are inspected locally; creation does not introduce a pull command.
QEMU metadata reads use `qemu-img info -U` so an already-running unit can be
verified on retry; this is read-only and never a commit/rebase operation. See
[QEMU's image utility documentation](https://www.qemu.org/docs/master/tools/qemu-img.html).

`create` follows the existing runtime contract: install the guest template and
start its launchd job. Registration uses the per-UUID guest registrar. `stop`
persistently disables and stops that job, verifies quiescence, then requests a
graceful guest shutdown and waits for observed poweroff. A failed SSH connection
or ambiguous host response is never absence or permission to force power off.
A timeout leaves a visible failure and preserves the disk; this runtime does
not call `docker kill` or force-stop a guest.

A stopped cache clear or stopped registrar operation temporarily boots the guest
only when metadata proves the listener was persistently disabled. It never
enables, bootstraps or kickstarts a runner. The guest is shut down again after
the operation. A running/drained guest must also be proven quiescent before
cache clearing. Reads never boot a guest: offline telemetry stays unknown, logs
are empty, and deep probes explain that the guest is powered off.

`remove(keep_data=True)` boots the disabled guest, removes only its registration,
workspace and temporary files through the inner runtime, preserves cache/logs,
shuts down and removes the QEMU execution unit. The overlay and assigned port
remain for `create` of the same UUID. Changing CPU/RAM/template requires this
recreate path. It deliberately preserves the guest OS disk; an OS-base upgrade
is a separate migration, not an implicit cache-destroying reset.

`remove(keep_data=False)` requires observed poweroff, removes only the unit whose
labels identify this UUID and pool, verifies absence, then deletes only that
UUID's validated directory. It does not delete the base, another UUID or a path
supplied by a request. An unreachable Docker daemon prevents deletion.

For migration, first prepare clean pool instances while maintenance prevents
controller scheduling. Copy each previous runner's registration, cache and
history only to its matching UUID in its new guest, retaining its external forge
identity as required. Disable the matching guest launchd job before shutdown and
update that instance's metadata only after verifying disk ownership, port,
template and stopped state. Do not copy a shared-guest adoption marker: pool
instances use their own `com.nomercy.rnr-<UUID>` launchd label. Verify all IDs,
registrations and stopped heartbeats before allowing the controller to start any
listener. Keep the old guest powered off and never run both registrations.

## Validation

`python -m pytest agent/tests/test_macos_pool.py agent/tests/test_macos_runtime.py`
uses fake Docker/QEMU/SSH and temporary host storage. It verifies two isolated
instances, same-UUID retry/recreate, base preservation, port/ownership conflicts,
stopped cache safety, fail-closed unknown states, offline measurements, registrar
isolation and process-tree telemetry. These tests do not establish that a
particular QEMU image boots; deployment requires an actual isolated guest boot,
stop, cache-clear and recreate acceptance pass while maintenance remains on.
