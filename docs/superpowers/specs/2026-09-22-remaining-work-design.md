# Finishing the uniform runner platform - design

Date: 2026-09-22. Status: approved by the operator in conversation the same
day. Parent design: `2026-09-17-uniform-hyperv-runner-platform-design.md`
(section numbers without a file name refer to it). Current state:
`docs/operations/runner-platform.md` section 1.

This design covers every point the platform does not yet do as `uniform.md`
and the parent design describe, plus one new request: the dashboard shows the
labels a workflow can use. It is split into seven workstreams (W1-W7) that
can be accepted one by one. Each says what is done, how it is proven, and how
it is undone.

## 0. Rules that hold for every workstream

- **No job is ever aborted.** Every destructive step goes through the
  controller, which drains first and acts only on an idle or drained runner.
  Nothing is stopped or removed by hand while a controller path exists.
- **One runner of a fleet out of service at a time**, for recreate, cache
  clear and migration alike.
- **Order of deployment:** worker agent, then control plane, then dashboard,
  and never while the controller is rebuilding runners (a restarted agent
  drops the verb in flight). Confirm the worker is healthy between steps.
- **Every live change is announced before it is made**, with what it touches.
  Changes to live infrastructure, registrations or production configuration
  need the operator's go at the time; approvals already given are recorded in
  the plan.
- **Backups before mutations:** the control store and history with SQLite's
  backup API before every controller deploy; originals of disks are kept, not
  overwritten, until the replacement has served real jobs.
- **Proof, not claims.** A workstream is done when its acceptance check has
  been run against the live system and its output recorded in
  `docs/superpowers/plans/2026-09-17-uniform-evidence.md`. After any change
  to a unit image or template, the proof includes a real forge job and the
  result counts in `history.db`.
- **Tests first** for every code change (red, then green), and the full agent
  and dashboard suites green before anything is deployed.
- **Commits** in the operator's name, no AI attribution, not pushed.
- Out of scope: the `nomercy-ffmpeg` repository (its `BUILD_JOBS=12` is the
  cause of its slower builds; left to its owners), and buying a Windows
  Server licence (OPEN-2).

## W1. GitHub Linux: finish the job-completed hook fix

**Problem.** From 2026-09-21 every GitHub job on the ten Linux runners was
marked failed: the unit set `ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/runner/cleanup`
and the GitHub runner runs a hook only if its path ends in `.sh`, `.ps1` or
`.js`. Fixed in image `nomercy/runner-unit-github:e6b7a117356a` (commit
5525332); `github-linux-x64-1` runs it, the fleet's unit template names it.

**Design.** No new code. After one real job has completed successfully on
`github-linux-x64-1`, recreate the fleet through the controller (one runner
at a time, idle only). A recreate keeps each runner's cpuset, memory, swap
and disk limits (verified: `replacement_spec` differs from the live spec only
in `runtime_template`).

**Acceptance.**
1. `history.db` shows a GitHub run on `github-linux-x64-1` with result
   `Succeeded` after 2026-09-22T12:59Z, and its job log has no
   "is not a valid path to a script".
2. All ten GitHub Linux containers run image `e6b7a117356a`, each with its
   original cpuset, `Memory` 32 GiB and `MemorySwap` 64 GiB, read-only root,
   `nproc` 16.
