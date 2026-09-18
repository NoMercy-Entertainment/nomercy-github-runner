# Operating the uniform runner platform

For the person who runs this fleet. What exists today, what is built but not
yet deployed, how to deploy the parts that can be deployed, and what to do
when something goes wrong. The design is
`docs/superpowers/specs/2026-09-17-uniform-hyperv-runner-platform-design.md`
(section numbers below refer to it), and the plan with every task's gate is
`docs/superpowers/plans/2026-09-17-uniform-hyperv-runner-platform-implementation.md`.

## 1. Where things stand

| Part | State |
| --- | --- |
| The 13 runners on WSL (`github-runner-*`, `forgejo-runner-*`) | Running as before. Nothing in this work touched them |
| The v1 dashboard (`/`, `/runner/<name>`, `/api/...`) | Unchanged, except for three changes listed in section 3 |
| The v2 dashboard (`/v2`, `/runners/<runner_id>`, `/api/v2/...`) | Built and tested. It appears when the dashboard image is rebuilt |
| The controller (reconciler, provisioning flow, operations, audit) | Built and tested against stand-ins. **Not running anywhere**: no process runs the reconciler loop yet |
| The control agent (`agent/`) and its three runtimes | Built and tested against fakes. **Installed on no machine**, and it has no start-up entry point yet |
| Hyper-V workers, control-plane VM | **Not created** (phase 5, gate HYPERV) |
| The Windows service runner and the macOS appliance | Running as before, outside the control plane. The v2 page shows them with their actions disabled until they are adopted (T-0802) |

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

## 3. Deploying the dashboard image

Rebuilding the dashboard image is the only deployment this work makes
possible without new infrastructure. It is the usual rebuild of the
`dashboard` service in `docker-compose.runners.yml` (container
`runner-dashboard`), on the engine the fleet runs on:

```sh
docker compose -f docker-compose.runners.yml build dashboard
docker compose -f docker-compose.runners.yml up -d dashboard
```

It recreates the dashboard container only; the runners are not touched.

What changes for the running fleet when you do:

1. **`/v2` appears beside `/`.** It shows six fleets and one card per
   runner - today's containers through the v1 collector, the Windows service
   and the macOS appliance with their actions disabled - and says the control
   plane has not run. Actions on today's containers go to the same v1 routes
   the old page uses.
2. **Removing a runner or recreating a fleet needs the admin role**, on v1
   too (T-1902, design 18.2). An operator can still start, stop, restart,
   drain and clear cache. Admins notice nothing.
3. **Secrets are masked on every way out** (T-1801): every `/api/` JSON
   response, every websocket frame and every log line. Nothing that is not a
   secret changes.

Nothing else changes: `/` is identical, the collector is the same, and the
controller does not start. The image gains `api_v2.py` and `cards.py`
(listed on the `COPY` line) and the `cryptography` package it already had.

## 4. Manual installation, when the gates are open

These are the steps the gated tasks will take. They are written so that
whoever opens a gate knows what is expected. None of them has been run.

### 4.1 A Linux worker (T-0601, HYPERV)

1. A Gen2 VM on a new VHDX on `D:`, with static memory per OPEN-5.
2. Ubuntu and Docker CE through `scripts/install-docker.sh`, which sets
   `live-restore`.
3. The agent. **Still to be built:** an entry point that reads the worker's
   configuration and starts `agent.server.AgentServer` with
   `agent.heartbeat.HeartbeatSender`. The pieces exist and are tested; the
   thing that starts them does not.
4. A certificate from the control plane's authority (`control/ca.py`, role
   `agent`), pinned in `workers.certificate_fingerprint` at enrolment.
5. **Runner images adopt the unit layout first** (design 15.1): the
   `/runner/work`, `/runner/cache`, `/runner/reg` and `/runner/logs` mounts,
   and the `/runner/register` and `/runner/deregister` entry points, with the
   plan on standard input. Today's images do neither.

### 4.2 A Windows worker (T-0701, HYPERV + WINDOWS-INFRA)

