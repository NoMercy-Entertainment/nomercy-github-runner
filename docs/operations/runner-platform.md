# Operating the uniform runner platform

For the person who runs this fleet. What runs today, how it is deployed, and
what to do when something goes wrong. The design is
`docs/superpowers/specs/2026-09-17-uniform-hyperv-runner-platform-design.md`
(section numbers below refer to it), and the plan with every task's gate is
`docs/superpowers/plans/2026-09-17-uniform-hyperv-runner-platform-implementation.md`.

## 1. Where things stand

As of 2026-09-24. Every runner below is built, registered and driven by the
controller; nothing is started by hand.

| Part | Where | State |
| --- | --- | --- |
| Control plane: controller + dashboard | VM `rnr-control` (10.77.0.10), compose `/etc/runner-platform/compose.yml` | Running. The public name reaches the dashboard through the host's portproxy |
| GitHub Linux, 10 runners | VM `rnr-linux-1` (10.77.0.20), 80 GiB, 56 vCPU | Running. One container each, read-only root, own 100 GiB filesystem, 16-core cpuset, 32 GiB RAM + 32 GiB swap |
| Forgejo Linux, 3 runners | `rnr-linux-1` | Running, 6 GiB RAM + 6 GiB swap, labels `ubuntu-latest`, `ubuntu-22.04(-full)`, `ubuntu-24.04(-full)` |
| Forgejo Windows, 1 runner | BEAST-UNIT itself (OPEN-2) | Running as its own service and virtual account, in a Job Object, on its own fixed 100 GiB VHDX, labels `windows-2022`, `windows-latest` |
| GitHub Windows | BEAST-UNIT | Cell buildable; see section 1.1 |
| Forgejo macOS, 1 runner | Hyper-V VM `macos-runner` (QEMU guest) | Running from the debloated reusable base with its original Forgejo registration; the per-runner pool is not deployed |
| Windows ARM64 (GitHub + Forgejo) | Separate QEMU guest on `macos-runner` | Windows 11 ARM64 installer booted; guest installation and both registrations still in progress. See `images/windows/arm64/README.md` |
| GitHub macOS | - | Not running; see section 1.1 |
| WSL distro `github-runners` | - | Stopped, kept as the rollback copy of the old volumes. Its worker `wsl-linux-1` has no runners and reads degraded |
| Docker Desktop | its own WSL VM, 32 GiB | Unrelated to the runners since the move |

### 1.1 What is not finished

- **GitHub macOS.** The existing macOS guest is one shared appliance with no
  per-runner memory limit, and placement refuses to put a second runner on a
  host whose memory it cannot account for. The per-runner appliance pool
  (`agent/runtimes/macos_pool.py`, `images/macos/`) is built and tested. On
  2026-09-24 the debloated standalone base booted to macOS and SSH from a
  fresh overlay; see `images/macos/pool/BASE-20260924.md`. The shared Forgejo
  guest now uses this base, but the pool has not been deployed and the macOS
  host still runs the older agent.
- **Forgejo macOS rebuildable.** Needs `FORGEJO_RUNNER_ARTIFACT_MACOS` naming
  the installed template, which the pool work brings with it.
- **The WSL rollback copy** is deleted only after a stability period, by the
  operator.
- **The Hyper-V VMs `rnr-control` and `rnr-linux-1` run on `.avhdx`
  checkpoint disks.** Merge the checkpoints in a quiet window.

### 1.2 Faults found and fixed on 2026-09-22

Each looked healthy on the dashboard, which is why they are listed:

- Every Forgejo runner carried the pilot label, so no Forgejo job matched
  (fixed on the fleets and in `Initialize-RunnerPlatform.ps1`).
- Every GitHub job on the Linux runners since the move was marked failed by
  the job-completed hook path (`/runner/cleanup` has no `.sh`).
- A runner added by scaling got no cpuset and saw all 56 cores.
- The Linux runners could not be recreated at all: placement did not accept
  the worker's per-runner filesystems as a disk quota.
- A fleet-wide cache clear took every idle runner out at once.

### 1.3 Forgejo Linux memory: what 6 GiB was measured against

Each Forgejo Linux unit has 6 GiB RAM and 6 GiB combined RAM+swap. Measured
on 2026-09-22, about a day after the units were made and with jobs having run
on all three (`memory.stat`, `memory.events` and `memory.peak` of each unit's
cgroup, not `docker stats`):

