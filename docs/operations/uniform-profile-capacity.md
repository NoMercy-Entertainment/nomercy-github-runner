# Uniform runner profile: verified 2026-10-12

The requested profile is 8 cores, 16 GiB memory, 100 GiB owned storage and
40 GiB cache per runner. All 19 managed runners now report that profile:
13 Linux, two Windows x64, two Windows ARM64 and two macOS. Each has its own
100-GiB filesystem; filesystem metadata slightly reduces usable capacity.
The 40-GiB cache ceiling is inside that owned filesystem, not an additional
disk allocation. CPU and memory figures are enforced ceilings per runner.

Both macOS runners have separate QEMU guests. Windows ARM64 runs on its own
Hyper-V appliance host, independent of macOS. Its outer host has 40 GiB RAM
and 16 vCPU; its Windows guest has 36 GiB RAM and 16 emulated CPUs. The two
Windows services have disjoint eight-CPU windows and separate fixed
100-GiB VHDXs on the new 320-GiB aggregate data disk. That aggregate disk is
not the capacity exposed to either runner. The previous data disk remains
offline for rollback. Both original registration identities were preserved.

The final native continuation completed the ARM live resource checks and
returned both listeners to idle. The independent non-ARM check has no
pending runners. All 13 GitHub origin guards are proven, with no missing or
unread reports; all four GitHub fleets have live Fillz group policy allowing
every organization repository, including public repositories. The public
dashboard returns HTTP 200 through its sign-in route. Complete CI on
`7ec286c` passed before these final read-only checks.

Both owned macOS guests also passed live toolchain checks: Xcode 16.4
(16F6), completed first-launch initialization, Apple Clang 17, Swift 6.1.2,
Apple platform SDKs, CMake, Ninja, Git, Make, Python and Homebrew. FFmpeg is
not preinstalled.

The physical host must not be restarted or have its pagefile settings
changed. The old host-maintenance automation is revoked. The final ARM
cutover restarted only its VM and guest; it did not restart the physical
host or modify paging. The capacity measurements below are historical
preflight records, not current maintenance authorization.

## Host constraint

Measured on the Windows Hyper-V host:

| Item | GiB |
| --- | ---: |
| Physical memory | 255.9 |
| Commit limit | 255.9 |
| Committed memory | 233.6 |
| Available physical memory | 34.3 |
| Required commit reserve (`settings.psd1`) | 20 |
| Commit available above that reserve | 2.3 |

There is no active host pagefile. Available physical memory and available
commit are different; the VM allocation checks use commit headroom.

The prepared ARM design needs a 40-GiB outer host, up from 12 GiB, to back
the 36-GiB Windows guest and its two 16-GiB runner ceilings. The macOS host
needs 40 GiB, up from 24 GiB, for two separate 16-GiB guests and host
overhead. The combined increase is 44 GiB. With the current reserve,
41.7 GiB more commit headroom is needed before both allocations can be
made safely.

Docker Desktop's separate WSL VM accounts for approximately 32 GiB of host
commit and is running. It is not the retired, stopped `github-runners`
distribution. Stopping Docker Desktop cannot be treated as harmless
cleanup; it may interrupt unrelated applications and the Forgejo service.
The running llama service and other host applications are also outside
this runner migration. None were stopped to manufacture headroom.

## Storage

The host SSD has approximately 96.5 GiB free and the data RAID approximately
1,337.5 GiB. A host paging allocation would consume real SSD space and
requires a separately reviewed host-maintenance route; it is not silently
enabled during a running runner job. Moving appliances to another host or
releasing independently authorized host allocations are other capacity
routes.

Outer appliance disks retain existing data, base images and rollback copies.
Their aggregate sizes are not per-runner storage quotas. The per-runner
target remains 100 GiB, including its owned filesystem, with a 40-GiB cache
ceiling. Existing data is not deleted or a live disk shrunk to make an outer
disk resemble the per-runner quota.

## Cutover conditions

The ARM FFmpeg verification job stalled on LIBX265 and was cancelled with
the operator's explicit authorization on October 11. A native host-side
database watcher observed the actual drained state, installed the prepared
origin hooks without changing registration or resource allocations, and
returned the runner to Fillz. The healthy shared macOS guest remains active
until both separate guests have proved boot, SSH, registration preservation,
storage boundaries and restart behavior.