3. GitHub lists all ten online with `self-hosted, Linux, X64, beast-unit`.
4. After 24 hours: GitHub runs on the fleet have results other than
   all-failed (a failed run is inspected: the failing step is the
   workflow's, not "Complete runner").

**Rollback.** Set the fleet's unit template back to
`maintenance-20260921-r2` and recreate. (That image has the fault; rollback
exists only for a fault in the new one.)

## W2. The labels a workflow can use, on the dashboard

**Problem.** A card shows `rnr-linux-1 · linux/x64`, which is the worker and
platform, and nothing shows the labels a runner is registered with. That is
how the pilot labels stayed unnoticed for two days.

**Design.**

- **Source of truth is the forge.** The controller already reads each forge's
  runner list once per pass (`ProvisioningFlow._records`) and finds each
  runner's record by registration id (`record_for`). On every observation it
  now also stores the record's label names on the spec, in a new column
  `runner_specs.forge_labels` (JSON list, nullable), with the time in
  `forge_labels_at`. A record without a label list leaves the stored value as
  it was; a runner with no record gets `null` (unknown, never "none").
- **Normalised to what `runs-on:` takes.** GitHub labels are the `name` of
  each label object. Forgejo labels are names; if a Forgejo label ever comes
  back as `name:scheme://image`, only `name` is kept. Order is the forge's.
- **The card** gets one line, "Labels", with the stored names as chips, and
  "unknown" when there are none yet. The line with the worker reads
  "worker rnr-linux-1 · linux/x64", so it is not read as a label.
- **The detail page** shows the same list, and the fleet's own labels (what a
  new runner is registered with) under the fleet header on the fleet page.
- **Drift is visible.** `registration_drift` already compares a record with
  what the runner was registered with. Its sentence is stored in
  `last_note` today; the card shows it as a warning line when it is set, so
  a runner that registered with other labels than its fleet asks is flagged
  in amber, not buried in a note.
- **No platform conditional** in a template or route (the existing grep
  tests hold); the provider adapter does the normalising.

**Data flow.** forge list -> `record_for` -> provider `record_labels(record)`
(new, on both adapters) -> `specs.update(forge_labels=...)` -> card builder
-> `/api/v2/fleets` and `/api/v2/runners/<id>` -> template.

**Acceptance.**
1. Unit tests: both adapters' `record_labels`, the store column and its
   migration on a copy of the live database, the card field, and "unknown"
   for a runner with no record.
2. Live: every card shows the labels GitHub or Forgejo lists for that runner
   (checked against the forge APIs for all runners), and the fleet header
   shows the fleet labels.
3. The browser test renders a card with labels.

**Rollback.** Redeploy the previous image; the new column is unused by it.

## W3. macOS: one guest per runner

**Decided by the operator:** each macOS runner gets its own QEMU guest with
enforced CPU and memory (design 10.5); the macOS host VM stays at 24 GiB.

**Current state.** Hyper-V VM `macos-runner` (Ubuntu, 24 GiB) runs one
Docker-OSX container `macos-sequoia` (QEMU, `RAM=12`, 6 vCPU) whose guest
holds the adopted runner `beaststack-macos-sequoia` (Forgejo). Its agent is
the older one (`20c0a63`) without pool support. Built and tested but not
live: `agent/runtimes/macos_pool.py` (per-UUID guests, overlays on a
read-only base, SSH port per guest), the appliance image
`nomercy/macos-appliance:20260921-r3` (pinned id
`sha256:ab06f626...e745`), the two guest templates, and a copied base disk
`/var/lib/runner-appliances/bases/clean-base-20260921.qcow2` that still holds
the old registration, launchd state and markers. The r3 guest has not yet
been shown to boot to macOS and SSH.

**Budget.** Two guests at 8 GiB RAM and 4 vCPU, plus 2 GiB QEMU overhead
each, is 20 GiB; the host keeps about 4 GiB. The old guest (12 GiB) cannot
run beside them, so the migration has a short Forgejo macOS outage. The
worker declares `memory_bytes` 20 GiB and `max_runners` 2 for placement.

**Design, in order:**

1. **Clean base, from the copy only.** Boot a throwaway guest on an overlay
   of the copied base with networking to the forges blocked, remove the
   runner's `.runner`, `.credentials*`, the adopted marker, caches and the
   launchd job for the old runner, set launchd's disabled state for any
   runner label, shut the guest down cleanly, and write the result as a new
   standalone base (`qemu-img convert`, not an overlay). The original disk of
   `macos-sequoia` is never opened writable. The base's SHA-256 is recorded.
2. **Boot proof.** Start one pool guest from the new base with the r3 image
   and prove: macOS reaches login, SSH answers on its port with the pool's
   key, both templates are present and executable, no runner process runs,
   and a clean `shutdown -h now` powers it off within the timeout. Recorded
   in the evidence file. If r3 does not boot, the fault is found and fixed in
   `images/macos/appliance/` before anything else in W3.