| | unit 1 | unit 2 | unit 3 |
| --- | --- | --- | --- |
| peak | 6144 MiB (at the limit) | 6145 MiB | 6144 MiB |
| anonymous (real process memory) | 56 MiB | 55 MiB | 53 MiB |
| page cache | 2646 MiB | 4175 MiB | 4208 MiB |
| `max` events (reclaim at the limit) | 4597 | 2785 | 1270 |
| `oom_kill` | 0 | 0 | 0 |
| swap used at peak | 0 | 0 | 0 |

So the limit is reached, but by page cache, which the kernel reclaims; the
work itself needs tens of MiB. Nothing was killed and nothing swapped. 6 GiB
stands.

**When to raise it:** any Forgejo unit showing `oom_kill` above 0 in
`memory.events`, or anonymous memory above 4 GiB during a measured job. A
raise has to fit the Linux worker's admission - 72 GiB RAM and 640 GiB swap,
bounded overcommit - counted against the ten GitHub units at 32 GiB + 32 GiB
each. Read `anon` before deciding: a unit sitting at its limit is not
evidence on its own.

## 2. Gates

Every task in the plan carries a gate. Nothing past LOCAL runs unattended.

| Gate | Meaning |
| --- | --- |
| LOCAL | A developer machine with the repository and Python. No infrastructure. Safe unattended |
| HYPERV | The real Hyper-V host, elevated. Creates or configures VMs |
| WINDOWS-INFRA | Windows host configuration: features, firewall, services, certificates |
| MACOS-ENV | Access to the macOS/QEMU environment |
| FORGE-LIVE | Registers or deregisters against the live GitHub org or Forgejo instance |
| NEVER-AUTO | Destroys data, stops capacity or changes production routing. A human decides, at the time, and the decision goes in the evidence file |

## 3. Deploying

Order: worker, then control plane, then dashboard - and never while the
controller is rebuilding runners (a restarted agent drops the verb in
flight). Confirm the worker is healthy before the next step.

### 3.1 Controller and dashboard

Both run the same image, `nomercy/runner-dashboard:<tag>`, built on the
control plane from `dashboard/` (the tag is a hash of the files). To deploy:

1. Build: copy `dashboard/` (without `tests/`) to the control plane and
   `docker build -t nomercy/runner-dashboard:<tag> .` there.
2. Back up `control.db` and `history.db` with SQLite's backup API into
   `/data/maintenance/deploy-<stamp>/`, and copy `compose.yml` beside itself.
3. Set both `image:` lines in `/etc/runner-platform/compose.yml` to the tag
   and `docker compose up -d controller`, then `dashboard`.
4. Check `docker logs rnr-controller` shows passes and `/login` answers.

Schema changes are additive and applied on start. Rollback: put the saved
`compose.yml` back and `docker compose up -d`. The runners keep running
throughout; only the page is away for a few seconds.

Settings the controller reads come from `/etc/runner-platform/controller.env`
(and `dashboard.env` for the page); a fleet's own settings, on the Settings
page, win over them.

### 3.2 Linux unit images

Built on `rnr-linux-1` from `images/linux/unit` (`Dockerfile.github`,
`Dockerfile.forgejo`), tagged with a hash of the directory. Put the tag in
the fleet's unit template on the Settings page, then recreate the fleet: the
controller rebuilds one runner at a time, only when idle, keeping each
runner's cpuset, memory and disk. After any image change, prove it with a
real job and read the result counts in history (see the GitHub hook fault
in 1.2).

### 3.3 The Linux agent

`/opt/runner-agent/agent` on `rnr-linux-1`, service `runner-agent`
(`KillMode=process`: a restart leaves the runners running).

### 3.4 The Windows agent

`infra\hyperv\Install-WindowsWorker.ps1 -WindowsStorage`, elevated, from
the repository's HEAD. It keeps the code it replaces as
`C:\ProgramData\nomercy\agent\app.previous`. A runner made before storage
was switched on keeps its plain directory and cannot be managed with storage
on: remove that runner before switching storage on, then add a new runner.
Creating a runner allocates its fixed 100 GiB disk, which takes about
six minutes.

## 4. Where things live

