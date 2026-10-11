# Remaining uniform-profile capacity, 2026-10-11

The requested profile is 8 cores, 16 GiB memory, 100 GiB owned storage and
40 GiB cache per runner. It is verified live for the 13 Linux runners and
the two Windows x64 runners. The two ARM and two macOS runners still need
their resource cutovers; their current, smaller allocations are not the
requested profile.

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

## Authorized maintenance route

The operator approved a 64-GiB pagefile on the host SSD and a controlled host
restart after all active jobs finish. The read-only preflight measured
95.9 GiB SSD free, leaving 31.9 GiB after paging allocation. The plan predicts
43.1 GiB commit headroom after the 44-GiB VM increase. This is a capacity
plan, not evidence that the pagefile is active or the VM cutovers are done.

The maintenance checkpoint records all 19 runner and registration identities.
A native database watcher waits for actual idle jobs, closes the fleet through
the existing provider drain, and requires confirmed drained states before
host shutdown. Recovery restores serving capacity as each worker becomes
healthy. Physical memory and active pagefile usage are checked again after
the restart; changing paging settings alone does not pass that gate.

## Origin-policy verification

Both macOS and Windows ARM have now executed real GitHub jobs through their
installed origin hooks. All 13 managed GitHub runners report an unchanged
guard with a recorded allowed decision. The dashboard payload and heading
renderer were checked for all four GitHub fleets: every heading is green
with the requested Fillz access policy and outside-fork refusal text. This
does not mean the pending ARM/macOS resource cutovers are complete.
