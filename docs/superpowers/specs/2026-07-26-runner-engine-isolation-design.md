# Runner Engine Isolation — Design

**Date:** 2026-07-26
**Status:** Implemented and verified 2026-07-26
**Scope:** `D:\docker-compose\GithubRunners` and a new WSL distro. BeastStack is
read-only context — nothing under `D:\docker-compose\BeastStack\` changes.

## Requirement

The six self-hosted GitHub Actions runners must be **physically unable** to
affect the BeastStack production stack (Immich, MinIO, Forgejo, Portainer, Open
WebUI, Stable Diffusion). Not "capped", not "unlikely to" — unable.

Stated by the user: *"the github runners stack need to be on a different virtual
disk than beast-stack, so they cannot corrupt or break or fetch each other's
data."*

## Why Phase A is insufficient

Phase A (2026-07-26, branches `runner-disk-containment` and
`fix-runner-registration`, both deployed) capped BuildKit build cache at 20 GB
per runner and added a 6-hourly janitor. It took the host disk from 29 GB free
to 279 GB free and stopped unbounded growth.

But it is a **policy** control, not a **structural** one:

- Runners and BeastStack still share one virtual disk,
  `D:\Docker\DockerDesktopWSL\disk\docker_data.vhdx` (1007 GB usable).
- They share one Docker daemon, so they share an image store, a volume
  namespace, and a build cache.
- A single job writing faster than GC reclaims can still fill the shared disk
  between sweeps, which is exactly the failure that took BeastStack down.

Phase A makes the disaster unlikely. It does not make it impossible.

## Design

A second WSL distro, `github-runners`, running its own Docker Engine, with its
virtual disk on D:.

```
D:\Docker\DockerDesktopWSL\disk\docker_data.vhdx   BeastStack   (Docker Desktop)
D:\Docker\GithubRunners\Data\ext4.vhdx             Runners      (own dockerd)
```

| | BeastStack | Runners |
|---|---|---|
| Virtual disk | `docker_data.vhdx` | `D:\Docker\GithubRunners\Data\ext4.vhdx` |
| Docker daemon | Docker Desktop | dedicated `dockerd` in the distro |
| Image store | separate | separate |
| Volume namespace | separate | separate |
| Can enumerate the other's containers | no | no |
| Can fill the other's disk | **no** | **no** |

Two daemons, two disks, no shared state and no socket between them. A runner
cannot list, read, write, or delete anything belonging to BeastStack, and
filling its own disk produces failed builds on that engine and nothing else.

Everything from Phase A carries over unchanged: the `builder.gc` cap, the
janitor loop, and both registration fixes. Those now govern usage *inside* the
isolated disk rather than inside a shared one.

### Disk sizing

**1 TB maximum**, matching the current `docker_data.vhdx` and equal to WSL's
default, so no explicit resize is needed at creation.

The ceiling is deliberately generous. It is not the mechanism that keeps usage
low — Phase A's 20 GB build-cache GC is. The ceiling exists solely as the wall
guaranteeing the runners cannot reach BeastStack's disk. D: has 3,517 GB free,
so a generous wall costs nothing.

The `.vhdx` is dynamically allocated: it starts near-empty and grows to what is
used. Expected steady state is ~180 GB.

**Known behaviour, not a defect:** a WSL `.vhdx` does not shrink when data
inside is deleted. Reclaiming host space after a large spike needs a manual
compact. This is the same behaviour that leaves the current `docker_data.vhdx`
at 1011 GB on disk while only 678 GB is in use.

### Environment (verified 2026-07-26)

- WSL **2.7.11.0**, kernel 6.18.33.2 — supports `--location`/`--import`,
  systemd, and per-distro disk management.
- One distro today: `docker-desktop`. No name collision.
- `.wslconfig` present with `nestedVirtualization=true`, `memory=0`,
  `processors=0` (unlimited).
- D: free: 3,517 GB.
- Host Docker Engine 29.5.3; inner runner Docker 29.4.0.

## Consequences the user must accept

Stated plainly because they are permanent, not transitional:

1. **Docker Desktop will not show the runners.** They live on another engine.
   Managing them means `wsl -d github-runners docker ...`, or adding the engine
   to Portainer as a second endpoint (Portainer supports this well).
2. **The distro must be running for runners to work.** WSL distros do not
   auto-start. This requires a scheduled task at boot, and that task becomes a
   new single point of failure. It must be tested with a real reboot, not
   assumed.
3. **Migration destroys the current containers.** This is desirable here — it
   reclaims the ~673 GB currently stuck in their writable layers, which no prune
   can reach (see below).
4. **`deploy.replicas: 6` becomes six explicit services.** Unavoidable: replicas
   all receive identical mounts, and six Docker daemons sharing one data root
   corrupt each other.

## The orphaned-layer finding that makes migration worthwhile anyway

Measured 2026-07-26 after Phase A had fully settled:

| Runner | Writable layer | Inner daemon reports |
|---|---|---|
| 1 | 119 GB | ~0.4 GB |
| 2 | 115 GB | ~0.4 GB |
| 3 | 118 GB | ~0.4 GB |
| 4 | 115 GB | ~1.3 GB |
| 5 | 120 GB | ~1.3 GB |
| 6 | 86 GB | ~24.6 GB |
| **Total** | **673 GB** | |

Build cache is 0 fleet-wide and images are pruned, yet each writable layer still
holds ~118 GB the inner daemon does not account for. The gap was visible at
diagnosis too (runner-1: daemon reported 70 GB, `du` measured 149 GB) and has
grown from 53% of the layer to ~99%.

**Confirmed** by a `du` breakdown inside runner-1 (2026-07-26):

```
/                                   128.3 GB
  /var                              102.8 GB
    /var/lib/docker/fuse-overlayfs  102.5 GB   <-- orphaned
  /root                              14.0 GB   (.gradle 8.2, actions-runner 2.2,
                                                .rustup 1.4, .konan 0.85, .cache 0.73)
  /usr                                9.8 GB
  /opt                                1.7 GB

