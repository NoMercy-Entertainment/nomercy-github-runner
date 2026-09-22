# Linux runner disk budgets

This is opt-in. Without `storage` in the worker configuration, existing Docker
volumes and their removal behavior are unchanged. Enabling it does **not**
migrate legacy volumes. A legacy volume, unknown filesystem, changed image
size, foreign mount, or ambiguous inspection causes an explicit refusal.

```json
"storage": {
  "root": "/var/lib/runner-storage",
  "default_bytes": 107374182400
}
```

The agent runs as root. The root directory and its parents must not be
symlinks; the root and control files must belong to the agent and cannot be
writable by other users. The root must be dedicated to this feature. The
worker needs Python, e2fsprogs (`mkfs.ext4`, `blkid`), util-linux (`mount`,
`umount`, `findmnt`, `losetup`), and loop devices.

Each validated runner UUID owns `UUID/metadata.json`, `UUID/disk.ext4`, and
`UUID/mnt`. The image is sparse, with a fixed logical size. `disk_limit` in an
execution-unit create spec overrides the default; both are integer bytes,
at least 64 MiB. An existing image cannot silently change size. Filesystem
metadata consumes part of this ceiling, so usable capacity is smaller.

The existing five Docker volume names use the local driver with `type=none`,
`o=bind`, and fixed subdirectories `work`, `cache`, `reg`, `dind`, `logs` on
that filesystem. Volume labels, options, filesystem UUID, loop backing file,
offset, and mount target are checked before reuse. One filesystem bounds the
combined five volumes. Sparse allocation does not reserve host space: the
worker's underlying filesystem still needs free-space monitoring and a
capacity budget. Another runner cannot borrow this runner's unused ceiling.

Managed containers require an image labelled `nomercy.readonly_root=true`.
The runtime resolves that image to its immutable image ID before creating
storage, uses `--read-only`, and gives `/run` a bounded 64 MiB tmpfs. The
image must put its writable runner installation and Docker configuration
on the owned volumes. Legacy images are refused before volume allocation.
Stopped maintenance helpers use the same restrictions.

**Boundary:** the owned filesystem bounds persistent runner data. The
read-only root cannot grow a writable layer. `/run` and Docker's `/dev/shm`
are separate bounded memory filesystems; outer Docker logs remain outside
the disk ceiling and rotate at three 10 MB files (two for maintenance).
Privileged execution is required by the nested engine; this is resource
containment for trusted jobs, not an isolation boundary against a malicious
privileged job. Telemetry reports the whole owned filesystem as
`disk_used_bytes`, `disk_free_bytes`,
`disk_usable_bytes`, and its image ceiling as `disk_limit_bytes`.

`keep_data=true` in this managed mode preserves work, cache, nested engine
data, and logs; registration is cleared only after its Docker volume is
gone. `keep_data=false` removes the image only after the container and all
five Docker volumes are proven absent, the filesystem is unmounted, and no
loop device remains attached. It deletes only known owned control files;
unknown objects or nested mounts stop cleanup. The legacy, disabled mode
retains its existing clean-workspace recreation behavior.

## Mount before Docker starts

Install the reviewed source at a root-owned fixed path, for example
`/opt/runner-platform`, and create the private storage root with mode 0700.
Keep the agent stopped and runner containers stopped during installation and
any separate data migration. Install a Docker service drop-in at
`/etc/systemd/system/docker.service.d/runner-storage.conf`:

```ini
[Service]
Environment=PYTHONPATH=/opt/runner-platform
ExecStartPre=/usr/bin/python3 -m agent.runtimes.linux_storage --root /var/lib/runner-storage
```

Adapt only those administrator-controlled installation paths; they are not
accepted in runtime requests. Do not prefix the command with `-`: failure
must prevent Docker starting and automatically restarting runners on empty
host mountpoint directories. Include any separate backing-disk mount in
the Docker unit's `RequiresMountsFor=/var/lib/runner-storage` dependency.
Apply the drop-in with `systemctl daemon-reload`, then start Docker only
after the separately reviewed migration and preflight are complete.

The helper also runs directly as the same fixed `python3 -m ... --root ...`
command. It validates every metadata/image pair and mounts existing
filesystems. It never formats an image at boot. A failed or interrupted
initial format leaves an image requiring offline inspection; retries never
reformat it. A successful format followed by an interrupted mount or Docker
volume creation resumes safely. Keep independent backups during migration.

## Validation

The fake-executor tests exercise path ownership, format/mount interruptions,
foreign loops, legacy-volume refusal, volume reuse, data retention, and
removal ordering. On a disposable Linux worker, with root and loop devices:

```sh
RUNNER_LOOP_TEST=1 python3 -m pytest agent/tests/test_linux_storage.py -k real_small_loop_filesystem_limit -q
```

This opt-in test mounts a fresh 64 MiB filesystem, verifies that a larger
write reaches ENOSPC, and cleans up its own UUID. It does not test reboot or
live runner builds. Deployment acceptance must additionally test Docker
startup ordering, a worker reboot, both forge images, and retained migrated
data. Windows skips the real mount and symlink-specific tests.
