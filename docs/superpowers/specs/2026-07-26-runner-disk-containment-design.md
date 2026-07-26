# Runner Disk Containment — Design

**Date:** 2026-07-26
**Status:** Phase A deployed to all six runners 2026-07-26; Phase B pending a 24-48h measurement window
**Scope:** `D:\docker-compose\GithubRunners` only. BeastStack is out of scope and must not be touched.

## Problem

Six self-hosted GitHub Actions runners filled the shared Docker VM disk to 100%,
taking down BeastStack services (FlareSolverr failed with `[Errno 28] No space
left on device`; another service returned 500s). The runners' writable layers
totalled ~911 GB against a 1007 GB disk; every other container on the host
combined used under 3 GB.

## Evidence

Measured 2026-07-26 inside `githubrunners-github-runner-1`:

```
$ du -x -d1 /var | sort -rn
149123464   /var
149119396   /var/lib          <-- 99.997%
     2836   /var/cache
     1172   /var/log

$ du -x -d1 /var/lib/docker | sort -rn
148955400   /var/lib/docker
148868720   /var/lib/docker/fuse-overlayfs   <-- 99.9% of /var
    73256   /var/lib/docker/image
    13076   /var/lib/docker/buildkit
```

Logs and apt caches are negligible. The consumer is Docker-in-Docker layer
storage.

Inner-daemon accounting across the fleet (`docker exec ... docker system df`):

| Runner | Images | Build cache | Cache records |
|---|---|---|---|
| 1 | 1.44 GB | 68.59 GB | 528 |
| 2 | 25.75 GB | 83.98 GB | 590 |
| 3 | 26.43 GB | 76.06 GB | 606 |
| 4 | 27.13 GB | 101.2 GB | 607 |
| 5 | 27.05 GB | 80.33 GB | 613 |
| 6 | 24.91 GB | 44.31 GB | 376 |

All six report **0 active images** and **0 running containers** — the entire
footprint is reclaimable garbage. `docker buildx du` on runner-3 confirms
76.06 GB total, 76.06 GB reclaimable.

Note: runner-1's daemon reports ~70 GB while `du` measures 149 GB on disk.
Extracted fuse-overlayfs diffs exceed BuildKit's blob accounting, so the
reported figures are a floor, not the true footprint.

## Root cause