fuse-overlayfs directories on disk:  471
images the daemon knows about:         4
build cache records:                   1
```

~467 orphaned diff directories holding ~102 GB the daemon has no record of.
Unreachable by any prune, because the daemon does not know they exist. Only
~26 GB of the writable layer is legitimate (OS plus toolchain caches).

Across six runners this is roughly **610 GB of unreachable garbage**.
**Recreating the containers is the only way to reclaim it**, which the migration
does as a side effect.

## Rejected alternatives

**Bind-mount a Windows path with an ext4 loopback file per runner.** Puts the
DinD data root on D: and gives a hard per-runner cap. Rejected as the primary
approach: it runs the build hot path over Docker Desktop's 9p filesystem
(noticeably slower CI), sparse-file semantics over 9p are unproven, and an
unclean kill can leave the filesystem dirty. It also leaves the runner
*containers* on `docker_data.vhdx` — only their inner data moves — so it does
not satisfy the requirement.

**Second VHD attached via `wsl --mount --vhd` into the existing distro.** Native
speed and a separate disk, but the runner containers themselves still live on
`docker_data.vhdx` under Docker Desktop's daemon, so the daemon and image store
stay shared. Partial isolation only.

**Keep Phase A alone.** Rejected by the requirement. Policy, not structure.

**Drop DinD and mount the host socket.** Would give runner jobs full control of
the daemon running Immich, MinIO and Forgejo. The exact opposite of the
requirement.

## Migration principle

Build the new engine **alongside** the running fleet. Prove one runner works
there. Cut over. Only then tear down the old containers.

There must be a working fleet at every point, and the rollback at every stage
must be "start the old containers again" — which remains available until the
final teardown step.

## Out of scope

- Anything under `D:\docker-compose\BeastStack\`.
- Per-runner disk isolation (runner-vs-runner). The requirement is
  runners-vs-BeastStack; a single shared 1 TB disk satisfies it. Six disks would
  be six more things that can fail to mount.
- The `RUNNER_MANUALLY_TRAP_SIG` change that would let deregistration succeed
  mid-job (tracked separately).

## Measured outcome

Migration completed 2026-07-26. Six runners plus a dashboard now run on the
`github-runners` engine; BeastStack is untouched on Docker Desktop.

### Isolation, demonstrated

```
runners engine:   /dev/sde   1007G   24G used   934G free    2%
docker desktop:   overlay    1007G  678G used   279G free   71%
```

Fill test: allocating a 20 GB file on the runners' disk left Docker Desktop's
free space at exactly 279 GB before and after, with all containers running
throughout. The new engine reports zero of BeastStack's containers, images or
volumes.

### Verified working

- Six runners registered and `Listening for Jobs`; org shows exactly 7
  registrations (six plus the unrelated `nomercy-mac-mini`), no orphans.
- **Nested Docker-in-Docker builds work.** A real multi-layer build inside a
  runner (WSL distro -> container -> inner dockerd on fuse-overlayfs ->
  BuildKit) completed, the resulting image ran, and build cache accounting
  moved 0B -> 1.209MB. This was the highest-risk assumption in the design.
- Phase A carried over intact: GC cap present, janitor running.
- Deregistration fix carried over: old fleet stopped with exit 143 via the
  SIGTERM handler.
- Dashboard live on :9200 reading the runners' own socket read-only.

### The bug that nearly sank it

Runners entered a register -> SIGTERM -> restart loop every ~20s. Root cause:
**WSL shuts down an idle distro**, stopping docker.service, whose shutdown
SIGTERMs every container; the next command boots the distro again and the cycle
repeats. `journalctl -u docker.service` showed a deliberate "Stopping
docker.service" with NRestarts=0 and Result=success - not a crash.

Enabling systemd is NOT sufficient; dockerd running as a systemd service does
not keep the distro alive. WSL needs a live session holding it open, which is
what the keepalive scheduled task provides.

A first diagnosis blamed the 9p mount of start.sh and was **wrong**. That test
was confounded: it ran a 100s loop inside the distro, incidentally holding it
open. Re-tested with the confounder controlled, a 9p-mounted start.sh is
completely stable. The false fix was reverted rather than left in place.

### Known limitations

- **The keepalive is logon-triggered**, so the runners return after a reboot
  only once someone logs into Windows. The machine auto-logs-in, so this is
  expected to work, but **the reboot has not been tested** - the user
  explicitly prohibited rebooting.
- ~~No real CI job has run on the new engine yet.~~ **Resolved 2026-07-26:**
  real workflows now run on the isolated engine, including
  `jvm-android` completing with result Succeeded on runner-3. The `documents`
  workflow fails, but it failed identically on the old engine before any of
  this work - pre-existing, not caused by the migration.
- The dashboard's stale-data banner is correct by inspection but has not been
  observed rendering; forcing the failure would need a browser.
- ~~673 GB remains locked in the stopped old containers.~~ **Reclaimed
  2026-07-26.** Removing them took the BeastStack disk from 678 GB used /
  279 GB free to **51 GB used / 906 GB free (6%)**. Removal ran at roughly
  2 GB/min for ~90 minutes - unlinking ~470 orphaned overlay directories per
  runner is inherently slow. The containers entered `Dead` state during it,
  Docker's own removal having failed partway on layers that size.

### Final state (2026-07-26)

Two stacks, two disks, each with ~905 GB free:

```
BeastStack  (Docker Desktop)   1007G   51G used   906G free   6%
Runners     (github-runners)   1007G   52G used   905G free   6%
```

16 BeastStack containers running. Six runners plus the dashboard on the
isolated engine, actively executing real workflows. Build cache capped at
40 GB per runner. Dashboard live on :9200.

Compared with the state that started this work - 978 GB used, 29 GB free, 98%
full, with BeastStack failing on ENOSPC - the shared disk now sits at 6%, and
the runners physically cannot consume it.
