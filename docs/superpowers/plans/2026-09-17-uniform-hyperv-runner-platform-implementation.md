# Uniform Hyper-V runner platform: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> or `superpowers:executing-plans` to work this plan task by task. Steps use
> checkbox (`- [ ]`) syntax. Do not skip the gating in "Where a task may run".

**Goal:** the six provider x platform combinations of `uniform.md`, all created,
scaled and operated through one controller, one RunnerSpec, one lifecycle and
one control protocol, on Hyper-V, with WSL removed from the runner
architecture.

**Spec:** `docs/superpowers/specs/2026-09-17-uniform-hyperv-runner-platform-design.md`.
Section numbers below refer to it. Requirement IDs (FR/NFR/CON/MIG/ACC) are
defined in spec section 4 and traced in spec section 8.

**Tech stack:** Python 3.12, Flask 3.0.3, flask-sock, sqlite3, pytest, the
`docker` CLI, Hyper-V PowerShell cmdlets, GitHub REST, Forgejo REST v1.

**Status:** nothing in this plan has been implemented. No task below has been
started. This document was written together with the spec and neither has been
executed.

---

## Where a task may run

Every task carries one of these gates. A task must not be started before its
gate is satisfied, and tasks marked **NEVER-AUTO** must be performed by a human
or with an explicit, specific instruction for that run.

| Gate | Meaning |
| --- | --- |
| **LOCAL** | Runs on a developer machine with the repository and Python. No infrastructure. Safe to run unattended. |
| **HYPERV** | Needs the real Hyper-V host, elevated. Creates or configures VMs. |
| **WINDOWS-INFRA** | Needs Windows host configuration: optional features, firewall, services, certificates. |
| **MACOS-ENV** | Needs access to the macOS/QEMU environment. |
| **FORGE-LIVE** | Registers or deregisters against the live GitHub org or Forgejo instance. Consumes real tokens and creates real runner records. |
| **NEVER-AUTO** | Destroys data, stops capacity, or changes production routing. Requires an explicit human decision recorded at the time. |

A task with several gates needs all of them.

---

## Global constraints

These hold for every task and are not repeated per task.

- **The 437 tests in `dashboard/tests/` must stay green.** Run
  `cd dashboard && python -m pytest tests/ -q` before and after every task.
  A task that would require weakening an existing test must stop and report it
  as a behaviour change instead.
- **No live infrastructure change without an explicit instruction for that
  change** (CON-10, `uniform.md` 361, 411). Creating a VM, stopping a runner,
  deregistering at a forge and deleting a volume are all in this class.
- **Never abort a running job** (MIG-9). Every task that touches a runner first
  checks it is idle, through `docker_ops.idle_check()` today and through the
  controller's health view afterwards.
- **Never call a VM or QEMU guest a container** (CON-7). Names, log lines, API
  fields and UI strings all use the vocabulary of spec section 6.
- **Secrets stay out of logs, API responses and audit records** (NFR-4). Any
  new field carrying a token is added to the redaction list in the same task
  that introduces it, not later.
- **Every new `dashboard/*.py` module must be added to the `COPY` line in
  `dashboard/Dockerfile`**, or `tests/test_image_contents.py` fails.
- **Stable IDs, never names** (FR-4). No new code may key on a runner name.
  `providers.valid_name()` stays as an input allowlist because names still
  reach command lines.
- **Every schema change ships with a migration and a backfill test** (NFR-11,
  MIG-7). The `runs` table currently keys history on the container name; losing
  that mapping loses the history.

---

## Phase map

| Phase | Tasks | Gate | Delivers |
| --- | --- | --- | --- |
| 0. Preparatory abstraction | T-0001..T-0005 | LOCAL | Seams, with behaviour unchanged |
| 1. RunnerSpec and state store | T-0201..T-0204 | LOCAL | The data model and its migration |
| 2. Controller and reconciliation | T-0301..T-0308 | LOCAL | Desired state, operations, idempotency |
| 3. Control protocol and agent | T-0401..T-0406 | LOCAL | The closed verb set, mTLS, versioning |
| 4. Runtime adapters | T-0501..T-0503 | LOCAL | Docker adapter behind the new interface |
| 5. Linux Hyper-V worker | T-0601..T-0604 | HYPERV | The first non-WSL worker |
| 6. Windows worker | T-0701..T-0705 | HYPERV, WINDOWS-INFRA | Windows Server guest, runner on the OS |
| 7. macOS appliance | T-0801..T-0805 | MACOS-ENV | The appliance contract, on the existing QEMU guest |
| 8. GitHub provider across platforms | T-0901..T-0903 | FORGE-LIVE | GitHub on Linux, Windows, macOS |
| 9. Forgejo provider across platforms | T-1001..T-1003 | FORGE-LIVE | Forgejo on Linux, Windows, macOS |
| 10. Dashboard and API | T-1401..T-1407 | LOCAL | One card, one detail page, one route family |
| 11. Lifecycle actions | T-1301..T-1303 | LOCAL | The 18 verbs over the generic service |
| 12. Scaling | T-1501..T-1502 | LOCAL | Desired capacity per fleet |
| 13. Cache management | T-1601..T-1603 | LOCAL | Ownership-scoped cache clear |
| 14. Telemetry and logging | T-1801..T-1803 | LOCAL | Metrics, logs, audit |
| 15. Security hardening | T-1901..T-1903 | LOCAL, WINDOWS-INFRA | Certificates, authz, redaction |
| 16. Migration and WSL retirement | T-1701..T-1709 | NEVER-AUTO | The move, reversible at each step |
| 17. Cutover and rollback | T-2101..T-2103 | NEVER-AUTO | Production switch |
| 18. Integration and acceptance | T-2001..T-2003 | mixed | Evidence against ACC-1..19 |
| 19. Documentation and runbooks | T-2201..T-2203 | LOCAL | Operator documentation |

---

## Phase 0 — Preparatory abstraction

Behaviour must not change. Every task ends with the same 437 tests green and
the running fleet untouched.

### T-0001 — Extract a RuntimeAdapter contract

- **Gate:** LOCAL · **Requirements:** FR-20, CON-4, CON-8 · **Depends on:** nothing
- **Goal:** name the seam platform differences will hide behind, moving no logic.
- **Files:** create `dashboard/runtime/__init__.py`, `dashboard/runtime/base.py`, `dashboard/tests/test_runtime_contract.py`; modify `dashboard/Dockerfile` (COPY line).
- **Symbols:** `RuntimeAdapter` with exactly `create`, `start`, `stop`, `remove`, `status`, `telemetry`, `logs`, `exec_probe`, `clear_cache`, `capabilities`. `ExecUnitRef(kind, handle)`. `Probe` enum: `DISK_USAGE`, `JOB_STATE`, `AGENT_VERSION`, `CACHE_SIZE`.
- **Steps:** (1) abstract definitions and dataclasses only; (2) `exec_probe` takes a `Probe` member, never a string, which is the type-level form of NFR-10; (3) add to the Dockerfile COPY line.
- **Tests:** the adapter defines exactly those ten methods; `Probe` has no free-text member; `base.py` imports nothing from `docker_ops` or `providers`, asserted by parsing the AST.
- **Verify:** `cd dashboard && python -m pytest tests/test_runtime_contract.py tests/test_image_contents.py -q`
- **Expected:** new tests pass, 437 unchanged.
- **Rollback:** delete the directory and the COPY entry; nothing references it.
- **Security:** the closed enum is the first enforcement of "no arbitrary command endpoint"; reject any later free-text member.
- **Done when:** the contract exists, is unused, and import isolation passes.

