# Linux Worker startup receive timeout

The official v2.338.0 Listener accepts `GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT`
in 30..300 seconds. Its Worker has a separate hard-coded 30-second receive
timeout. Under storage stalls the Worker can exit before the Listener sends
the job, leaving GitHub with a renewed job and no process executing it.

This derivative changes the Worker's private startup timeout to the same
environment setting and bounds. The job's execution timeout is unchanged.
It logs `Worker startup IPC timeout: 300 seconds.` before receiving a job.

The source archive, upstream commit and original Worker DLL are checksum
pinned in `build.py`. All other Worker source files and the action schema
come from that archive. Dependencies are referenced from the upstream
distribution already in the base image; no NuGet download is required. The
SDK must be 8.0.427. A new upstream runner needs a new reviewed build and
checksums; this script refuses to silently patch an unknown distribution.

Build on the Linux worker, from the repository root:

```sh
sudo docker build -f images/github-worker-startup/Dockerfile \
  -t nomercy/runner-unit-github:20261116-startup1 images/github-worker-startup
```

The final image contains only the replacement assembly and its manifest on
top of the existing unit. The image label `nomercy.worker_startup_patch=1`
and assembly informational version `2.338.0+nomercy.worker-startup.1`
identify the derivative. `/runner/worker-startup.json` records source and
artifact checksums. Keep the original base tag for rollback.

For existing registrations, extract the DLL and manifest into a private
bundle on the Linux worker. The installer's default mode prints a plan:

```sh
sudo python3 images/github-worker-startup/install.py /path/to/bundle
sudo python3 images/github-worker-startup/install.py /path/to/bundle --apply
```

The installer validates every managed GitHub Linux registration and its
current DLL before making changes. It accepts only the pinned original or
the bundle's already installed DLL. It records its backup directory under
`/var/lib/runner-agent/maintenance/worker-startup-*`. It writes a separate
file, fsyncs it, and renames it over the old DLL, preserving ownership and
permissions. Existing processes keep their old mapped inode. No service
restart, drain, deregistration or recreation is needed.

Set the fleet's `unit_template` to the new image as well, so new runners and
future recreations receive the fix. Updating that default does not recreate
existing runners. Their currently recorded image still names the old unit;
the installer reports the actual Worker checksum separately.

Rollback consists of restoring the prior fleet template and atomically
restoring `Runner.Worker.original.dll` to the recorded targets in
`deployment.json`, using `install.atomic_replace`. Check each current target
still has the deployed checksum first. Already running Workers finish with
the assembly they loaded. Keep the backup and image until verification ends.

Validation includes an actual Worker transport probe, not just a setting
string check. In a separate unregistered container with networking disabled,
`tests/ipc_probe.py` sends a deliberately invalid 69,014-character message
after a configurable delay. Receipt is checked in the real Worker's log;
invalid JSON then terminates safely without running a job. The official
Worker fails the 35-second delay despite inheriting 300; the derivative
receives messages delayed by 35 and 45 seconds. `tests/SettingsProbe.cs`
checks nine actual constructor settings, including invalid input and clamps.
`agent/tests/test_worker_startup_install.py` checks mapped-inode preservation
and rejection of unverified bundles and runs with stdlib unittest on Linux.