3. **Installer.** `Install-ApplianceHost.ps1` gains an `-AppliancePool`
   switch that writes the `appliance_pool` block (image id, base, BaseSystem,
   data root, templates, `base_guests_disabled`, uid/gid, SSH port base,
   timeouts) and the pool's capacity, validated by the agent's own config
   loader before the service is restarted. Tests cover the written config.
4. **Controller settings.** `FORGEJO_RUNNER_ARTIFACT_MACOS` names the
   installed Forgejo template with its SHA-256, which makes the Forgejo
   macOS fleet available and rebuildable. Fleet defaults: CPU 4, memory
   8 GiB for both macOS fleets; labels: Forgejo keeps `macos-15, macos-14,
   macos-13, macos-latest` (what the adopted runner carries), GitHub gets
   `beast-unit`.
5. **Migration, with the operator's go at the time:**
   1. Drain `beaststack-macos-sequoia` through the controller and wait for
      it to be idle and drained.
   2. Set the Forgejo macOS fleet to 0: the controller deregisters it and
      removes the adoption. The runner's files stay in the old guest.
   3. Stop `macos-sequoia` cleanly (`restart=no` already), keep it and its
      disk untouched as the rollback.
   4. Deploy the new agent with the pool (step 3).
   5. Raise Forgejo macOS to 1, then GitHub macOS to 1, one after the other.
6. **Placement** already requires `appliance_per_runner` and enforcement for
   a macOS runner with CPU or memory limits (`placement.enforces_appliance_limits`);
   no change there.

**Acceptance.**
1. Both macOS runners are pool guests with their own overlay, SSH port, 4
   vCPU and 8 GiB, reported by the agent (`resource_enforcement` on the card).
2. Forgejo lists `forgejo-macos-x64-1` online with the four macOS labels;
   GitHub lists `github-macos-x64-1` online with `self-hosted, macOS, X64,
   beast-unit`.
3. Each takes a real job and succeeds.
4. Drain, restart, clear cache, recreate and remove work on both (W7).
5. The host's free memory with both guests idle is recorded and above 2 GiB.

**Rollback.** Before step 5.4: cancel the drain. After: set both macOS
fleets to 0, reinstall the previous agent, start `macos-sequoia`, adopt the
runner again with `python -m control adopt` (its registration was deleted in
5.2, so the runner re-registers from the old guest - accepted as the price
of the rollback).

**Risk.** R-1 of the parent design stands: macOS on this hardware is outside
Apple's licence and on unsupported layers. Nothing here changes that.

## W4. Hyper-V checkpoints

**Problem.** `rnr-control` and `rnr-linux-1` run on `.avhdx` differencing
disks: each has a checkpoint, and every write since goes into a growing
child disk. `rnr-linux-1`'s data disk has one too.

**Design.** An elevated read-only script lists each VM's checkpoints
(`Get-VMSnapshot`), their type and date, and the chain of every disk. Then,
per VM and with the operator's go, `Remove-VMSnapshot` for the checkpoints
the operator agrees are no longer needed; Hyper-V merges the child disk into
its parent while the VM runs. Merge is done when idle (no running jobs on
`rnr-linux-1`), and one VM at a time, waiting for the merge to finish
(`Get-VM` status and the `.avhdx` gone) before the next. The VM's automatic
checkpoints are switched off (`Set-VM -AutomaticCheckpointsEnabled $false`)
so a restart does not create a new one.

**Acceptance.** `Get-VMHardDiskDrive` lists only `.vhdx` paths for both VMs;
both VMs healthy; the controller and all runners on `rnr-linux-1` unaffected
(no container restarted, `StartedAt` unchanged).

**Rollback.** None needed: removing a checkpoint discards only the ability
to return to that point in time, which the operator decides per checkpoint.

## W5. Forgejo Linux memory