### T-0002 — Move the Docker calls behind DockerRuntimeAdapter

- **Gate:** LOCAL · **Requirements:** FR-20, CON-8 · **Depends on:** T-0001
- **Goal:** today's behaviour reachable through the new contract, still called from the old paths.
- **Files:** create `dashboard/runtime/docker_adapter.py`, `dashboard/tests/test_docker_adapter.py`; modify `dashboard/docker_ops.py`, `dashboard/Dockerfile`.
- **Symbols:** `DockerRuntimeAdapter` absorbing the bodies of `docker_ops.start`, `stop`, `restart`, `remove`, `create`, `logs_since`, `prune`, `_inner_df`, `_stats_map`, `_cpu_caps_map`.
- **Steps:** (1) copy each body unchanged, keeping every comment — they carry the reasons for the 180s removal timeout, the `-v` semantics and the `keep_data` split; (2) leave a delegation behind each old name with its signature intact; (3) do not move callers in this task.
- **Tests:** drive the adapter with a stubbed `_docker` and assert the same argv the existing tests assert.
- **Verify:** `cd dashboard && python -m pytest tests/ -q`
- **Expected:** 437 plus new, with no existing test edited.
- **Rollback:** revert the delegation commit; the adapter is inert without it.
- **Security:** add a test asserting no other `dashboard/*.py` builds a `docker` argv.
- **Done when:** `docker_ops` makes no direct lifecycle call to the docker CLI.

### T-0003 — Give Provider a platform axis

- **Gate:** LOCAL · **Requirements:** FR-19, FR-20, CON-3 · **Depends on:** nothing
- **Files:** modify `dashboard/providers.py`, `dashboard/tests/test_providers.py`.
- **Symbols:** `supports(platform, arch)`, `agent_artifact(platform, arch)`, `registration(spec)`, `deregistration(spec)`, `job_state(spec, forge_status)`.
- **Steps:** (1) current behaviour for `linux`; (2) `GITHUB.supports("windows","x64")` and `("macos","arm64")` True; `FORGEJO.supports` for those two True only when the self-built artefact is configured, else False, so an unbuildable fleet is visibly unavailable; (3) no runtime imports.
- **Tests:** one assertion per cell of spec 9.5; plus import isolation.
- **Verify:** `cd dashboard && python -m pytest tests/test_providers.py -q`
- **Rollback:** revert the file; the methods are additive.
- **Security:** `RegistrationPlan` carries a token — add its field name to the redaction list in the same commit.
- **Done when:** every cell is answered by data, not by a conditional at a call site.

### T-0004 — Version the existing API

- **Gate:** LOCAL · **Requirements:** FR-11, NFR-9, MIG-6 · **Depends on:** nothing
- **Files:** modify `dashboard/app.py`; create `dashboard/tests/test_api_versioning.py`.
- **Steps:** (1) register every existing `/api/...` route a second time under `/api/v1/...`, same handler; (2) add `GET /api/version`; (3) the websocket payload gains a `schema` field.
- **Tests:** each alias returns what the unprefixed route returns; an unauthenticated call to an alias is rejected exactly as the unprefixed one is.
- **Verify:** `cd dashboard && python -m pytest tests/test_api_versioning.py tests/test_routes.py -q`
- **Rollback:** remove the alias registrations.
- **Done when:** both prefixes serve and `/api/version` reports them.

### T-0005 — Make the WSL couplings executable assertions

- **Gate:** LOCAL · **Requirements:** CON-2, ACC-5 · **Depends on:** nothing
- **Files:** create `dashboard/tests/test_wsl_couplings.py`.
- **Steps:** one xfail test per row of spec 2.5, each asserting the coupling's absence, reason "still WSL-coupled; unmarked by T-1708".
- **Verify:** `cd dashboard && python -m pytest tests/test_wsl_couplings.py -q -rx`
- **Expected:** all xfail today; the migration flips them one by one.
- **Done when:** every row of spec 2.5 has exactly one assertion.

---

## Phase 1 — RunnerSpec and state store

### T-0201 — The schema

- **Gate:** LOCAL · **Requirements:** FR-3, FR-4 · **Depends on:** nothing
- **Files:** create `dashboard/store/__init__.py`, `dashboard/store/schema.py`, `dashboard/store/specs.py`, `dashboard/tests/test_spec_store.py`; modify `dashboard/Dockerfile`.
- **Symbols:** tables `runner_specs`, `workers`, `fleets`, `operations`, `audit` exactly as spec 11.1 and 11.3. `SpecStore.create/get/list/update/soft_delete`, all taking `runner_id`.
- **Steps:** (1) `CREATE TABLE IF NOT EXISTS` in the style of `history.py`, WAL, foreign keys on; (2) `runner_id` is a UUIDv4 string generated by the store, never by a caller; (3) `spec_version` increments on every update and an update carrying a stale version is refused.
- **Tests:** round-trip; refusal on a stale `spec_version`; `runner_id` uniqueness; soft delete keeps the row readable.
- **Verify:** `cd dashboard && python -m pytest tests/test_spec_store.py tests/test_image_contents.py -q`
- **Rollback:** the module is unused until T-0301; delete it.
- **Security:** no column holds a token; the registration token lives only in the operation trace, redacted.
- **Done when:** every field of spec 11.1 exists with the stated type.

### T-0202 — Fleets and desired capacity

- **Gate:** LOCAL · **Requirements:** FR-15, FR-5 · **Depends on:** T-0201
- **Files:** modify `dashboard/store/specs.py`; create `dashboard/tests/test_fleets.py`.
- **Steps:** (1) seed the six fleets of spec 14.4 idempotently; (2) `set_capacity(fleet_id, n)` writes desired capacity and returns an operation id; (3) a fleet for an unsupported cell is seeded with capacity 0 and `available=False` from `provider.supports()`.
- **Tests:** seeding twice changes nothing; capacity is per fleet; an unsupported fleet cannot be given capacity.
- **Verify:** `cd dashboard && python -m pytest tests/test_fleets.py -q`
- **Done when:** six rows exist and each reports availability from provider data.

### T-0203 — Per-instance storage naming

- **Gate:** LOCAL · **Requirements:** FR-18, CON-5 · **Depends on:** T-0201
- **Files:** create `dashboard/store/storage.py`, `dashboard/tests/test_storage_naming.py`.
- **Symbols:** `storage.names(runner_id) -> {work, docker, cache, reg, logs}`.
- **Steps:** (1) every name derives from `runner_id` alone; (2) a test asserts two different ids never collide and that no name is derivable from a display name.
- **Tests:** determinism, collision-freedom, and that no path escapes the per-runner prefix.
- **Verify:** `cd dashboard && python -m pytest tests/test_storage_naming.py -q`
- **Security:** this is the control that makes `clear_cache` provably scoped (FR-16).
- **Done when:** the five areas of spec 15.1 have generated names.

### T-0204 — History backfill

