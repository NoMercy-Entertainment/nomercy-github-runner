# Linux runner startup failures and storage stalls, 2026-11-16

Times below are CET (UTC+1) unless explicitly marked UTC. Physical host
BEAST-UNIT was never restarted. No VM, runner container, listener, running
job or Docker daemon was restarted by this repair.

## Two separate symptoms

FFmpeg [run 43776377514](https://github.com/NoMercy-Entertainment/nomercy-ffmpeg/actions/runs/43776377514),
attempt 1, had two jobs with no visible steps. Their Workers had already
exited hours before the offline notifications:

| Job | Runner | Worker start | Failure |
| --- | --- | --- | --- |
| linux-x86_64 / docker-build, 133007652534 | github-linux-x64-4 | 01:03:24 | Receive cancelled at 01:04:04; Worker exited 1 before receiving the job |
| windows-x86_64 / docker-build, 133007652581 | github-linux-x64-9 | 01:03:44 log created; initialization finished around 01:04:08 | Receive cancelled at 01:04:41; Worker exited 1 before receiving the job |

These are both Linux runners running cross-builds, not the Windows VM.
The Listeners continued renewing the job leases with no Worker running the
jobs. The Linux job's send was delayed from 01:03:24 to 01:04:09, after its
Worker had already stopped receiving. Listener IPC timeout was already 300
seconds in both its startup log and inherited environment.

The notifications concerned runners 10, 1, 2 and 7 at 04:26..04:28. These
were different runners. Their earlier FFmpeg jobs had already completed.
The logs show slow acquisition/acknowledgement, HTTP timeouts, pending
GitHub authentication/configuration migration, and new sessions around
04:30..04:33. They recovered without container restarts. Runner 9 had a
further transient outage at 04:49..04:55. Runner 5 went offline at 05:21 and
returned by the 05:27:50 alarm poll, also without a restart. Its existing
Worker continued and reported its test job's result at 05:29:51.

Storage pressure is strongly supported as a contributor to these slow
operations; the observations do not prove every GitHub-side delay originated
locally. Do not confuse the initial Worker failures at 01:04 with the later
notifications at 04:26.

The operator reran the failed FFmpeg jobs at approximately 04:46 while this
investigation was underway and confirmed that action through the question
tool. The repair did not cancel or rerun the workflow. FreeBSD had completed
successfully at 04:35:36. At 05:45, both replacement x64 builds were executing
their actual Docker build steps. Their final results were still pending.

The successful baseline was checked against GitHub: v1.0.45 run
`41646318919`, v1.0.46 run `42716501234`, and v1.0.47 runs `43526378417` and
`43604999863`. The first successful v1.0.47 run included platform builds of
roughly 2 hours 19 minutes to 3 hours 11 minutes; the next used warm caches
and finished those builds in minutes. Both heavy and cached builds have
therefore succeeded before. The diff from the v1.0.47 tag commit to
`5a093b954b56d3a55b77586180ff1a93bf8c2dad` contains no workflow changes.
The failed initial handoff did not reach checkout, so the FFmpeg source
changes cannot directly explain that failure. The hard-coded startup wait
was a pre-existing, timing-sensitive defect, not a new v1.0.48 workflow rule.

## Confirmed Worker receive-timeout defect

Upstream [JobDispatcher](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/JobDispatcher.cs)
reads `GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT`, defaulting to 30 and clamping
to 30..300. Upstream [Worker](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Worker/Worker.cs)
instead hard-codes a separate 30-second timeout before receiving the initial
job. The previous platform setting fixed only the sender.

An isolated real-Worker probe reproduced this: an inherited value of 300
did not prevent the original Worker from timing out when message delivery
was delayed 35 seconds. The patched Worker received the same message. The
final artifact also received a message delayed 45 seconds. Tests used
unregistered containers with networking disabled and deliberately invalid
JSON, so the probe exercised transport without executing a CI job. An exit
code of 1 from the probe Worker is expected for that invalid JSON; the test
distinguishes successful receipt from the original receive cancellation.

The operator explicitly approved a local Worker derivative. The change
applies the same environment value and bounds to the Worker's receive wait,
and logs the effective timeout. Only `Runner.Worker.dll` was replaced.

| Provenance | Value |
| --- | --- |
| Upstream version | 2.338.0 |
| Upstream source commit | 197ca027b90de199464dee3bb9ff198a1db60967 |
| Source archive SHA-256 | f9ad169b07995dad7964a4f9ce95673a153317a4888ba8d540ecead71def725a |
| Original Worker SHA-256 | 5b70067b6cbf706bcccdc73ef4a3ce0b118a86ad5cdf82b54dcca96724c67a0f |
| Patched Worker SHA-256 | 8a68a267f74a8ed9479dd1840dab701bedb1f58b9a391b85f9f3ca8beb46ce4f |
| SDK | 8.0.427 |
| New image | nomercy/runner-unit-github:20261116-startup1 |
| Image ID | sha256:b8db2ff3db89308520bdb1aa14e9549c71b03a5cfe1c1350dfbb15f976463c8b |

The standalone build and Dockerfile build produced the identical DLL hash,
with zero warnings and zero errors. The existing distribution's dependencies
are retained. Nine constructor tests cover default, invalid, overflow,
minimum, ordinary and maximum settings. Linux tests verify atomic replacement
preserves an existing mapped assembly and rejects an unverified bundle before
accessing any registration.

All ten live Linux registrations received the file atomically around 05:21.
Backups and the deployment manifest are on the worker:
`/var/lib/runner-agent/maintenance/worker-startup-s4tfhw1t/`.
All ten installed hashes were read back. Their container start times and
restart counts remained unchanged. The new Worker is used by the next job;
Workers already executing jobs retain their old mapped inode.

The fleet's `unit_template` was changed from `20261029` to the new image
without recreating anything or changing desired capacity 10. The controller
database was backed up with SQLite's backup API to
`/data/maintenance/worker-startup-20261116/control.before-template.db` and the
change was audited as `worker-startup-patch` by
`codex (operator-approved repair)`. Future creates and recreates therefore
retain the correction. Windows and macOS were not patched.

## Storage measurements

The physical storage is a three-drive Toshiba HDWD130 RAID 0 behind an
Adaptec 6805T, exposed as D:. Both the Linux root VHDX and data VHDX reside
there. No checkpoint chain or configured Hyper-V IOPS limit was present.

The actual path is:

```text
runner work / nested Docker / cache / registration / diagnostics
  -> that runner's own 100 GiB ext4 image, through its own loop device
  -> /var/lib/runner-data on Linux /dev/sdb
  -> D:\HyperV\runner-platform\data\rnr-linux-1-data.vhdx
  -> the three physical HDDs in the shared RAID 0 array
```

Every runner already has a separate `disk.ext4`. All 13 loop devices had
`DIO 1`. The earlier buffered-loop double-cache/OOM fix was present; all
observed unit cgroups reported `oom_kill 0`, and no matching kernel OOM kill
was found for this incident. `docker stats` alone is insufficient evidence
because it subtracts inactive cache.

Sysstat retained historical measurements. Between 01:00 and 01:10 the data
disk was 99.64% busy, averaged 257.82 ms per request and a queue depth of
52.37; VM CPU iowait averaged 40.29%. Full I/O pressure was 73.36% over that
interval. The 04:20..04:30 interval around the notifications also showed
high I/O pressure and an average data disk queue near 30.

A later live sample around 05:38 measured approximately 102 MiB/s written
to sdb, of which approximately 95 MiB/s was through runner 3's loop1. Its
average write wait was 361 ms, with sdb at 204 ms and queue depth 48.1.
`pidstat` showed dotnet, build/tool processes and dpkg as active writers.
Kernel loop workers account for the same I/O at another layer; their byte
counts must not be added to application counts as independent traffic.
No independent runaway loop-driver workload was demonstrated.

Separate VHDXs could improve isolation, scheduling and remove a layer in a
redesigned storage layout. They would still share the same HDDs' physical
IOPS. The size of one VHDX alone is not evidence that it serializes all
writers. No storage migration or live filesystem restructuring was done.

## Disk/controller health

Read-only ARCCONF checks reported controller, array and ZMM `Optimal`, no
failed/degraded logical devices, no failed stripes, no current background
operation, write-back cache enabled with protection, and SATA links at
6.0 Gb/s. Controller temperature was 46..47 C, reported normal.

The historical device log contains 21 link failures and 1,389 aborted
commands for serial 87BLZSMAS. Six samples from 05:13:25 to 05:15:38 showed
no increases. Current controller statistics reported zero link, medium,
hardware and aborted-command errors for each of current device IDs 4, 5 and
6. The dead-drive history contains entries for current-drive serials from
December 2025. Neither those old entries nor `Optimal` establish a complete
physical media diagnosis. There was no evidence here of a currently
disabled controller cache, active rebuild, or newly accumulating disk errors.

Windows disk events on November 12 refer to disk 2; D: is disk 0. Those
events were not attributed to this array without a verified historical
device mapping. Windows storage reliability counters do not expose full
per-drive data behind this controller. Generic SMART passthrough was not
forced: smartmontools documents broken AACRAID support on Windows.

## Applied storage and memory policies

On the Linux VM, `configure-runner-io.sh` bounds global dirty writeback at
256 MiB and starts background writeback at 64 MiB, replacing RAM-percentage
thresholds that grew with dynamic memory. The data disk now uses
`mq-deadline`, and a udev rule matches its filesystem UUID so the policy
survives device reorder. Files live in `/etc/sysctl.d/90-runner-io.conf` and
`/etc/udev/rules.d/90-runner-io.rules`. The script is installed as
`/usr/local/sbin/nomercy-runner-io`. Original runtime values and prior files
were retained in `/var/lib/runner-data/runner-io-backup-e2yTqFgU/`.
The settings were read back and the deployed script passed `bash -n`.

The operator separately approved increasing only the Linux VM's dynamic
memory buffer from 100% to 150%. `Set-LinuxRunnerMemoryBuffer.ps1` checks
the projected change against at least 32 GiB free host physical memory and
20 GiB free commit, then verifies the live change and rolls back its buffer
on verification failure. No minimum/maximum allocation, other VM, or host
memory policy was changed. Repository settings retain 150% for future use.

The online change completed at 05:45:51 with the VM still running and its
uptime preserved. Workload demand had fallen since the earlier plan, so an
extra 19 GiB was not immediately allocated: the setting is a dynamic target,
not a fixed RAM assignment. At readback it had 90.8 GiB assigned, with about
76 GiB free physical host memory and 106 GiB free commit. The buffer is
additional cache headroom; it does not eliminate mandatory build writes.

## Verification and limits

At 05:45:27, seven runners had started patched Workers. Nine real jobs had
already completed successfully on the corrected assembly (runners 2, 4, 6
and 9). Every observed new Worker reported a 300-second receive timeout and
received its job message. Some other jobs were still executing. The last
GitHub poll at 05:45:15 showed all ten Linux runners online and no active
alarms. All ten DLL checksums matched the tested artifact, and no runner
container was restarted by this work.

The two replacement FFmpeg x64 builds remained in progress. Their final
results cannot be claimed before GitHub reports completion. This repair
addresses a reproduced startup defect and reduces avoidable I/O stalls;
it does not certify zero future hardware, network or CI-code failures.
The shared mechanical array remains a performance constraint under many
concurrent write-heavy jobs. A later offline alarm must be investigated
against its timestamps rather than dismissed because the startup patch
is installed.

At the later 05:57:30 readback, 19 patched Workers had received their job
messages on seven runners. Fifteen jobs had completed successfully and
three were still executing. One completed job reported failure: app-web
[verify, job 133098229082](https://github.com/NoMercy-Entertainment/nomercy-app-web/actions/runs/43806448184/job/133098229082).
Its log showed all 3,004 tests passing, followed by Vitest reporting an
uncaught `ReferenceError: document is not defined` from a delayed callback
in `src/components/NMComponent.vue:122`. This was an application/test-lifetime
error after successful Worker startup, not a Worker transport failure.

The older runner-5 server job failed one of 600 setup tests with a
`TimeoutException` in `DegradedModeRecoveryLoopTests` at 05:23:56, during the
storage-stall interval. The log does not establish whether load or a test
race was decisive, so this was not dismissed as a proven unrelated code
failure. Its subsequent attempt on runner 6 passed the fast-test phase and
was building the plugin template at 05:58. No application tests were edited
by this repair. The 05:52:09 GitHub poll again showed all ten Linux runners
online and no active alarms. Full VM I/O pressure had fallen to 8.03% over
the last five minutes at 05:57; workload changes prevent attributing that
entire reduction to the new settings.

Final checkpoint at 06:03:09: all ten Linux runners were online, no alarms
were active, and the server retry job `133096144554` had completed with
`success`, including its fast tests and plugin-template build. The two
FFmpeg x64 build jobs were still in progress at the last workflow poll.
Focused repository checks passed: 5 Python tests passed and 5 platform-only
checks were skipped on Windows; the two deployment behavior tests separately
passed on the Linux worker. PowerShell syntax/configuration parsing,
`bash -n`, Python compilation and `git diff --check` passed. No broader
application-code changes were made.

Raw, local evidence and scripts from the investigation are retained under
`scratchpad/incident-20261116/` (ignored by Git). They include kernel/cgroup
and sysstat snapshots, Worker probes, ARCCONF data and repeated counters,
operator-approved deployment output, runtime hashes, controller alarms,
the fleet-template backup/update, and memory-budget readback.

Operational references: [Worker build and rollback](../../../images/github-worker-startup/README.md),
[Linux VM memory buffer utility](../../../infra/hyperv/Set-LinuxRunnerMemoryBuffer.ps1),
[kernel writeback controls](https://docs.kernel.org/admin-guide/sysctl/vm.html),
[Hyper-V storage I/O](https://learn.microsoft.com/en-us/windows-server/administration/performance-tuning/role/hyper-v-server/storage-io-performance),
[smartmontools Windows AACRAID limitation](https://www.smartmontools.org/static/doxygen/os__win32_8cpp_source.html).