**Evidence, 2026-09-22.** The three Forgejo units at 6 GiB RAM + 6 GiB swap
reached their limit (`memory.events max` 1270-3625) with `oom_kill 0` and
about 55 MiB anonymous memory; the rest was page cache, reclaimed under the
limit. So the jobs they run fit, and the limit bounds cache, not work.

**Design.** Keep 6 GiB. Record the evidence in the operations page. The rule
for raising it: any `oom_kill` in a Forgejo unit's `memory.events`, or
anonymous memory above 4 GiB in a measured job. Raising it must fit the
Linux worker's admission (72 GiB RAM + 640 GiB swap, bounded overcommit).

**Acceptance.** The evidence and the rule are in the operations page.

## W6. WSL out of the runner architecture

**Design (design 16.8 and `uniform.md` MIG-8):**

- Delete `scripts/keepalive-distro.ps1`, `scripts/install-keepalive-task.ps1`,
  `scripts/publish-dashboard-lan.ps1`, `scripts/provision-distro.ps1`,
  `infra/fleet/Install-WslAgent.ps1`, `infra/fleet/Publish-WslAgent.ps1` and
  the drifted `docker-compose.yml`. Before deleting, grep for every reference
  and remove or update it; the portproxy that publishes the dashboard is
  owned by `infra/hyperv` and is kept.
- `install/*.sh`, `install/*.ps1`, `scripts/start*.sh` and
  `docker-compose.runners.yml`: headers marked deprecated, and the install
  scripts fail fast with a pointer to `docs/operations/runner-platform.md`.
- The dashboard's v1 pages and the Docker-socket code they use: nothing on
  the control plane uses them (no socket there). Remove the v1 routes, the
  WSL collector and `docker_ops.py` together with their tests, after the grep
  shows no v2 path imports them; `/` redirects to the v2 fleet page.
- Remove the worker `wsl-linux-1` from the inventory (a new
  `python -m control retire-worker <host_id>`, refused while any spec names
  the worker) and revoke its certificate pin.
- **Not in this workstream:** deleting the WSL distro `github-runners`. It is
  the rollback copy of the old volumes. Criterion for deleting it: seven days
  with every Linux runner on the Hyper-V worker, no rollback needed, and the
  operator's explicit go.

**Acceptance.** `rg -i "wsl"` in the operational code paths returns only the
Docker Desktop notes and history; the full suites pass; `wsl-linux-1` is
gone from the workers table; the dashboard works with v1 removed.

**Rollback.** `git revert` of the removal commit; the old dashboard image
still exists on the control plane.

## W7. Regression CI and the acceptance record

**CI.** Replace `.github/workflows/build-image.yml` (it builds the retired
WSL runner image) with `tests.yml`: on push and pull request, on the fleet's
own runners (`[self-hosted, Linux, X64, beast-unit]`), set up Python 3.12,
install `flask`, `flask-sock`, `cryptography`, `pytest`, and run the agent
and dashboard suites. No secrets are needed. The Windows-only tests skip on
Linux as they do today.

**Acceptance run (design 19.4).** For each of the six cells, through the
dashboard's own API: create (scale up), a real job, drain, cancel drain,
restart, clear cache (with before and after sizes), recreate, remove (scale
down), with the forge's view checked after each. One table in
`docs/superpowers/plans/2026-09-17-uniform-acceptance.md` with the date,
command, output and pass or not-run per ACC row. A real job is triggered in
a repository of this project (a `workflow_dispatch` workflow added for the
purpose to this repository, and a Forgejo repository the operator names),
never in a consumer repository. Cells that cannot be run are recorded as not
run with the reason.

**Acceptance.** CI green on the branch; the acceptance table complete; the
parent design's section 23 checks (conformance suite on all runtimes,
import isolation, no platform conditional in templates) pass.

## W8. The Linux unit's root filesystem is writable again