- **Gate:** LOCAL · **Requirements:** NFR-11, MIG-7 · **Depends on:** T-0201
- **Files:** modify `dashboard/history.py`; create `dashboard/tests/test_history_backfill.py`.
- **Steps:** (1) add nullable `runs.runner_id` and an index; (2) for each distinct `runs.runner`, create a `runner_specs` row in state `absent` with `deleted_at` set and `display_name` = the old name; (3) update `runs.runner_id`; (4) drop no column and delete no row; (5) idempotent — a second run changes nothing.
- **Tests:** row count identical before and after; every `runs` row resolves to a spec; running twice is a no-op; a fresh database migrates cleanly.
- **Verify:** `cd dashboard && python -m pytest tests/test_history_backfill.py tests/test_forgejo_history.py tests/test_history_provider.py -q`
- **Expected:** existing history tests unchanged and green.
- **Rollback:** the added column is nullable and unused by old code; leaving it is harmless.
- **Done when:** ACC-15 can be evidenced by a row count.

---

## Phase 2 — Controller and reconciliation

### T-0301 — RunnerService and the worker inventory

- **Gate:** LOCAL · **Requirements:** FR-5, FR-8 · **Depends on:** T-0201, T-0002, T-0003
- **Files:** create `dashboard/control/__init__.py`, `dashboard/control/service.py`, `dashboard/control/inventory.py`, `dashboard/tests/test_runner_service.py`.
- **Symbols:** `RunnerService.plan(fleet_id, n)`, `.set_desired(runner_id, state)`, `.act(runner_id, verb, idempotency_key)`; `Inventory.register_worker/list/health`.
- **Steps:** (1) the service writes intent and returns an operation id, never performing work in the call; (2) adapters are selected by table lookup on `(provider, platform)`; (3) a plan that `provider.supports()` refuses fails before anything is created.
- **Tests:** every call returns an operation id; an unsupported combination is refused at plan time with a reason; adapter selection is data-driven — asserted by monkeypatching the table.
- **Verify:** `cd dashboard && python -m pytest tests/test_runner_service.py -q`
- **Done when:** no method performs remote work synchronously.

### T-0302 — The reconciler

- **Gate:** LOCAL · **Requirements:** FR-5 · **Depends on:** T-0301
- **Files:** create `dashboard/control/reconciler.py`, `dashboard/tests/test_reconciler.py`.
- **Steps:** (1) compute the difference between `fleets.desired_capacity` and healthy instances; (2) drive one step per pass, so a pass is short and re-entrant; (3) it is the only writer of `actual_state`; (4) never exceed desired capacity, counting instances in transitional states.
- **Tests:** converges up and down; never overshoots; a second concurrent pass does nothing; a `degraded` worker receives no destructive verb.
- **Verify:** `cd dashboard && python -m pytest tests/test_reconciler.py -q`
- **Done when:** repeated passes over a converged fleet perform no work.

### T-0303 — The lifecycle state machine

- **Gate:** LOCAL · **Requirements:** FR-6 · **Depends on:** T-0301
- **Files:** create `dashboard/control/states.py`, `dashboard/tests/test_state_machine.py`.
- **Steps:** encode spec 12.2 as an explicit transition table; illegal transitions raise rather than being silently ignored.
- **Tests:** every edge of the diagram; every non-edge rejected; the eighteen verbs of `uniform.md` 162-181 each map to a defined edge or a read.
- **Verify:** `cd dashboard && python -m pytest tests/test_state_machine.py -q`
- **Done when:** the table and the diagram in spec 12.2 agree, asserted by a test that parses the diagram.

### T-0304 — The eighteen verbs over one service

- **Gate:** LOCAL · **Requirements:** FR-6, FR-13 · **Depends on:** T-0303
- **Files:** modify `dashboard/control/service.py`; create `dashboard/tests/test_lifecycle_verbs.py`.
- **Steps:** one method per verb, each a state-machine transition plus an operation; `restart` is stop then start; `recreate` is remove with `keep_data=True` then create.
- **Tests:** each verb from each legal state; each verb refused from each illegal state with a reason.
- **Verify:** `cd dashboard && python -m pytest tests/test_lifecycle_verbs.py -q`
- **Done when:** no verb is implemented twice for different platforms.

### T-0305 — One provisioning flow

- **Gate:** LOCAL · **Requirements:** FR-7 · **Depends on:** T-0302, T-0304
- **Files:** create `dashboard/control/provision.py`, `dashboard/tests/test_provisioning_flow.py`.
- **Steps:** implement the nine steps of spec 12.4 once, parameterised only by the two adapters.
- **Tests:** the step order is asserted; injecting a failure at each step runs exactly the compensations of spec 12.5 in reverse.
- **Verify:** `cd dashboard && python -m pytest tests/test_provisioning_flow.py -q`
- **Security:** the registration token never enters a log, an operation result or an audit row — asserted with a sentinel value.
- **Done when:** one code path serves all six cells.

### T-0306 — Operations and idempotency

- **Gate:** LOCAL · **Requirements:** FR-11, NFR-5 · **Depends on:** T-0301
- **Files:** create `dashboard/control/operations.py`, `dashboard/tests/test_operations.py`.
- **Steps:** unique `idempotency_key`; a repeat returns the original operation; every operation carries a deadline and an attempt log.
- **Tests:** duplicate key returns the first operation and performs no work; an operation past its deadline is re-driven; the trace records each attempt.
- **Verify:** `cd dashboard && python -m pytest tests/test_operations.py -q`
- **Done when:** replaying any mutating call is provably safe.

### T-0307 — Timeouts and bounded retries

- **Gate:** LOCAL · **Requirements:** NFR-6 · **Depends on:** T-0306
- **Files:** create `dashboard/control/retry.py`; modify `dashboard/control/*`; create `dashboard/tests/test_timeouts.py`.
- **Steps:** encode the table in spec 17.2; no call without a deadline; a failed forge lookup caches "unknown", never the last good answer.
- **Tests:** each row of the table; a slow call is cut at its deadline; a failure is not cached as success.
- **Verify:** `cd dashboard && python -m pytest tests/test_timeouts.py -q`
- **Done when:** no remote call in `dashboard/control/` lacks a timeout, asserted by an AST test.

### T-0308 — No half instances

- **Gate:** LOCAL · **Requirements:** NFR-8, ACC-14 · **Depends on:** T-0305
- **Files:** modify `dashboard/control/provision.py`; create `dashboard/tests/test_partial_failure.py`.
- **Steps:** every step that creates external state writes its intent first; the sweeper re-drives or compensates anything past its deadline.
- **Tests:** crash after create-before-record, after record-before-register, and after register-before-confirm; each converges to either a healthy instance or no instance and no forge record.
- **Verify:** `cd dashboard && python -m pytest tests/test_partial_failure.py -q`
- **Done when:** the three crash points leave nothing behind.
---

## Phase 3 — Control protocol and agent

### T-0401 — The agent, with a closed verb set