| Thing | Where |
| --- | --- |
| Control store and history | volume `runner-platform_controller-data` on `rnr-control` (`/data/control.db`, `/data/history.db`) |
| Linux runner data | `/var/lib/runner-data/runners/<runner_id>/` on `rnr-linux-1`, one ext4 image each; swap file on the same disk |
| Windows runner disks | `D:\runner-disks\<runner_id>.vhdx`, mounted at `D:\runners\<runner_id>` |
| Windows templates | `C:\ProgramData\nomercy\templates\` |
| macOS runner | inside the QEMU guest of VM `macos-runner`, `/Users/runner/templates/` |
| SSH to the VMs | `rnr-admin@10.77.0.10` / `.20` with `D:\HyperV\runner-platform\ssh\id_ed25519` |

Forge tokens are read from the environment files; the secret store
(`POST /api/v2/secrets/...`, admin) overrides them once a token is set there.

## 5. Runbooks (design 22)

### 5.1 A runner is stuck in a transitional state

1. `GET /api/v2/runners/<runner_id>` - read `card.current_operation`,
   `card.last_error`, `card.readiness` and the audit tail.
2. `GET /api/v2/operations/<operation_id>` - attempts and trace.
3. Past its deadline, a creation is swept by the reconciler to `failed`
   with nothing left outside; a removal waiting on the forge
   (`deregistering`) is retried on every pass and finishes once the forge
   answers. A registration whose reply was lost is held until it could no
   longer be in flight (5 min); then, if the forge lists no runner of that
   name, it is rolled back.
4. `POST /api/v2/runners/<runner_id>/actions/repair` with an
   `Idempotency-Key` header forces `failed -> provisioning`. Repair keeps
   what is there - the unit, and a registration the forge still has - and
   makes only what is missing.

### 5.2 A worker is degraded

Three missed heartbeats (30 s) and a worker is degraded; one speaking a
protocol major the controller does not is degraded with that reason
(`GET /api/control/workers`). A degraded worker is sent nothing destructive,
and its runners are held, not removed: they return when it does.

### 5.3 The disk is filling

1. Each runner's storage and cache are on its card, from the heartbeat
   every five minutes.
   The GitHub Linux fleet clears unused nested Docker cache and old workspaces
   synchronously after every job, then again at unit startup. The cleanup
   preserves the just-finished job's workspace until the next safe start.
2. `POST /api/v2/fleets/<fleet_id>/clear-cache` clears the idle runners of a
   fleet and reports, per runner, what it freed or why it was skipped.
3. If that is not enough, remove runners individually (admin): the reconciler
   drains, deregisters and removes each one without interrupting its job.
4. Never delete a volume by hand: `remove` does it after the registration.

### 5.4 Rotating an agent certificate

`docs/operations/certificate-rotation.md`: `python -m control certificates
status` shows every certificate's expiry and warns 30 days ahead; renewals
are prepared and activated per role, under the existing authority.

### 5.5 Emergency: stop everything without losing registrations

Remove each runner from the dashboard, or use
`POST /api/v2/runners/<runner_id>/actions/remove` with an `Idempotency-Key`
header as admin. The desired runner count falls with each removal and stays
at zero after a reboot. Busy runners finish their jobs first.

### 5.6 A runner's forge says something else than the process

`card.readiness` has both halves. A process that is up while the forge
cannot be asked reads `unknown`; while the forge says offline, `offline`.
Neither is `ready`. Check the forge first: this is how the WSL DNS outage
and the Forgejo crawler overload first looked.

## 6. Version deprecation

GitHub deprecates runner versions, and a fleet on a deprecated one stops
registering all at once. Forgejo's self-built artefacts have no release feed
to watch. So at each release:

| Artefact | Where it is pinned | What to do |
| --- | --- | --- |
| GitHub runner, Linux | `RUNNER_VERSION` and `RUNNER_SHA256` in `images/linux/unit/Dockerfile.github` | Bump, rebuild the image (3.2), set it on the fleet, recreate the fleet |
| GitHub runner, Windows | `infra/windows/templates/actions-runner-<version>-windows` | Fetch with `images/windows/fetch-actions-runner.ps1`, redeploy the Windows agent, recreate |
| Forgejo runner, Linux | `BASE` in `images/linux/unit/Dockerfile.forgejo` | Rebuild from the new release (3.2) |
| Forgejo runner, Windows | `images/windows/manifest.json` | Rebuild from the tag with `images/windows/build-forgejo-runner.md`, and add a manifest line. The running binary reports `dev`, so its provenance is unknown |
| Forgejo runner, macOS | `images/macos/manifest.json` | As Windows, with `GOOS=darwin`. Not yet measured |
| The macOS appliance | QEMU/KVM inside Hyper-V | A host or hypervisor update can break it with no vendor support (design 9.3.1). Check it after each |

## 7. Access

| Role | May |
| --- | --- |
| viewer | read everything; post nothing |
| operator | start, stop, restart, drain, cancel drain, clear cache, add runners |
| admin | as operator, and remove, recreate, deregister, recreate a fleet, manage access, set the forge tokens |

Every request to the controller is audited, accepted or refused, with the
actor and the reason, and how each operation ended: `GET /api/v2/audit`,
filtered by `runner_id`, `fleet_id`, `verb` or `decision`. The audit table
refuses updates and deletes.
