# Windows runner disk limits

The `windows-process` runtime can use one fixed VHDX per runner. This is an
opt-in worker configuration; absent or disabled configuration retains the
existing directory runtime:

```json
{
  "windows_storage": {
    "enabled": true,
    "root": "D:/runner-disks",
    "default_limit": 107374182400,
    "reserve_bytes": 21474836480
  }
}
```

`disk_limit` is the virtual disk size in bytes, at least 1 GiB, in whole MiB.
The default is 100 GiB. GPT and NTFS consume part of that space: usable
`disk_limit_bytes` is measured separately from `disk_virtual_bytes`.
`disk_used_bytes` and `disk_free_bytes` come from the filesystem. A missing or
unverified volume is unknown, never an empty disk. Configured workers publish
`disk_quota` and `disk_limit_enforced`; each disk still has to pass verification
before use.

The privileged agent creates `D:/runner-disks/<uuid>.vhdx` and an adjacent
protected JSON manifest, attaches the image without a drive letter, and mounts
its NTFS volume at `D:/runners/<uuid>`. The image and manifest are restricted
to SYSTEM and Administrators. The mountpoint parent is restricted too. The
runner's virtual service account receives Modify on its own mounted volume.
The helper refuses reparse ancestors and mismatched disk, partition, volume or
mount identities. It never adopts an existing plain runner directory.

The image is created with `diskpart` (a script file, never an inline command
line) and attached with the Storage module's disk-image cmdlets
(`Mount-DiskImage`, `Get-DiskImage`, `Dismount-DiskImage`). None of this comes
from Hyper-V: those cmdlets, and the module they ship in, exist only on a
Hyper-V host, not inside a Hyper-V guest, and a Windows runner worker can be
either. There is no fallback to the Hyper-V cmdlets - one path, working in
both places. Declared size is checked against the manifest on every use, and
so is fixed-versus-dynamic type - but type is never re-inferred from the live
file (a heuristic cannot tell a grown dynamic disk from a fixed one); it is
recorded in the manifest once, at creation, since diskpart was asked for a
fixed disk and that fact does not change later. Neither of these is an
identity check, and neither may stand in for one.

Disk identity is proven by attaching and reading the disk's own id, never
inferred from size or type. A disk already attached (the normal state while
its runner is in use) is read directly; a detached disk - the normal state
immediately after a host reboot, before anything has remounted it - is
attached read-only purely to read that id, then dismounted again, whether the
id matches or not: nothing here needs write access just to prove identity,
and nothing here is left attached after a refusal that it itself caused by
attaching. An image that cannot be attached at all is refused and named as
such, never treated as good enough to trust because it otherwise looked
right. `remove` proves identity this way before it will delete anything, even
when the disk was never attached during this run - it does not take a
same-sized file's word for it.

diskpart's own exit code and its own output text are never the decision for
whether image creation succeeded, only supporting evidence in the failure
message: both are documented to lie (diskpart can print a failure and still
exit 0, and its text is localized). The only authority is the post-condition
- the image now existing at the size that was asked for - checked directly
against the file itself. A creation that fails this check removes whatever
partial or corrupt file diskpart left behind, rather than leaving it for the
next attempt to trip over.

Creation reserves the entire fixed disk physically and first checks for its
size plus 64 MiB metadata allowance plus `reserve_bytes` free on the host NTFS
volume. Storage mutations share a named mutex. Other host applications can
still consume free space; the reserve is a preflight check, not a host-wide
filesystem reservation. Fixed allocation can take minutes and is never
silently changed to dynamic allocation. A failed partial creation is retained
and retried from its protected stage manifest; existing filesystems are not
reformatted. Resizing requires an explicit offline operation and is refused
by normal create/recreate.

All runner areas, including work, cache, registration, logs and temp, live on
that volume. HOME/USERPROFILE point to work, APPDATA/LOCALAPPDATA to cache, and
TEMP/TMP to temp. Tools that deliberately write elsewhere are outside this
disk limit: a native Windows process is not a whole-OS filesystem sandbox.

The expected volume GUID is in protected SCM service parameters. `jobhost`
checks it using the unprivileged Win32 mountpoint API before reading the
runner-writable unit file or spawning any child. Managed services stay at
`demand` startup, including after start. Following host reboot, the controller
starts only desired running units; the agent remounts and verifies their
existing disks first. Stopped runners stay stopped. No runner relies on an
automatic service start racing volume attachment.

CPU, memory and affinity limits are likewise passed through protected SCM
parameters and validated before the Job Object is created. Values in the
runner-writable unit JSON cannot override them; environment settings remain
in that unit file. Existing services must be reconfigured by the runtime to
receive this protection; changing Python code alone does not rewrite SCM.

Registration also stays inside the service account boundary. The privileged
agent invokes a fixed client module that exchanges authenticated JSON bytes
over a local named pipe. The jobhost checks its service identity, enters its
Job Object and then hosts that endpoint. The registration script and binaries
execute there, under the runner's account and limits; SYSTEM never executes
files from the writable registration directory. The protocol never uses
pickle. A per-runner 32-byte authentication key is stored below
`C:/ProgramData/nomercy/runner-keys`, writable only by SYSTEM/Administrators
and readable by that runner's own service account. Full removal deletes the
key; recreate preserves it. Existing services need the new `--runner-id`
parameters and a restart before the new registration client can reach them.
GitHub and Forgejo Windows templates cannot remove their own forge record;
deregistration returns that explicitly and the controller deletes by known ID.

Removal with `keep_data=true` preserves the VHDX, cache, logs and workspace,
and resets registration and temp. This also preserves existing data during
replacement compensation. Full removal first proves the service is stopped,
then removes only the identified volume access path and backing image. It
never recursively deletes a mounted runner root. Interrupted removal resumes
from its deletion stage and a completed removal can be repeated.

## Existing directory migration and deployment acceptance

Migration is explicit and offline: prove idle/stopped state, preserve the old
directory as a backup, create the new owned volume, copy and verify its data
and ACLs, update the protected service parameters, then validate the result.
Keep the original backup until the controller and runner registration are
verified. Normal runtime calls deliberately refuse an existing unmounted
directory; they never implement this migration by deleting data.

Before enabling production fleets, test the deployed helper under the real
agent account on a small disposable disk: create, write until full, verify
the mount through jobhost's API, remount, recreate retention, missing-mount
start refusal, stopped-state persistence and detach/removal. The local tests
exercise actual PowerShell control flow with mocked privileged storage
cmdlets and temporary ordinary files; they do not substitute for this host
acceptance test.