- **Gate:** LOCAL · **Requirements:** FR-9, NFR-10, CON-9 · **Depends on:** T-0001
- **Files:** create `agent/__init__.py`, `agent/server.py`, `agent/verbs.py`, `agent/tests/test_verbs.py`.
- **Symbols:** one dispatch table mapping verb name to handler. `agent.verbs.VERBS` is a frozen mapping; a verb not in it is a 404 before any parsing.
- **Steps:** (1) implement the table of spec 13.1 exactly; (2) no handler takes a command, a path outside the per-runner tree, or a shell string; (3) `exec_probe` accepts only a `Probe` member.
- **Tests:** an unknown verb is refused; every handler signature is asserted; a test enumerates the source for `subprocess`, `os.system`, `Invoke-Expression` and fails on any call whose arguments are not a literal list.
- **Verify:** `cd agent && python -m pytest tests/ -q`
- **Security:** this is NFR-10. The test above is the enforcement, not the intention.
- **Done when:** the agent cannot be asked to run anything that is not a named verb.

### T-0402 — Mutual TLS

- **Gate:** LOCAL · **Requirements:** NFR-1, NFR-2 · **Depends on:** T-0401
- **Files:** create `agent/tls.py`, `dashboard/control/agent_client.py`, `dashboard/tests/test_mtls.py`.
- **Steps:** (1) a private CA in the control plane; (2) the agent presents a client certificate whose subject is its `host_id`; (3) the controller pins the fingerprint from `workers`; (4) a mismatch is refused and recorded.
- **Tests:** a wrong CA, a wrong subject and a changed fingerprint are each refused with a distinct reason; plain HTTP is refused.
- **Verify:** `cd dashboard && python -m pytest tests/test_mtls.py -q`
- **Rollback:** none; do not ship the agent without this.
- **Done when:** no code path reaches an agent without a verified certificate.

### T-0403 — Per-verb authorization

- **Gate:** LOCAL · **Requirements:** NFR-3 · **Depends on:** T-0402
- **Files:** modify `dashboard/control/agent_client.py`; create `dashboard/tests/test_verb_authz.py`.
- **Steps:** `workers.capabilities` lists permitted verbs; the controller refuses anything else before dispatch; the agent refuses again on receipt.
- **Tests:** a verb absent from capabilities is refused at both ends; the refusal is audited.
- **Verify:** `cd dashboard && python -m pytest tests/test_verb_authz.py -q`
- **Done when:** authorization is checked twice and both are tested.

### T-0404 — Heartbeat, health and capabilities

- **Gate:** LOCAL · **Requirements:** FR-10 · **Depends on:** T-0402
- **Files:** modify `agent/server.py`, `dashboard/control/inventory.py`; create `dashboard/tests/test_heartbeat.py`.
- **Steps:** 10 s heartbeat carrying agent version, capabilities, per-instance actual state and counters; three missed beats mark the worker `degraded`; health is tri-state and `unknown` is never collapsed.
- **Tests:** missed beats degrade; a degraded worker receives no destructive verb; `unknown` survives a round trip.
- **Verify:** `cd dashboard && python -m pytest tests/test_heartbeat.py -q`
- **Done when:** `workers.last_seen_at` and `runner_specs.last_seen_at` are driven by beats.

### T-0405 — Async operations end to end

- **Gate:** LOCAL · **Requirements:** FR-11 · **Depends on:** T-0306, T-0401
- **Files:** modify `agent/server.py`, `dashboard/control/operations.py`; create `dashboard/tests/test_async_ops.py`.
- **Steps:** the agent returns 202 with a local handle and reports progress by `event`; no long work inside a request.
- **Tests:** a slow verb returns 202 immediately; progress events update the operation; a lost reply is recovered by the idempotency key.
- **Verify:** `cd dashboard && python -m pytest tests/test_async_ops.py -q`
- **Done when:** no agent call blocks longer than its fast timeout.

### T-0406 — Protocol versioning

- **Gate:** LOCAL · **Requirements:** NFR-9 · **Depends on:** T-0401
- **Files:** modify `agent/server.py`, `dashboard/control/agent_client.py`; create `dashboard/tests/test_protocol_version.py`.
- **Steps:** major-only `X-Protocol-Version`; an unimplemented major marks the worker `degraded` with a reason rather than guessing.
- **Tests:** older and newer majors both degrade rather than fail obscurely.
- **Verify:** `cd dashboard && python -m pytest tests/test_protocol_version.py -q`
- **Done when:** a version mismatch is visible in the dashboard as a reason.

---

## Phase 4 — Runtime adapters behind the contract

### T-0501 — LinuxContainerRuntime

- **Gate:** LOCAL · **Requirements:** FR-20 · **Depends on:** T-0002, T-0203
- **Files:** create `agent/runtimes/linux_container.py`, `agent/tests/test_linux_runtime.py`.
- **Steps:** implement the contract using the argv from T-0002; create the five per-runner volumes of spec 15.1; report `capabilities` with `job_containers=True`.
- **Tests:** contract suite with a stubbed docker; volume names come from `storage.names`; `remove(keep_data=True)` keeps them and `False` removes them.
- **Verify:** `cd agent && python -m pytest tests/test_linux_runtime.py -q`
- **Done when:** the contract suite passes against it with a fake backend.

### T-0502 — The contract suite itself

- **Gate:** LOCAL · **Requirements:** FR-20, ACC-6 · **Depends on:** T-0501
- **Files:** create `agent/tests/contract/__init__.py`, `agent/tests/contract/suite.py`.
- **Steps:** write the nine scenarios of spec 19.2 as a parameterised suite, **before** the Windows and macOS adapters exist, so they are written to the contract rather than to an implementation (risk R-6).
- **Tests:** the suite is the test; it runs against `LinuxContainerRuntime` with a fake backend now.
- **Verify:** `cd agent && python -m pytest tests/contract -q`
- **Done when:** the suite is runnable against any adapter by parameter.

### T-0503 — Capability honesty

- **Gate:** LOCAL · **Requirements:** FR-20, CON-7 · **Depends on:** T-0502
- **Files:** modify `agent/tests/contract/suite.py`.
- **Steps:** each scenario a runtime declares unsupported must be declared false in `capabilities()`; the suite asserts the correspondence in both directions.
- **Verify:** `cd agent && python -m pytest tests/contract -q`
- **Done when:** a runtime cannot silently skip a scenario.

---

## Phase 5 — Linux Hyper-V worker

### T-0601 — Build the Linux worker VM

- **Gate:** HYPERV · **Requirements:** CON-1, FR-18 · **Depends on:** T-0501
- **Files:** create `infra/hyperv/New-LinuxWorker.ps1`, `infra/hyperv/README.md`.
- **Steps:** (1) create a Gen2 VM on a new VHDX on `D:`; (2) static memory per OPEN-5; (3) install Ubuntu, Docker CE via `scripts/install-docker.sh` (which already sets `live-restore`); (4) install the agent; (5) enrol it in `workers`.
- **Verify, read-only first:** `Get-VM`, `Get-VMProcessor`, `Get-VMMemory`; then in guest `docker info --format '{{.LiveRestoreEnabled}} {{.Driver}}'`.
- **Expected:** the worker appears `healthy` in the dashboard with capabilities.
- **Rollback:** stop and delete the VM; nothing else has changed.
- **Security:** the agent listens only on the management address, firewalled to the control plane.
- **Never-auto:** creating the VM is a deliberate act; do not script it into CI.
- **Done when:** the controller can create and remove a throwaway Linux instance on it.

### T-0602 — Control-plane VM