**Added 2026-09-22, after W1's roll-out, from a live failure.** The managed
storage mode runs each Linux unit with `--read-only` and a tmpfs `/run`. Two
jobs of `nomercy-docs` then fail where they succeeded before the move:
`android-actions/setup-android` ("Read-only file system", repeatedly) and
`npx playwright install --with-deps` ("E: List directory
/var/lib/apt/lists/partial is missing. - Acquire (30: Read-only file
system)"). Both install system software into the runner's own filesystem,
which every GitHub-hosted runner allows and the WSL fleet allowed.

**Decided by the operator:** the root filesystem is writable again. The rest
of the isolation stays exactly as it is - one container per runner, its own
100 GiB filesystem for `/runner`, its own cpuset, memory and swap ceilings,
its own registration and caches. What is given up is that a job can dirty
the image layer of its own runner, which a recreate discards anyway.

**Design.** The agent's `storage` configuration gains `readonly_root`
(boolean, default true, so nothing else changes by omission). When it is
false the runtime does not pass `--read-only`, does not require the image's
`nomercy.readonly_root` label, and does not demand a read-only root when it
checks an existing container or runs the maintenance helper. Everything else
about managed storage - the owned volumes, their identity checks, the
per-runner filesystem - is unchanged. The Linux worker sets it false.

**Acceptance.** `readonly_root: false` on the Linux worker; the thirteen
units recreated; `docker inspect` shows `ReadonlyRootfs: false` and the same
cpuset, memory, swap and volumes as before; the `nomercy-docs` run that
failed on this succeeds; a Forgejo job still succeeds.

**Rollback.** Remove the key (default true) and recreate.

## W10. The Windows runners move into their own Hyper-V guest

**Added 2026-09-23, decided by the operator**, who will supply and activate a
Windows licence. This reopens OPEN-2 of the parent design: it was decided on
2026-09-18 that no licence would be bought, and the Windows runners have run
as services on BEAST-UNIT itself ever since. The operator's objection is the
one the design records as the cost of that choice - a job runs on the host
that also runs their desktop, and the whole machine is its playground.

**W10a (done 2026-09-22, before the guest exists):** a Windows fleet's
whole-number CPU limit becomes a per-runner window of cores, enforced as a
Job Object affinity mask, exactly as the Linux cpusets work. Both Windows
runners are held to 16 of the 56 processors.

**W10b, the guest.**

| | |
| --- | --- |
| Guest | Windows 11 Pro, Gen 2, vTPM and Secure Boot on (Hyper-V supports both), 16 GiB static memory, 8 vCPU, a 200 GiB dynamic system disk and a 200 GiB data disk for the runners' VHDX store |
| Network | The `rnr-internal` switch with a static address in 10.77.0.0/24 (.30), as the other guests have, plus the Default Switch for outbound traffic |
| Licence | The operator's: the ISO is downloaded here, the key is entered and activated by the operator over the console or RDP. Nothing about the key is stored in this repository or passed to any tool |
| Agent | `Install-WindowsWorker.ps1`, unchanged, run inside the guest with `-WindowsStorage`; it enrols with the controller and gets its own certificate |
| Runners | Created by the controller on the new worker, with the same fleets, labels and limits; the two runners on the host are drained, deregistered and removed afterwards |

**Division of work.** This session prepares the VM, the unattended install
answer file (local administrator, OpenSSH server, RDP, no telemetry opt-ins)
and the scripts; the operator runs the elevated steps and does the licence
and activation. No key, product ID or activation output is ever written to
the repository or to an evidence file.

**What must be true before the host's runners go:** the guest's worker reports
healthy, a runner created there registers at its forge, and it runs one real
job of each provider. Only then are the host's services removed - the
rollback until that moment is that they are still there and serving.

**What this does not solve.** A Windows guest cannot nest Docker for Windows
containers without nested virtualization, which Hyper-V supports on this
processor; the runners do not use Windows containers today (design 9.2), and
nothing here changes that.

## Order and dependencies

W1 first (it is the fault with the widest effect), then W2 (small, and it
makes every later check visible), W5 (documentation only), W6's code
removal, W3 (the longest, with one operator-timed outage), W4 (needs a quiet
window), and W7 last, because its acceptance run covers what the others
built.

## What "done" means

All seven workstreams accepted as above, the evidence recorded, the
operations page describing the result, the suites green in CI, and the only
open items those the operator has decided to leave: the WSL distro until
its seven days, and R-1.