Each runner runs its own Docker daemon, started at
[`scripts/start.sh:65`](../../../scripts/start.sh#L65), with a `daemon.json`
generated at [`scripts/start.sh:43-50`](../../../scripts/start.sh#L43-L50)
containing no garbage-collection policy. Nothing anywhere prunes. Six
independent daemons build the same projects and each hoard a private copy of
every layer forever.

Environment facts confirmed:

- Inner Docker **29.4.0**, BuildKit **v0.29.0**, `docker` driver (built-in).
- Storage driver `fuse-overlayfs`, containerd snapshotter disabled.
- `docker_data.vhdx` is **already on D:** (`D:\Docker\DockerDesktopWSL\disk\`,
  1011 GB). The problem is not the drive letter — it is that a single
  fixed-size VHDX is shared with BeastStack. D: itself has 3.67 TB free.

## Goals

1. Runner disk growth is bounded and cannot starve BeastStack.
2. Runner storage eventually lives on the host under
   `D:\Docker\GithubRunners\Data`.
3. Six runners, same registration, same jobs — behaviour unchanged.

## Non-goals

- Any change under `D:\docker-compose\BeastStack\`.
- Any host-wide or stack-wide Docker command.
- Fixing BeastStack's broken root `docker-compose.yml`.

## Isolation property (why this is safe)

Each runner's `dockerd` is a separate Docker installation with its own
`/var/lib/docker` and no socket to the host daemon. It cannot enumerate or
touch BeastStack containers. Every prune in this design runs *inside* a runner
via `docker exec githubrunners-github-runner-N docker ...` and is structurally
incapable of reaching Immich, MinIO, Forgejo or any other BeastStack service.

No host-level `docker system prune`, `docker volume prune`, or
`--remove-orphans` is used at any point.

## Phase A — cap in place

Pure configuration. No compose restructure, no bind mounts, no container
recreation. Two changes, both in `scripts/start.sh` (already bind-mounted to
`/root/start.sh`, so it takes effect on restart without an image rebuild).

### A1. BuildKit GC policy

Extend the `daemon.json` heredoc at `scripts/start.sh:43-50` with a `builder.gc`
block targeting ~20 GB of build cache per runner. This is the load-bearing
change: it converts unbounded growth into a self-limiting steady state.

**Open detail, to be resolved during the runner-1 test, not guessed:** Docker 29
deprecated `defaultKeepStorage` in favour of `maxUsedSpace` / `reservedSpace` /
`minFreeSpace`. The implementation must confirm which key this daemon accepts by
observing that `dockerd` starts and the policy appears in `docker info`. A
malformed `daemon.json` prevents `dockerd` from starting.

### A2. Janitor loop

A background loop started before `exec ./run.sh`, covering what BuildKit GC does
not:

- `docker image prune -af --filter until=72h` against the inner daemon — the
  25-27 GB of dead images each runner holds. The 72h floor deliberately
  preserves recently-used base images so ordinary builds keep their warm cache.
- Sweep `_work/*` directories untouched for 14+ days, clearing abandoned
  workspaces such as the three `actions_github_pages_*` found on runner-1. An
  active job touches its workspace continuously, so mtime is a safe
  discriminator against deleting live work.
- Trim `_diag` logs.

Runs every **6 hours** via a backgrounded shell loop rather than a cron daemon,
keeping the container self-contained. The first pass is delayed rather than run
at startup, so a runner restarting into a queued job is not competing with the
janitor for I/O.

### Expected outcome

~25-30 GB per runner, ~180 GB fleet-wide, against today's 911 GB.

This is a **soft** cap. A single pathological job can overshoot between GC
passes. Phase B provides the hard guarantee.

### Rollout

1. Apply to **runner-1 only**. Restart it alone (`docker restart
   githubrunners-github-runner-1`).
2. Confirm `dockerd` starts, the GC policy is live, and the runner re-registers
   and reaches "Listening for Jobs".
3. Only then restart the remaining five, one at a time.

Rolling a bad `daemon.json` to all six simultaneously would take the entire
fleet offline. The staged rollout is mandatory, not optional.

### Verification

- `docker exec githubrunners-github-runner-N docker info` shows the GC policy.
- `docker exec githubrunners-github-runner-N docker system df` shows build cache
  trending to the cap.
- `docker ps -s` writable layers stay small (baseline: 90-182 GB each).
- All 22 containers still running; BeastStack untouched.
- Free space on the Docker VM disk recovers and holds.

## Phase B — hard isolation on D: (deferred)

Once Phase A has stopped the bleeding, prototype on a single runner:

Bind-mount `D:\Docker\GithubRunners\Data` into each runner; each runner creates
and loop-mounts its own sparse ext4 image at `/var/lib/docker`. The containers
are already `privileged: true`, so `losetup`/`mount` are available.

This gives a genuine hard cap (the image file size), places data physically on
D:, and runs fuse-overlayfs on real ext4 rather than over 9p.

Requires replacing `deploy.replicas: 6` with six explicit services
(`github-runner-1..6`), because all replicas otherwise receive the **identical**
bind source — six daemons sharing one `/var/lib/docker` would corrupt each other
within minutes. The same constraint applies to `_work` and toolchain caches.

Risks to evaluate during the prototype: loop-mount-over-9p performance,
sparse-file semantics over 9p, and filesystem dirtiness after an unclean
container kill.

## Reclaiming the existing ~911 GB (destructive — separate approval)

Not part of Phase A rollout. Requires explicit approval before execution.

Key finding: **the destructive path may prove unnecessary.** Nearly all 911 GB
is unused, reclaimable cache. Phase A's janitor and GC will reclaim a large
share in place, without recreating containers. Measure after Phase A settles
before considering recreation.

If recreation is still needed, it is **safe for registration** — verified in
`scripts/start.sh`:

- `start.sh:3-4` generates a fresh random runner name on every start.
- `start.sh:148` registers unattended with `--replace` on every boot.
- `start.sh:141-143` traps `EXIT`/`TERM` and deregisters cleanly on shutdown.

No manual re-registration is required. Preconditions before any recreation:

- Drain first — runners were observed executing `jvm-android` jobs; do not kill
  mid-job.
- Confirm no orphaned registrations accumulate in the org runner list.
- Scope every command explicitly to this compose project. Never
  `--remove-orphans`.

## Measured outcome

Deployment: all six runners migrated 2026-07-26 via `docker restart` only. No
container was recreated, no writable layer was dropped, no registration was
lost. All 22 containers (6 runners + 16 BeastStack) were confirmed running
throughout, verified by name census and `docker inspect` timestamps.

### Immediate results

- Host disk free: 29 GB (98% used) at baseline → 273 GB (72% used) after
  rollout. Measured as `1007G total, 684G used, 273G avail, 72%`.
- Approximately 244 GB reclaimed with zero destructive action — BuildKit GC
  alone.
- Fleet build cache: ~941 GB at baseline → ~192 GB and still falling.
- Per-runner build cache immediately after rollout: runner-1 21.98 GB,
  runner-2 31 GB, runner-3 35.93 GB, runner-4 31.45 GB, runner-5 36.33 GB,
  runner-6 35.24 GB. Runner-1 is furthest converged because it restarted
  first.
- Runner-1's trend, which is the direct evidence the GC policy enforces:
  68.59 GB (baseline, 528 cache records) → 42.71 GB (~6 min after restart) →
  21.98 GB (470 records), converging on the 20 GB `maxUsedSpace` cap.

### Validation finding

`dockerd --validate` returns `configuration OK` even for entirely fabricated
field names — Go's JSON unmarshal silently ignores unknown keys. Config
validation alone therefore proves nothing about whether a policy is active.
This nearly caused a false-confidence failure. Enforcement was confirmed only
by measuring build cache falling over time. The `builder.gc.policy[].maxUsedSpace`
key was separately confirmed genuine against moby's `daemon/config/builder.go`
(`BuilderGCRule.MaxUsedSpace`, a bare `json:",omitempty"` tag matched via Go's
case-insensitive unmarshal) and containerd's `filters.ParseAll` (documented:
"If no filters are provided, the filter will match anything"), which confirms
a filterless single-entry policy does select cache records rather than being
inert.

### Other findings

- The abandoned `actions_github_pages_*` directories are at `/root/`, NOT
  under `/root/actions-runner/_work` as the design originally assumed. They
  are fleet-wide: runner-1: 3, runner-2: 3, runner-3: 2, runner-4: 4,
  runner-5: 3, runner-6: 0. The janitor was extended with a dedicated
  `-maxdepth 1` sweep for them.
- The janitor's first sweep runs 6 hours after each container start; at time
  of writing it had not yet run, so the 25-27 GB of dead images per runner is
  still outstanding and expected to be reclaimed on that schedule.

### Phase B assessment

**Likely unnecessary, pending confirmation.** Phase A alone recovered 244 GB
and capped growth, without the complexity, 9p performance cost, or unclean-
shutdown risk of the ext4-loopback approach. The decision should be made on
the 24-48h measurement, not now.

## Known issues

**Pre-existing, not introduced by this work.** `scripts/start.sh` line ~158
runs `./config.sh remove --token "$REG_TOKEN" 2>/dev/null || true`, swallowing
failures. When removal fails, the subsequent `--replace` config aborts with
"Cannot configure the runner because it is already configured" and `run.sh`
falls back to the existing registration. Observed on runner-6, which
self-healed in ~30s and reused its existing identity (no duplicate
registration created). Traced via git history to commit `7998be3`
(1 April 2026). Residual risk: a ~30s window where the container reports `Up`
and the daemon is healthy but the runner cannot receive work, invisible to
container-level health checks. Suggested fix, not applied here as it is out
of scope for a disk-containment change: make the removal failure non-silent,
or fall back to deleting the local `.runner`/`.credentials` files.

## Rejected alternatives

**Drop DinD, mount the host socket.** Would dedupe cache 6x and give a single
prune point. Rejected: runner jobs would gain full control of the daemon running
Immich, MinIO and Forgejo — a `docker rm` in a workflow could destroy the user's
photo library. Directly contradicts the primary constraint.

**Bind-mount `/var/lib/docker` to a Windows path.** Does not work. Docker's
overlay drivers cannot operate over the 9p/virtiofs mount Docker Desktop uses
for Windows paths; the daemon refuses to start or corrupts data.

**Dedicated WSL distro / separate VHDX on D:.** Native block-device performance
and a hard cap. Rejected: Docker Desktop manages only the single
`docker-desktop` distro here; cross-distro `/mnt/wsl` mounts do not survive
Docker Desktop restarts and updates, and none of it is reproducible from
compose.

**Named volume for `/var/lib/docker`.** An anonymous volume would solve
per-replica identity elegantly, but the volume still lives inside the same
shared `docker_data.vhdx` — it achieves neither isolation from BeastStack nor
placement on D:.

## Out of scope, flagged

- `RUNNER_CPU_LIMIT=0` and `RUNNER_MEM_LIMIT=0` in `.env` leave the runners
  unlimited on CPU and RAM — a second contention path against BeastStack if the
  observed failures included slowness or OOM rather than only disk errors. Not
  included in this design; raise separately if wanted.
- `.env` holds a live classic GitHub PAT. Correctly gitignored, but it was
  displayed during the 2026-07-26 investigation session and should be rotated.