- **Gate:** HYPERV · **Requirements:** FR-8 · **Depends on:** T-0601
- **Files:** create `infra/hyperv/New-ControlPlane.ps1`.
- **Steps:** a second Gen2 VM running the controller, the dashboard and the state store on its own disk, for the reasons in spec 10.2.
- **Verify:** the dashboard answers on the control-plane address; the state store is on its own volume.
- **Rollback:** delete the VM; the WSL dashboard is still serving.
- **Done when:** the control plane no longer shares a failure domain with a worker.

### T-0603 — Move the state store

- **Gate:** HYPERV, NEVER-AUTO · **Requirements:** NFR-11 · **Depends on:** T-0602, T-0204
- **Steps:** stop the WSL dashboard, copy `dashboard-data` (history.db, users.json, secret.key, state.json), start the new one, verify row counts, keep the old volume untouched as the rollback.
- **Verify:** `SELECT COUNT(*) FROM runs` identical; a user can sign in without re-approval.
- **Rollback:** start the WSL dashboard again; its volume was never modified.
- **Done when:** ACC-15 is evidenced.

### T-0604 — Networking and publication

- **Gate:** HYPERV, WINDOWS-INFRA · **Requirements:** CON-2 · **Depends on:** T-0602 · **Decision:** OPEN-6
- **Steps:** give the control plane a stable address; if OPEN-6 chooses an External switch, create it during a maintenance window because creating one briefly interrupts host networking; otherwise keep an Internal switch with a static address and one portproxy rule.
- **Verify:** the dashboard is reachable from the LAN and from the reverse proxy; `DASH_PUBLIC_URL` unchanged so OIDC redirects keep working.
- **Rollback:** restore the previous portproxy rule.
- **Done when:** the address no longer changes across a reboot, which is what removed the keepalive rewrite.

---

## Phase 6 — Windows worker

### T-0701 — Build the Windows worker VM

- **Gate:** HYPERV, WINDOWS-INFRA · **Requirements:** FR-1, CON-1, CON-6 · **Depends on:** T-0301 · **Decisions:** OPEN-2, OPEN-3
- **Files:** create `infra/hyperv/New-WindowsWorker.ps1`.
- **Steps:** (1) Gen2 VM, Windows Server per OPEN-2; (2) **no** container engine unless OPEN-3 chooses containers — spec 9.2 explains why; (3) install the agent as a Windows service; (4) create one local account per runner instance with an ACL-scoped directory tree.
- **Verify:** `Get-VM`, then in guest `Get-Service`, and the worker `healthy` in the dashboard.
- **Rollback:** delete the VM.
- **Security:** each runner account is a standard user with no logon rights beyond the service; the ACL test in T-0703 is the control.
- **Done when:** the worker reports capabilities with `job_containers=False`.

### T-0702 — WindowsProcessRuntime

- **Gate:** LOCAL to write, WINDOWS-INFRA to run · **Requirements:** FR-20 · **Depends on:** T-0502, T-0701
- **Files:** create `agent/runtimes/windows_process.py`, `agent/tests/test_windows_runtime.py`.
- **Steps:** implement the contract with per-instance account, directory tree, service and Job Object for CPU and memory.
- **Tests:** the contract suite against a fake backend locally; the conformance run on the real worker.
- **Verify:** `cd agent && python -m pytest tests/test_windows_runtime.py -q` locally; `pytest agent/tests/contract --runtime=windows` on the worker.
- **Done when:** the suite passes on the real worker, or the failing scenarios are declared false in capabilities.

### T-0703 — Windows isolation proof

- **Gate:** WINDOWS-INFRA · **Requirements:** FR-18, CON-5 · **Depends on:** T-0702
- **Steps:** two instances on one worker; assert each cannot read the other's workspace, cache or registration directory, and that a Job Object cap is enforced.
- **Verify:** an explicit cross-read attempt returns access denied; a memory hog is capped rather than taking the worker down.
- **Done when:** CON-5 is evidenced on Windows by test, not by assertion.

### T-0704 — GitHub Windows runner image and bootstrap

- **Gate:** WINDOWS-INFRA, FORGE-LIVE · **Requirements:** FR-1, MIG-5 · **Depends on:** T-0702
- **Files:** create `images/windows/bootstrap.ps1`, `images/windows/README.md`.
- **Steps:** download the pinned runner, verify its hash, configure with a token from the provider adapter, install as the per-instance service.
- **Verify:** the runner appears online in the GitHub org and completes a trivial workflow.
- **Rollback:** deregister through the dashboard, then remove.
- **Security:** the token arrives in the verb payload and is never written to disk.
- **Done when:** ACC-2 is evidenced for Windows.

### T-0705 — Forgejo Windows artefact

- **Gate:** LOCAL to build, FORGE-LIVE to register · **Requirements:** FR-1 · **Depends on:** T-0702 · **Decision:** OPEN-4
- **Files:** create `images/windows/build-forgejo-runner.md`.
- **Steps:** document and script a reproducible `GOOS=windows` build from `code.forgejo.org/forgejo/runner` at a pinned tag; record the commit, the toolchain and the hash in the image manifest; register with the `host` executor label.
- **Verify:** the runner appears in Forgejo and completes a trivial workflow.
- **Risk:** R-4 — there is no upstream release feed for this artefact; add it to the version-deprecation runbook.
- **Done when:** ACC-3 is evidenced for Windows, or OPEN-4 declines and the cell is marked unavailable with the reason.

---

## Phase 7 — macOS appliance

### T-0801 — The appliance contract

- **Gate:** LOCAL · **Requirements:** CON-7, FR-20 · **Depends on:** T-0502
- **Files:** create `agent/runtimes/macos_appliance.py`, `agent/tests/test_macos_runtime.py`.
- **Steps:** implement the contract against a fake backend; the five points of spec 10.6; the word "container" appears nowhere in the module.
- **Tests:** the contract suite; a lint test asserting the vocabulary.
- **Verify:** `cd agent && python -m pytest tests/test_macos_runtime.py -q`
- **Done when:** the suite passes with a fake backend, before any hardware exists.

### T-0802 — Adopt the running macOS runner, without touching its registration

- **Gate:** MACOS-ENV, NEVER-AUTO · **Requirements:** MIG-4, ACC-12 · **Depends on:** T-0801
- **Goal:** the Forgejo macOS runner that is online today becomes an ordinary managed runner, with no re-registration and no interrupted job.
- **Steps:** (1) install the control agent inside the macOS guest, as a launchd service beside the runner; (2) register the appliance as an execution unit of the existing `linux-worker` — no new worker kind; (3) write a RunnerSpec whose `display_name` is the runner's current name and whose `registration_id` and `registration_uuid` are **read from the forge**, so the record is adopted rather than recreated; (4) confirm the controller reports it `idle`.
- **Tests:** an adoption test asserting no registration call is made and the forge record is unchanged before and after.
- **Verify:** the card renders with the full action set; `GET /api/v2/runners/{id}` returns the spec; the forge still lists the same `uuid`.
- **Expected:** the runner never leaves `idle` and takes jobs throughout.
- **Rollback:** uninstall the agent; nothing about the runner or its registration changed.
- **Security:** the agent runs as the runner's own user, not root; its certificate is scoped to this appliance.
- **Done when:** MIG-4 holds and the "Elsewhere" section can be deleted (T-1405).

### T-0803 — MacApplianceRuntime against the real appliance