1. Windows Server per OPEN-2, and no container feature (design 9.2).
2. **A regular Python install, not the Microsoft Store one.** The Store
   Python's children escape every Job Object - measured on this host - and
   `agent.jobhost` refuses to run under it.
3. NSSM, at the path in `agent/runtimes/windows_process.py`'s `TOOLS`, or
   that entry changed to where it is.
4. Runner templates under `C:\ProgramData\nomercy\templates\<name>\`, each
   with `run.cmd`, `register.ps1` and `deregister.ps1`.
5. The agent itself as a service, as in 4.1 step 3.

### 4.3 The macOS appliance (T-0802, MACOS-ENV + NEVER-AUTO)

The agent runs inside the guest as the runner's own user, with templates
under `/Users/runner/templates/<name>/` (`run`, `register`, `deregister`).
Adoption must not re-register the running Forgejo runner: its RunnerSpec
takes the forge's existing ids (T-0802 step 3).

### 4.4 The forge tokens (T-1901 run step, NEVER-AUTO)

The store exists (`control/secrets.py`); moving the tokens into it is a
decision for its time:

1. As admin, `POST /api/v2/secrets/GH_TOKEN {"value": "..."}` and the same
   for `FORGEJO_API_TOKEN`. `GET /api/v2/secrets` then shows both set, with
   a fingerprint, and never a value.
2. Confirm the controller uses them (`SecretStore.overlay`).
3. Only then remove the values from `.env` - by hand, and recorded in the
   evidence file.
4. Rotating the tokens afterwards is a separate human decision.

## 5. Runbooks (design 22)

### 5.1 A runner is stuck in a transitional state

1. `GET /api/v2/runners/<runner_id>` - read `card.current_operation`,
   `card.last_error`, `card.readiness` and the audit tail.
2. `GET /api/v2/operations/<operation_id>` - attempts and trace.
3. Past its deadline, a creation is swept by the reconciler to `failed`
   with nothing left outside; a removal waiting on the forge
   (`deregistering`) is retried on every pass and finishes once the forge
   answers.
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
2. `POST /api/v2/fleets/<fleet_id>/clear-cache` clears the idle runners of a
   fleet and reports, per runner, what it freed or why it was skipped.
3. If that is not enough, lower the fleet's capacity (admin): the reconciler
   drains, deregisters and removes, the idlest first, never a busy one.
4. Never delete a volume by hand: `remove` does it after the registration.

### 5.4 Rotating an agent certificate

Issue a new one from the authority, install it beside the old, update the
pin, see a heartbeat arrive, then revoke the old (design 22.4). Certificate
expiry as a worker health field is T-1903, gate WINDOWS-INFRA, not built.

### 5.5 Emergency: stop everything without losing registrations

`POST /api/v2/fleets/<fleet_id>/capacity {"desired": 0}` as admin, per
fleet. Slower than stopping containers, and the only way that leaves no
orphaned registration. Busy runners finish their jobs first.

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
| GitHub runner | `RUNNER_VERSION` in `dockerfile` (and `scripts/start.sh`) | Bump, rebuild the image, recreate the runners one at a time |
| Forgejo runner, Linux | the Forgejo runner image | Rebuild from the new release |
| Forgejo runner, Windows | `images/windows/manifest.json` | Rebuild from the tag with `images/windows/build-forgejo-runner.md`, and add a manifest line. The running binary reports `dev`, so its provenance is unknown |
| Forgejo runner, macOS | `images/macos/manifest.json` | As Windows, with `GOOS=darwin`. Not yet measured |
| The macOS appliance | QEMU/KVM inside Hyper-V | A host or hypervisor update can break it with no vendor support (design 9.3.1). Check it after each |

## 7. Access

| Role | May |
| --- | --- |
| viewer | read everything; post nothing |
| operator | start, stop, restart, drain, cancel drain, clear cache, add runners, raise capacity |
| admin | as operator, and remove, recreate, deregister, recreate a fleet, lower capacity, manage access, set the forge tokens |

Every request to the controller is audited, accepted or refused, with the
actor and the reason, and how each operation ended: `GET /api/v2/audit`,
filtered by `runner_id`, `fleet_id`, `verb` or `decision`. The audit table
refuses updates and deletes.