Capacity must be backed before these resource cutovers are activated.
Increasing the budget constant or printing 16 GiB on a card is not a
substitute for that evidence.

## Revised work order, 2026-10-11

The operator has deferred ARM work until the other runners are complete.
The combined host-maintenance watcher was interrupted before paging was
configured, any VM was shut down, or any VM memory was changed. Its recovery
returns drained runners to service after their current jobs finish; no
running job is cancelled. The automatic host-startup recovery task is
disabled while the combined maintenance plan is superseded.

The remaining macOS-only host expansion is 16 GiB (24 to 40 GiB). It must
not include the deferred 28-GiB ARM expansion. The two prepared macOS guests
must be staged one at a time beside the existing guest, with normal CPU
weight, before their registrations are moved. The requested per-runner
profile remains unchanged.

## Revoked host-maintenance route

An earlier plan proposed a 64-GiB SSD pagefile and a physical-host restart.
The operator explicitly forbids further physical-host restarts and pagefile
changes. Its watchers and recovery automation must not be executed. The
saved 19-runner identity checkpoint is retained as recovery evidence only.

## Origin-policy verification

Both macOS and Windows ARM have now executed real GitHub jobs through their
installed origin hooks. All 13 managed GitHub runners report an unchanged
guard with a recorded allowed decision. The dashboard payload and heading
renderer were checked for all four GitHub fleets: every heading is green
with the requested Fillz access policy and outside-fork refusal text. This
does not mean the pending ARM/macOS resource cutovers are complete.

## Prepared macOS transfer and recovery

The macOS migration helper captures only stopped source listeners, checks
their existing forge registration IDs and UUIDs, and compares registration
file hashes after transfer and a maintenance reboot. The adopted Forgejo
runner's registration is copied from its existing legacy directory into the
prepared template; it is never registered again. Offline preparation writes
a persistently disabled launchd definition without starting the listener.
The original guest and its data remain available for rollback.

Guest shutdown now waits on Docker's native exit event and verifies the
resulting power state. Boot readiness still uses the existing SSH readiness
check; this change does not claim that every internal polling path is gone.

The host recovery plan also checkpoints the 22 currently running Docker
Desktop containers by their existing IDs and restores them through the
owning Windows account before returning the fleet to service. Stoney's
four SSH endpoints remain unreachable; a native network-change watcher
records reachability when the host network changes.

The macOS runtime, offline migration preparation and shutdown checks passed
108 targeted tests. Full CI on commit `474fe3a` passed. The earlier `c1e34c1`
run completed with one timing-sensitive compensation fixture failure; its
database commits were observed making progress on the HDD.
Following workflow runs use a bounded 4-GiB tmpfs for temporary test data,
retain the same test suites and 7200-minute job timeout, and unmount that
temporary storage after completion. A real Linux runner verified the mount
and cleanup route. The timeout changes are published separately; a failing
product test is not sufficient evidence to merge its PR.

Asynchronous agent operations now advertise native completion waiting.
The first request remains fast; an authenticated repeat with the same
idempotency key waits on a threading event instead of sending periodic
status requests. Lost connections can be retried without duplicating work;
older agents retain compatibility during the rolling upgrade. This transport
has passed 87 targeted controller tests and full CI on `9c68f16`. Its slow
operation and native completion fixtures use explicit events, so a loaded
disk cannot make their simulated work finish early. It does not change job time limits,
restart listeners, or claim that the older job-log collector is event driven.

The staged macOS continuation briefly drains the two source listeners for a
quiescent data capture, then restores them while installing and restarting
their disabled targets. Before final activation it checks the guest's actual
8-core/16-GiB hardware and 100-GiB APFS quota, drains only those two runners,
and preserves the original guest, configuration and registration files.
Mac tool downloads now use the owned cache rather than a shared toolcache.
The runtime, APFS storage, guest pool and job hooks passed 177 targeted tests.
These are preparation checks; the live cutover has not yet passed.

The historical host watcher is revoked. Remaining work must stay within
the runner VMs and guests. Forgejo event production, the legacy job-log
sampling path, Stoney's LAN-dependent enrollment and the federation RFC
remain separate outstanding work; this resource verification does not
claim they are complete.