- **Gate:** MACOS-ENV · **Requirements:** FR-20, ACC-11 · **Depends on:** T-0802
- **Goal:** the contract of T-0801 verified against the QEMU guest rather than a fake.
- **Steps:** (1) implement `create`, `start`, `stop`, `remove`, `status`, `telemetry`, `logs`, `exec_probe`, `clear_cache`, `capabilities` over the appliance; (2) `start` clears a leftover `/var/tmp/opencore-image-ng.sh-*` before boot, which is the recorded cause of the restart loop after any hard stop; (3) `telemetry` reports the VM's root-disk usage, which is the recorded cause of "offline runner, container Up, QEMU idle"; (4) `capabilities` declares `job_containers=false`.
- **Tests:** `pytest agent/tests/contract --runtime=macos` on the real appliance.
- **Verify:** every scenario of spec 19.2 passes, or the corresponding capability is declared false and the suite asserts the correspondence.
- **Rollback:** the adapter is only reached for this appliance; disabling it returns the runner to T-0802 behaviour.
- **Done when:** the same suite that passes for Linux passes here, unmodified.

### T-0804 — GitHub macOS instance

- **Gate:** MACOS-ENV, FORGE-LIVE · **Requirements:** FR-1, MIG-5 · **Depends on:** T-0803
- **Goal:** the sixth cell, on the same appliance.
- **Steps:** (1) create a second runner instance through the generic flow; (2) the GitHub runner installs as a launchd service inside the guest with its own per-instance directories per spec 15.1; (3) labels and runner group come from the fleet.
- **Verify:** the runner appears online in the GitHub org; a trivial workflow with `runs-on: [self-hosted, macos]` succeeds.
- **Expected:** the existing Forgejo instance is unaffected throughout — asserted by watching its state during the run.
- **Rollback:** remove through the dashboard, which deregisters first.
- **Security:** the registration token arrives in the verb payload and is never written to the guest's disk.
- **Done when:** ACC-2 is evidenced for macOS.

### T-0805 — Formalise the self-built Forgejo darwin artefact

- **Gate:** LOCAL to build, MACOS-ENV to install · **Requirements:** FR-1, R-4 · **Depends on:** T-0803
- **Goal:** the artefact that is already running becomes reproducible and traceable instead of incidental.
- **Files:** create `images/macos/build-forgejo-runner.md`, `images/macos/manifest.json`.
- **Steps:** (1) document the exact `GOOS=darwin` build from `code.forgejo.org/forgejo/runner` at a pinned tag, with the toolchain version; (2) record the commit, tag and SHA-256 in the manifest; (3) point `RunnerSpec.runtime_template` at that manifest entry; (4) add the artefact to the version-deprecation runbook, because there is no upstream release feed to watch for darwin.
- **Verify:** rebuilding from the documented steps reproduces the recorded hash.
- **Expected:** the running runner's binary matches the manifest, or the difference is recorded.
- **Done when:** ACC-3 is evidenced for macOS and R-4 has a named owner.

---

## Phase 8 and 9 — Provider adapters across platforms

### T-0901 — GitHub adapter, three platforms

- **Gate:** LOCAL to write, FORGE-LIVE to verify · **Requirements:** FR-19, CON-3 · **Depends on:** T-0003
- **Files:** modify `dashboard/github_api.py`, `dashboard/providers.py`; create `dashboard/tests/test_github_platforms.py`.
- **Steps:** registration, deregistration, status, tokens, labels and job info parameterised by platform; no runtime import.
- **Tests:** the six methods per platform against a recorded API; import isolation.
- **Verify:** `cd dashboard && python -m pytest tests/test_github_platforms.py -q`
- **Done when:** the adapter has no platform conditional beyond label defaults.

### T-0902 — GitHub runner groups and labels

- **Gate:** LOCAL · **Requirements:** FR-19 · **Depends on:** T-0901
- **Steps:** carry `runner_group` and `labels` from the fleet to registration; verify equality after registration and record a mismatch as `last_error`.
- **Verify:** `cd dashboard && python -m pytest tests/test_github_platforms.py -q`
- **Done when:** a label drift is visible rather than silent.

### T-0903 — GitHub deregistration ordering

- **Gate:** LOCAL · **Requirements:** NFR-8, MIG-9 · **Depends on:** T-0901
- **Steps:** deregister strictly before removing the execution unit; a failure blocks removal and surfaces, because the reverse order is what strands a registration.
- **Verify:** `cd dashboard && python -m pytest tests/test_partial_failure.py -q`
- **Done when:** no path removes a unit before its forge record.

### T-1001 — Forgejo adapter, three platforms

- **Gate:** LOCAL to write, FORGE-LIVE to verify · **Requirements:** FR-19, CON-3 · **Depends on:** T-0003
- **Files:** modify `dashboard/forgejo_api.py`, `dashboard/providers.py`; create `dashboard/tests/test_forgejo_platforms.py`.
- **Steps:** as T-0901; `supports()` returns False for a platform whose self-built artefact is not configured.
- **Done when:** an unavailable cell is data, not a crash.

### T-1002 — Forgejo deregistration, which the runner cannot do itself

- **Gate:** LOCAL · **Requirements:** MIG-9 · **Depends on:** T-1001
- **Steps:** **MEASURED**: `forgejo-runner` has no `unregister` subcommand, so only the API path deletes a record. Make that the only removal path and refuse a removal that cannot reach the forge, recording why.
- **Tests:** removal with the forge unreachable is refused, not forced.
- **Done when:** no Forgejo runner can be removed in a way that strands its record.

### T-1003 — Forgejo label syntax per executor

- **Gate:** LOCAL · **Requirements:** FR-19 · **Depends on:** T-1001
- **Steps:** encode `<name>:<type>://<image>` with types `docker`, `lxc`, `host`; Windows and macOS instances use `host`.
- **Tests:** label round-trip per platform; an empty label set is refused, as today.
- **Done when:** the label a fleet declares is the label the runner registers.
---

## Phase 10 — Dashboard and API v2

### T-1401 — One runner card

- **Gate:** LOCAL · **Requirements:** FR-12, CON-4, CON-8 · **Depends on:** T-0301
- **Files:** modify `dashboard/templates/index.html`; create `dashboard/tests/test_generic_card.py`.
- **Steps:** one card component fed by the payload of spec 14.1; platform differences render from `capabilities`, never from a template conditional.
- **Tests:** a grep test asserting no `platform ===` or `provider ===` conditional in the template; every field of spec 14.1 is rendered; a capability set to false renders its annotation.
- **Verify:** `cd dashboard && python -m pytest tests/test_generic_card.py -q`
- **Rollback:** the old markup is in git; the v1 UI still works.
- **Done when:** one component renders all six cells.

### T-1402 — One action set

- **Gate:** LOCAL · **Requirements:** FR-13 · **Depends on:** T-0304, T-1401
- **Files:** modify `dashboard/app.py`, `dashboard/templates/index.html`; create `dashboard/tests/test_generic_actions.py`.
- **Steps:** `POST /api/v2/runners/{runner_id}/actions/{verb}` for all eight; a verb absent from capabilities renders disabled with the reason.
- **Tests:** every verb for every platform reaches the same handler; an unsupported verb is refused with a reason, not a 500.
- **Verify:** `cd dashboard && python -m pytest tests/test_generic_actions.py -q`
- **Done when:** no per-platform route exists.

### T-1403 — Fleet routes

- **Gate:** LOCAL · **Requirements:** FR-14 · **Depends on:** T-0202
- **Files:** modify `dashboard/app.py`; create `dashboard/tests/test_fleet_routes.py`.
- **Steps:** capacity, recreate, clear-cache and add-runner per fleet; scale up and down are the same capacity call.
- **Verify:** `cd dashboard && python -m pytest tests/test_fleet_routes.py -q`
- **Done when:** the six fleets are operable from the page.

### T-1404 — Six fleets in the UI

- **Gate:** LOCAL · **Requirements:** FR-15 · **Depends on:** T-1403
- **Steps:** render fleets from the table; an unavailable fleet shows its reason from `provider.supports()`.
- **Done when:** adding a seventh fleet needs no code change.

### T-1405 — Delete the platform conditionals and Elsewhere

- **Gate:** LOCAL · **Requirements:** CON-8, ACC-12 · **Depends on:** T-1401, T-0802
- **Files:** modify `dashboard/templates/index.html`; delete `dashboard/external_telemetry.py`, `dashboard/tests/test_elsewhere_section.py`, `dashboard/tests/test_elsewhere_markup.py`, `dashboard/tests/test_external_telemetry.py`; modify `dashboard/docker_ops.py` (drop `_elsewhere`), `dashboard/Dockerfile`.
- **Steps:** remove `grid-elsewhere`, `makeElseCard`, `elseCards` and the notice; replace the hard-coded `ext4.vhdx` string with the worker's reported storage.
- **Tests:** a grep test asserting the strings are gone; the WSL-coupling test for the vhdx path flips from xfail to pass.
- **Verify:** `cd dashboard && python -m pytest tests/ -q`
- **Rollback:** revert; these are deletions, so the revert is exact.
- **Done when:** ACC-12 holds.

### T-1406 — Runner detail page

- **Gate:** LOCAL · **Requirements:** FR-12 · **Depends on:** T-1401
- **Files:** modify `dashboard/templates/runner.html`, `dashboard/runner_detail.py`.
- **Steps:** key on `runner_id`; read from the controller, not from `docker inspect`; render `capabilities`, `current_operation`, `last_error` and the audit tail.
- **Done when:** the page has no Docker-specific field.

### T-1407 — Retire v1 and the Docker-socket path

- **Gate:** LOCAL, NEVER-AUTO to deploy · **Requirements:** MIG-6, CON-2 · **Depends on:** every fleet on v2
- **Steps:** delete the v1 aliases, the socket mount from the control plane, and the `docker` CLI dependency from the dashboard image.
- **Verify:** `cd dashboard && python -m pytest tests/ -q`; the image no longer contains a docker binary.
- **Rollback:** the previous image tag.
- **Done when:** the dashboard cannot reach a container engine directly.

---

## Phase 11 to 13 — Lifecycle, scaling, cache

### T-1301 — Lifecycle over the API

- **Gate:** LOCAL · **Requirements:** FR-6 · **Depends on:** T-0304, T-1402
- **Steps:** wire the eighteen verbs to routes; reads are GET, mutations POST with an idempotency key.
- **Tests:** every verb reachable; every mutation requires a key.
- **Done when:** the verb list in spec 12.2 and the route table agree, asserted by test.

### T-1302 — Drain semantics

- **Gate:** LOCAL · **Requirements:** FR-6, MIG-9 · **Depends on:** T-1301
- **Steps:** drain stops new work and lets the current job finish; cancel drain restores; no destructive verb runs against a busy instance.
- **Tests:** drain while busy does not abort the job; a destructive verb against busy is refused with a reason.
- **Done when:** "never abort a running job" is enforced in one place.

### T-1303 — Repair

- **Gate:** LOCAL · **Requirements:** FR-6 · **Depends on:** T-0308
- **Steps:** the `failed -> provisioning` edge, re-running only the incomplete steps.
- **Done when:** a half-created instance converges without manual cleanup.

### T-1501 — Desired capacity

- **Gate:** LOCAL · **Requirements:** FR-14 · **Depends on:** T-0302, T-1403
- **Steps:** scale up creates; scale down drains the idlest instances first and never touches a busy one.
- **Tests:** scale down with all instances busy waits rather than aborting; scale up respects worker capacity.
- **Done when:** capacity changes converge without an operator watching.

### T-1502 — Placement

- **Gate:** LOCAL · **Requirements:** FR-5 · **Depends on:** T-1501
- **Steps:** pick a worker by platform, architecture, free capacity and health; refuse rather than overcommit.
- **Done when:** no instance is placed on a `degraded` worker.

### T-1601 — Cache ownership

- **Gate:** LOCAL · **Requirements:** FR-16, CON-5 · **Depends on:** T-0203
- **Steps:** every scope resolves to a path derived from `runner_id`; a scope that cannot be attributed is not offered.
- **Tests:** no scope resolves outside the per-runner prefix, asserted for all three runtimes.
- **Security:** this is the control that makes `uniform.md` line 322 true.
- **Done when:** the scope list is generated, not hand-written.

### T-1602 — The generic clear action

- **Gate:** LOCAL · **Requirements:** FR-16 · **Depends on:** T-1601
- **Steps:** one operation, per-runtime adapters; measure before and after per scope; continue past a failing scope; report freed bytes and errors per scope.
- **Tests:** partial failure still reports; a second call frees nothing and succeeds.
- **Done when:** FR-17's six properties are each asserted.

### T-1603 — Cache safety proof

- **Gate:** LOCAL · **Requirements:** FR-17 · **Depends on:** T-1602
- **Steps:** with two instances on one worker, clear one and assert the other's storage is byte-identical before and after.
- **Done when:** "never damages another runner" is a test, not a claim.

---

## Phase 14 and 15 — Telemetry, logging, security

### T-1801 — Redaction

- **Gate:** LOCAL · **Requirements:** NFR-4 · **Depends on:** T-0306
- **Files:** create `dashboard/control/redact.py`, `dashboard/tests/test_redaction.py`.
- **Steps:** field-name based, applied to every API response, log line and audit row.
- **Tests:** a sentinel token value never appears in any of the three; a new field carrying a token fails the test until listed.
- **Done when:** NFR-4 is enforced centrally.

### T-1802 — Audit

- **Gate:** LOCAL · **Requirements:** NFR-7 · **Depends on:** T-0306
- **Steps:** append-only; accepted and refused operations both recorded with actor, verb, target and reason.
- **Done when:** a refused destroy is visible after the fact.

### T-1803 — Telemetry and health

- **Gate:** LOCAL · **Requirements:** FR-10, FR-12 · **Depends on:** T-0404
- **Steps:** per-instance CPU, memory, storage, cache, reachability, last heartbeat; `ready` requires both the process up and the forge online.
- **Tests:** a healthy process with an unreachable forge reports `unknown`, not `ready` — the behaviour measured on 2026-09-17.
- **Done when:** the card shows every field of spec 14.1 from real data.

### T-1901 — Move the forge tokens out of `.env`

- **Gate:** LOCAL to build, NEVER-AUTO to run · **Requirements:** NFR-4 · **Depends on:** T-1801
- **Steps:** a write-only secret store in the control plane; the settings page can set but never read back; `.env` is left in place and unmodified by this task.
- **Security:** `.env` currently holds four live secrets in plaintext and the settings page can write it. Moving them is the point of the task; rotating them afterwards is a human decision.
- **Done when:** no API response can return a token value.

### T-1902 — Role split

- **Gate:** LOCAL · **Requirements:** NFR-3 · **Depends on:** T-1802
- **Steps:** read, operate and destroy groups per spec 18.2; destroy requires admin.
- **Done when:** a non-admin cannot remove a runner or reduce capacity.

### T-1903 — Certificate lifecycle

- **Gate:** WINDOWS-INFRA · **Requirements:** NFR-1 · **Depends on:** T-0402
- **Steps:** issue, install, rotate and revoke per runbook 22.4; expiry is a worker health field.
- **Done when:** an expiring certificate is visible before it expires.

---

## Phase 16 — Migration and WSL retirement

Every task here is **NEVER-AUTO**. Each names its rollback.

### T-1701 — Pre-flight

- **Gate:** NEVER-AUTO · **Depends on:** T-0603
- **Steps:** record free disk, fleet capacity, registration list per forge and the history row count. This is the baseline every later step is compared against.
- **Done when:** the baseline is written into the plan's evidence file.

### T-1702 — Parallel run

- **Gate:** NEVER-AUTO, FORGE-LIVE · **Depends on:** T-0601
- **Steps:** one GitHub Linux instance on the new worker, serving real jobs for a full day beside the WSL fleet.
- **Rollback:** remove it; the WSL fleet never changed.

### T-1703 — Migrate GitHub Linux

- **Gate:** NEVER-AUTO, FORGE-LIVE · **Requirements:** MIG-1 · **Depends on:** T-1702
- **Steps:** grow the new fleet to match; then, one at a time: drain, wait for idle, deregister, remove.
- **Verify:** capacity never drops below the baseline; no orphaned registration.
- **Rollback:** cancel drain; both fleets carry the same labels so the forge spreads work.

### T-1704 — Migrate Forgejo Linux

- **Gate:** NEVER-AUTO, FORGE-LIVE · **Requirements:** MIG-2 · **Depends on:** T-1703
- **Steps:** as T-1703, with the Forgejo ordering of T-1002: the forge record is deleted through the API before the container is removed.

### T-1705 — Migrate the Windows Forgejo service

- **Gate:** NEVER-AUTO, WINDOWS-INFRA, FORGE-LIVE · **Requirements:** MIG-3 · **Depends on:** T-0705
- **Steps:** managed instance with the same labels; verify it takes a job; stop the NSSM service once idle; delete its forge record through the dashboard; only then remove the service and directory.
- **Rollback:** restart the NSSM service, which is stopped rather than deleted until the last step.

### T-1706 — macOS into the lifecycle

- **Gate:** NEVER-AUTO, MACOS-ENV · **Requirements:** MIG-4 · **Depends on:** T-0802 or T-0803
- **Steps:** adopt the running runner per T-0802, then add the GitHub instance per T-0804. No hardware move; spec 16.4 is the procedure.
- **Done when:** the macOS runner has a RunnerSpec and the full action set.

### T-1707 — Verify what was preserved

- **Gate:** NEVER-AUTO · **Requirements:** MIG-7 · **Depends on:** T-1703..T-1706
- **Steps:** compare against the T-1701 baseline: history row count, registrations, names, labels. Caches are expected to be cold; that is recorded, not repaired.

### T-1708 — Retire WSL

- **Gate:** NEVER-AUTO · **Requirements:** MIG-8, CON-2 · **Depends on:** T-1707, T-1407
- **Steps:** delete `scripts/keepalive-distro.ps1`, `scripts/install-keepalive-task.ps1`, `scripts/publish-dashboard-lan.ps1`, `scripts/provision-distro.ps1` and `docker-compose.yml`; mark `install/*.sh` and `install/*.ps1` deprecated with a fail-fast pointer; retire `exporters/` per CON-9; unmark the xfails in `test_wsl_couplings.py`.
- **Verify:** `cd dashboard && python -m pytest tests/test_wsl_couplings.py -q` — all pass.
- **Rollback:** git revert; the WSL distro itself is stopped, not deleted, until T-2103.

### T-1709 — Orphan sweep

- **Gate:** NEVER-AUTO, FORGE-LIVE · **Requirements:** MIG-9 · **Depends on:** T-1708
- **Steps:** list forge registrations with no spec and specs with no execution unit; report both; delete nothing automatically.

---

## Phase 17 to 19 — Cutover, tests, documentation

### T-2101 — Cutover

- **Gate:** NEVER-AUTO · **Depends on:** T-1708
- **Steps:** point the reverse proxy at the control plane; keep `DASH_PUBLIC_URL` unchanged so OIDC keeps working; watch for one full working day.
- **Rollback:** T-2102.

### T-2102 — Documented rollback

- **Gate:** NEVER-AUTO · **Depends on:** T-2101
- **Steps:** written and walked through before T-2101 runs: restore the proxy target, start the WSL distro, start the old dashboard against its untouched volume. Recreate WSL runners only if the new fleet is unrecoverable.
- **Done when:** the procedure has been rehearsed, not merely written.

### T-2103 — Decommission

- **Gate:** NEVER-AUTO · **Depends on:** two weeks of stable operation
- **Steps:** delete the WSL distro and its VHDX. Last, and irreversible.

### T-2001 — Regression

- **Gate:** LOCAL · **Requirements:** NFR-12 · **Depends on:** all LOCAL tasks
- **Verify:** `cd dashboard && python -m pytest tests/ -q` and `cd agent && python -m pytest tests/ -q`
- **Done when:** both suites are green.

### T-2002 — Conformance per runtime

- **Gate:** HYPERV, WINDOWS-INFRA, MACOS-ENV · **Requirements:** ACC-6, ACC-11 · **Depends on:** T-0502
- **Verify:** `pytest agent/tests/contract --runtime=linux|windows|macos` on each real worker.
- **Done when:** each runtime passes or declares the gap in capabilities.

### T-2003 — Acceptance record

- **Gate:** mixed · **Requirements:** ACC-1..19 · **Depends on:** T-2002
- **Steps:** one table, one row per ACC id, with the command, the output, the date and pass or not-run. Not-run is a permitted and required outcome where the infrastructure does not exist.
- **Done when:** every ACC row has an entry and no row is claimed without evidence.

### T-2201 — Operator documentation

- **Gate:** LOCAL · **Depends on:** T-2003
- **Files:** create `docs/operations/runner-platform.md`; modify `README.md`.
- **Steps:** the runbooks of spec 22, the manual installation steps, and the gate table.

### T-2202 — Deprecation notices

- **Gate:** LOCAL · **Requirements:** MIG-8 · **Depends on:** T-1708
- **Steps:** every retired script gains a header saying what replaced it and when.

### T-2203 — Final report

- **Gate:** LOCAL · **Depends on:** T-2003
- **Steps:** the ten items of `uniform.md` 439-450, including the licence finding of spec 9.3 and an explicit list of tests not run.

---

## Evidence file

Every NEVER-AUTO task appends to `docs/superpowers/plans/2026-09-17-uniform-evidence.md`:
the date, the operator, the command, the output and the decision. That file is
the answer to "which tests were actually executed" (ACC-19).
