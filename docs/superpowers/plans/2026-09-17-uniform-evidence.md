# Uniform runner platform: evidence

Every step that touches live infrastructure is recorded here: the date, who
decided it, the command, what it gave, and the decision. The plan's rule for
NEVER-AUTO tasks, applied to every live change, so that "which of this was
actually done" has one answer (ACC-19).

## 2026-09-18 - the dashboard image rebuilt with the v2 work

- **Decided by:** the operator ("1: Akkoord", after being told that removing
  a runner or recreating a fleet would need the admin role on v1 as well).
- **Command** (in the `github-runners` distro, project `githubrunners`):
  ```
  docker compose -p githubrunners -f /mnt/d/docker-compose/GithubRunners/docker-compose.runners.yml build dashboard
  docker run --rm -e DASH_DATA=/tmp -e ENV_PATH=/tmp/.env --entrypoint python nomercy/runner-dashboard:local -c "import app, api_v2, cards; ..."
  docker compose -p githubrunners -f /mnt/d/docker-compose/GithubRunners/docker-compose.runners.yml up -d --no-deps dashboard
  ```
- **Output:**
  - the throwaway container imported the app with 69 routes, `/v2` and
    `/runners/<runner_id>` among them;
  - `runner-dashboard` was recreated, and nothing else: all 13 runners kept
    their uptime (25 h, 11 h and 20 h);
  - `/`, `/v2` and `/login` answered, the first two redirecting to sign-in;
  - the backfill ran and the log shows no traceback.
- **Found:** the Flask banner came out as `b'...'`, because the new log
  redactor accepted bytes. It was fixed (`4b6b0f7`), rebuilt and redeployed
  with the same three commands. The banner is clean.
- **Rollback:** `git revert` the commits since `c20b335` and repeat the build.
  The dashboard's data volume was not touched.

## 2026-09-18 - the Linux unit images built and tried in throwaway units

- **Decided by:** the operator ("3: Akkoord ... Je mag dus gewoon doorgaan",
  infrastructure work approved). Nothing that runs was touched: new image tags
  and containers of their own only.
- **Commands** (in the `github-runners` distro):
  ```
  docker build -f images/linux/unit/Dockerfile.forgejo --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest -t nomercy/runner-unit-forgejo:test images/linux/unit
  docker build -f images/linux/unit/Dockerfile.github  --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-github-runner:latest  -t nomercy/runner-unit-github:test  images/linux/unit
  docker run --rm --name unit-smoke-register     ... nomercy/runner-unit-forgejo:test   # register, stand-in forgejo-runner
  docker run --rm --name unit-smoke-register-gh  ... nomercy/runner-unit-github:test    # register, stand-in config.sh
  docker run -d --privileged --restart unless-stopped --name unit-smoke-drain ... nomercy/runner-unit-forgejo:test
  docker exec -i unit-smoke-drain /runner/register            # the plan on stdin
  docker update --restart=no unit-smoke-drain; docker kill --signal=TERM unit-smoke-drain   # the agent's drain, then again after 3 s
  docker update --restart=unless-stopped unit-smoke-drain; docker start unit-smoke-drain    # the agent's start
  docker rm -f unit-smoke-drain
  ```
- **Output:**
  - register printed `{"registration_id":"77","registration_uuid":"u-77"}`.
    A second call printed the same and did not call the runner again.
    deregister exited 3. Outside a container, the entry point refused.
  - The GitHub register passed `ACTIONS_RUNNER_INPUT_TOKEN` to `config.sh`
    in the environment. Its argument list was `--unattended --replace
    --disableupdate`, and the token was in it 0 times. An empty runner
    group was not passed at all.
  - The drain: the unit was `running` 3 s and 5 s after the first SIGTERM,
    with a second SIGTERM between. The stand-in runner logged `TERM
    received`, then `job finished`. The unit exited 0 after 11 s, with
    restart policy `no`, and was still exited 5 s later. The runner's
    configuration carried `shutdown_timeout: 3h`. After the start, the unit
    was `running` with policy `unless-stopped` and its runner started again.
  - Afterwards `docker ps` showed the 13 runners and the dashboard with their
    uptimes unchanged, and no `unit-smoke` container.
- **Found, and not changed (the live fleet is not touched without an
  instruction):** forgejo-runner cancels its jobs at once on SIGTERM unless
  its configuration sets `shutdown_timeout`. Its `config.example.yaml` says so
  ("If unset or zero, the jobs will be canceled immediately"), and its
  poller's `Shutdown` does so. `scripts/start-forgejo.sh` starts the daemon
  without a configuration, so a `docker stop` or a recreate of
  `forgejo-runner-1..3` cancels the job each is running. The unit image does
  not have this problem.
- **Left behind:** the two `:test` image tags, thin layers over the images
  already there. Remove with `docker rmi nomercy/runner-unit-forgejo:test
  nomercy/runner-unit-github:test`.

## 2026-09-18 - phase 5 prepared (not elevated; nothing on the host changed)

- **Decided by:** the operator ("3: Akkoord", phase 5 VM creation approved).
- **Command:** `infra\hyperv\Prepare-RunnerPlatform.ps1`, run twice. The
  second run kept everything and verified the image again.
- **Output:**
  - `D:\HyperV\runner-platform\ssh\id_ed25519`, the platform's own key. Its
    ACL grants SYSTEM, Administrators and the user.
  - The Ubuntu 24.04 cloud image, SHA-256
    `612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354`,
    equal to Canonical's SHA256SUMS. It was converted to
    `noble-server-cloudimg-amd64.vhdx` (2088 MB) in a throwaway `alpine:3.20`
    container.
  - `seed\rnr-control.iso` and `seed\rnr-linux-1.iso`: the admin account, a
    static management address, and an outbound adapter by DHCP. No secret and
    no code.
- **Changed in the design before anything was created:** the VMs reach the
  internet through a second adapter on the Default Switch, not through a
  NetNat of their own. WinNAT beside the NAT networks WSL and Docker Desktop
  keep is a known source of conflict, and WSL's network carries every running
  job (spec 20, OPEN-6).
- **Also found:** `enrol` on a store the controller had not made yet failed on
  a missing table. It was fixed and proven in the controller's own image, in a
  throwaway container that has since been removed. The controller planned a
  runner and held it in `planned` because no worker existed yet.

## 2026-09-18 - the elevated phase 5 step requested, and cancelled

- **Command:** `Start-Process pwsh -Verb RunAs ... New-RunnerPlatformVMs.ps1`
  at 14:53:48.
- **Output:** "The operation was canceled by the user." The UAC prompt was
  declined, or went unanswered. No log was written. `Get-NetAdapter` shows
  only `vEthernet (Default Switch)` and `vEthernet (WSL)`, so nothing was
  created. The request was not repeated.

## 2026-09-18 - forgejo-runner v13.1.0 built for Windows and macOS

- **Commands:** `docker run --rm golang:1.26.7 bash
  /build/build-forgejo-runner.sh v13.1.0`, twice, into two fresh output
  directories under `D:\HyperV\runner-platform\artefacts\`.
- **Output:** built from commit `6095cb17bfdbded5aa4ea84c18a4b69fd9574cca`
  with go1.26.7. The same three SHA-256 came out of both builds
  (`images/windows/manifest.json`). The Windows binary prints
  `forgejo-runner version v13.1.0`.

## 2026-09-18 - the Windows template's register exercised on this host

- **What:** `register.ps1` and `deregister.ps1`, run from a copy in the
  scratchpad against the built binary, as the agent runs them.
- **Found and fixed:** under `ErrorActionPreference = 'Stop'`, the
  deprecation warning that v13 always prints made every registration fail.
  Also, `register` against an unreachable instance never returned.
- **After the fix:** exit 124 at 91 s, with the reason given and the token
  not in the output. Idempotent with a registration present. Deregister
  exited 0 while unregistered and 3 once registered.
- **Cleanup:** the first, unbounded attempt left the test copy running. It
  was stopped by PID only after its path was checked to be the scratchpad
  copy. The live `forgejo-runner` service was never touched and was
  `Running` afterwards.

## 2026-09-18 - phase 5 deployed (elevated, by the operator)

- **Decided and run by:** the operator, `infra\hyperv\Deploy-RunnerPlatform.ps1`
  at 15:32:52 local. The log is `D:\HyperV\runner-platform\deploy-20260918-153254.log`.
- **Output:**
  - `rnr-control` was created: 4 GB static, 2 vCPU, 10.77.0.10. Commit free
    afterwards: 107.9 GB.
  - `rnr-linux-1` was created: 16 GB static, 8 vCPU, 10.77.0.20. Commit free
    afterwards: 91.8 GB.
  - The controller was up at `8c83fa7`, with its authority made on the
    control plane.
  - `rnr-linux-1` was enrolled, with the unit image
    `nomercy/runner-unit-forgejo:8c83fa7` built on it. Its agent is active
    and serving on 10.77.0.20:8443.
  - `beast-unit` (this host) was enrolled. The service `rnr-agent` is running.
  - `python -m control status`: both workers `healthy`, no runners, every
    fleet at capacity 0.
- **Checked afterwards:** `vEthernet (rnr-internal)` is up beside the Default
  Switch and WSL adapters. Commit free: 93.2 GB. The Windows runner is
  `Running`.

## 2026-09-18 - github-runner-1 stopped mid-job, by something outside this work

- **Seen at** 13:59Z, in the check after the deploy: `github-runner-1`
  `Exited (143)`. Its restart policy is `unless-stopped`, and it had not
  restarted.
- **Facts:**
  - dockerd logged `stopping restart-manager` for container `1a6cc29157d0`
    at 15:54:35 local (13:54:35Z). That is the mark of an explicit stop
    request through the engine's API.
  - The container exited at 15:54:52, 41 min of CPU consumed.
  - Its log shows `Running job: build-base / docker-build` from 12:57:49Z,
    then `Received SIGTERM`. The job was aborted.
- **Not the cause:**
  - The dashboard's request log has only the `/api/v2/fleet` poll between
    13:50Z and 13:58Z.
  - No systemd timer, cron entry or scheduled task in the distro stops
    containers.
  - No script in this repository does.
  - The deploy finished at about 13:37Z, and this session ran nothing
    between then and 13:59Z.
- **Not established:** who sent the stop. Two other interactive Claude
  sessions (FillCitiesKitV2) were running on this machine, one of them busy.
  A person using Docker Desktop or the CLI is also possible.
- **Action:** none. The runner was left stopped, pending the operator's
  word, because it may have been stopped on purpose.

## 2026-09-18 - github-runner-1: why its docker-build jobs hang at "Check Available Space"

- **Reported by:** the operator. Job
  <https://github.com/NoMercy-Entertainment/nomercy-ffmpeg/actions/runs/35347449994/job/105607264536>
  printed "Checking available disk space..." and nothing more. This solves
  the previous entry: that job was on `github-runner-1`, which is registration
  `nomercy-vn6jx`. It hung from 12:57:59Z until the container was stopped at
  13:54:35Z.
- **The step** (nomercy-ffmpeg `reusable-docker-build.yml`) runs `df -h`,
  `free -h` and `df -h /tmp`.
- **Only runner-1, and every docker-build job on it** (dashboard history plus
  its own log):
  - 17 Sept, 11:14Z (`nomercy-efi7l`, runner-1): hung for 4 h, then
    cancelled.
  - 17 Sept, 20:53Z (runner-1): ran until the container was stopped at
    00:40Z.
  - 18 Sept, 12:57Z (runner-1): hung until 13:54Z.

  Its short jobs, which have no `df` step, succeeded. Across the last 34
  nomercy-ffmpeg runs, no other runner had a slow disk check. The re-run of
  the job succeeded on github-runner-9 at 14:04Z.
- **What runner-1 is:**
  - Its configuration is identical to github-runner-2's apart from cpuset
    (0-15 against 4-19).
  - Like runners 2, 3 and 5-10, the live container has no volume for its
    nested engine. The engine runs fuse-overlayfs on the container's own
    writable layer. `docker-compose.runners.yml` already declares a volume
    for runners 1-6, but only github-runner-4 has been recreated with one.
  - The writable layers of runners 1 and 2 are too large for `du` to finish
    in 120 s. Runner-4's layer is 705 MB.
- **Started again at 14:35Z** (the operator's go) and probed at once:
  - `df -h` answered, and every mount answered `statfs` within 5 s.
  - The nested dockerd sat in D state (`submit_bio_wait`,
    `folio_wait_bit_common`). Loading the graphdriver took 27 s, where it
    takes 1-2 s elsewhere.
  - It came up. The runner has been `Listening for Jobs` since 14:37:03Z.
- **Conclusion, and its limit:** the hang belongs to runner-1's own
  persistent state, its fuse-overlayfs data root in an oversized writable
  layer, and it survives restarts. The stall could not be caught live, so
  which call blocks `df` is inferred, not observed.
- **Fix proposed, not applied:** recreate runner-1 with the volume the compose
  file declares (overlay2, a fresh layer, as runner-4), then put cpuset 0-15
  back. The recreate was refused by this session's permission check, and it
  waits for the operator.

## 2026-09-19 - github-runner-1 recreated onto its own volume (by the operator)

- **Decided and run by:** the operator, after the diagnosis above. The script
  refused while the runner had a job and was run once it was idle.
- **What it did,** in one runner and no other:
  - renamed the live container (made by the dashboard, so compose could not
    replace it in place) and stopped it, letting it deregister itself;
  - had compose create `github-runner-1` with the volume
    `githubrunners_github-runner-1-docker`;
  - put cpuset 0-15 back, which a recreate always drops;
  - removed the old container once the new one was listening.
- **Output:** `Nested Docker: /var/lib/docker is ext2/ext3 -> overlay2`,
  registered as `nomercy-72fkt`, `Listening for Jobs` at 2026-09-19T22:28:59Z.
  The whole run took 5 min 57 s, most of it removing the old writable layer.
- **After:** 13 runners and the dashboard up, runner-1 with cpuset 0-15 and
  32 GB, `df -h` inside it answering. The distro's disk went from 677 GB used
  to 525 GB: 152 GB freed.
- **Still on the old arrangement:** github-runner-2, -3 and -5 to -10 run
  their nested engine on fuse-overlayfs in their own layer, and runner-2's
  layer is already too large for `du` to walk. They were not touched.

## 2026-09-19 - the first runner on the new platform, made and removed (T-0601 done)

- **Decided by:** the operator ("akkoord"). FORGE-LIVE: a real Forgejo
  registration, with a label no workflow asks for, so no production job could
  reach it.
- **Made:** `python -m control capacity forgejo-linux-x64 1` on the control
  plane. The controller placed the runner on `rnr-linux-1`, the agent built
  the unit `rnr-f226fbdd-a04f-4ae8-bf77-5fc3a260fd22` from
  `nomercy/runner-unit-forgejo:8c83fa7`, registered it at Forgejo, and the
  flow reported it `idle` - which needs both the agent and the forge to say
  so.
- **Removed:** `capacity forgejo-linux-x64 0`. Within a minute:
  `draining` (the agent's own drain on the worker), then `removing`, then
  gone from the store. No container was left on the worker.
- **Checked at the forge, read-only:** Forgejo lists five runners, the three
  WSL Forgejo containers and the Windows and macOS ones. No `rnr-` runner is
  left, so the record was deleted with the runner.
- **This is phase 5's "done when": the controller can create and remove a
  throwaway Linux instance on its worker.** Both suites were green at the
  time: agent 403, dashboard 1689 with 9 expected failures.

## 2026-09-19 - github-runner-2 and -3 converted onto their own volumes

- **Decided by:** the operator ("ja ik geef akkoord"), after runner-1.
- **How, per runner** (`infra/fleet/Convert-RunnerToVolume.ps1`): its custom
  label `beast-unit` is taken off at GitHub, so no new job is routed to it;
  the script waits until GitHub and the runner both report no job; only then
  is the container replaced, its cpuset put back, and the new registration
  checked for the label again. Nothing is signalled while a job runs.
- **github-runner-2:** drained, converted, `overlay2`, registered as
  `nomercy-ngf2a` at 22:40:22Z with `beast-unit`, cpuset 4-19. The old
  container was removed after the new one was listening.
- **github-runner-3:** the same, listening at 22:53:32Z, cpuset 8-23. It was
  down about 8 minutes: unlike 1 and 2 it carried compose labels, so compose
  removed the old container - and its oversized layer - before making the
  new one. For the rest, `-PruneFirst` empties the old nested engine while
  the runner is drained, to keep that deletion off the critical path.
- **Left:** github-runner-5 and -6 (compose services), and -7 to -10, which
  the dashboard made and which its own `create()` will make again, with the
  volume it has given every new runner since 2026-09-17.

## 2026-09-19 - the first Windows unit, and the two faults it found

- **Asked for:** `capacity forgejo-windows-x64 1` on the control plane, with
  the pilot label `rnr-pilot-windows:host`, which no workflow asks for.
- **First it was not planned at all.** The store recorded the fleet
  available - seeding had seen `FORGEJO_RUNNER_ARTIFACT_WINDOWS` - while every
  pass refused it: "Forgejo publishes no windows runner binary". `plan()`
  re-checked availability with whatever the caller passed, and the reconciler
  passes nothing. Fixed (`7c9c2ce`): the service holds the deployment's
  settings and plans with them. The control plane was rebuilt at that commit
  and planned the runner immediately.
- **Then the create failed on this host**, and both halves of it are real:
  1. `icacls` refused the service's virtual-account SID:
     "No mapping between account names and security IDs was done". Windows
     maps that SID only once the service exists, so a tree cannot be locked to
     a service that has not been made yet - which is what the runtime did, and
     what its own docstring claimed was possible.
  2. The compensation then failed with "Can't open service!", which should be
     the no-op that says there is nothing to remove. NSSM writes UTF-16, so
     read as text its answer carries a NUL between every character and matched
     none of the strings the runtime looks for. Every status read had the same
     problem.
- **Fixed** in `agent/runtimes/windows_process.py`: the service is made first,
  then the tree is locked to it, and it is started last, so nothing of the
  runner's runs while its tree is open; and NSSM's output is read as text
  before anything is matched against it. The fake worker now answers as NSSM
  does, so the suite would have caught the second fault; three tests pin both.
- **Left as it is for now:** the failed runner
  `daa3e0b7-1f49-4bb3-b799-0ab07b0fad5f` and its directory tree
  `D:\runners\daa3e0b7-...`. The fixed agent has to be deployed first, which
  needs `Install-WindowsWorker.ps1` run elevated.

## 2026-09-19 - the Windows cell, made and removed on this host (T-0703)

- **Decided by:** the operator, who confirmed it touches neither the running
  CI nor the WSL fleet: it is this host and Forgejo only.
- **Cleared first:** `capacity forgejo-windows-x64 0` removed the failed
  runner from the store. Its directory tree stayed, because the create had
  failed before recording a handle and the removal asked for nothing. Fixed
  in the flow: a removal falls back to the name every unit of that runner
  has, which is derived from its runner_id (15.1). The tree from that first
  attempt was deleted by hand afterwards.
- **Made:** with the fixed agent deployed (`695bca7`), `capacity 1` gave
  `registering` and then `idle` within 40 s.
  - The service `rnr-3eddff71-95e2-4132-8f2f-e3343dad7d34` runs under its own
    virtual account, `SERVICE_START_NAME : NT SERVICE\rnr-3eddff71-...`,
    through NSSM at `C:\ProgramData\nomercy\bin\nssm.exe`.
  - Forgejo listed it as `rnr-3eddff71`, idle, with the single label
    `rnr-pilot-windows` - which no workflow asks for.
  - **The isolation is real:** `icacls D:\runners\3eddff71-...` from an
    ordinary account answers "Access is denied". Only SYSTEM, Administrators
    and the runner's own account are on that tree.
- **Removed:** `capacity 0` took it through `draining` and `removing` to gone
  in 90 s. No service is left, no directory tree, and Forgejo lists no `rnr-`
  runner.
- **So the Windows cell works end to end on a real worker**: create, register,
  drain, deregister, remove - each runner its own account, its own tree and
  its own Job Object.

## 2026-09-19 - github-runner-5 and -6 converted; what compose did to -6

- **github-runner-5:** drained at GitHub, emptied, converted, `overlay2`,
  registered again with `beast-unit`, cpuset 17-32. About a minute out of
  service.
- **github-runner-6 took 45 minutes**, and compose is why. For a runner it
  manages, compose removes the old container - and its oversized layer -
  before creating the new one, and that removal wedged: the old container sat
  `Dead` with no mounts left and no progress, while the new container had
  been created but never started, under compose's temporary name.
  - The new container was correct in every other way: the image, the volume
    `githubrunners_github-runner-6-docker` and 32 GB.
  - It was renamed to `github-runner-6`, started, and given back cpuset
    24-39. It came up on `overlay2`, registered as `nomercy-4ttl8` and has
    `self-hosted, Linux, X64, beast-unit`. No job was touched: it had been
    drained at GitHub since the start.
  - The old container is left `Dead`. A container wedged this way cleared
    itself after about three hours once before, and nothing waits on it.
- **The lesson for the rest:** `-PruneFirst` was not enough, because the
  deletion still runs while compose holds the service. Runners 7 to 10 are
  not compose services, so they take the fast path the dashboard's own
  `create()` gives - the one runners 1 and 2 took, where the old container is
  removed only after the new one is serving.
- **Fleet now:** runners 1 to 6 on their own volumes with `overlay2`; 7 to 10
  still to do, held at the operator's word while a CI run is in flight.

## 2026-09-19 - the Windows runner taken over by the platform (MIG-5 for this cell)

- **Decided by:** the operator, who confirmed nothing was using it.
- **Before:** the service `forgejo-runner`, started by NSSM from
  `C:\forgejo-runner`, running a binary that reports `dev`. Forgejo knew it as
  `beaststack-windows-runner` (id 2) with `windows-2022, windows-latest`.
- **Steps:**
  1. The controller was given the production labels for the Windows cell
     (`Initialize-RunnerPlatform.ps1 -ControlPlaneOnly -WindowsLabels
     'windows-2022:host,windows-latest:host'`).
  2. The operator ran `Retire-LegacyWindowsRunner.ps1`, elevated. It asked
     Forgejo first - `idle` - then stopped the service and set it to Manual.
     Nothing was deleted: `C:\forgejo-runner` and its registration stayed.
  3. `capacity forgejo-windows-x64 1`: the controller made the unit, which
     reached `idle` in 100 s as the service
     `rnr-249c8d01-45b0-4479-a385-b582355a2bfa`, under its own virtual
     account.
  4. Forgejo then listed `rnr-249c8d01` idle with `windows-2022,
     windows-latest`, and `beaststack-windows-runner` offline.
  5. Only then was the old record deleted (HTTP 204).
- **After:** Forgejo has five runners, one of them the managed Windows one.
  The old service is `Stopped`, `Manual`.
- **Rollback, one command:** `Retire-LegacyWindowsRunner.ps1 -Restore` starts
  the old service, which registers itself afresh. The managed runner can be
  removed with `capacity forgejo-windows-x64 0`.
- **What differs for a job:** the managed runner runs under a virtual account
  with its own tree, its TEMP inside it, and a Job Object ceiling of 8 GB.
  Tools installed system-wide are still there; anything installed only in the
  old runner account's profile is not.

## 2026-09-19 - github-runner-7 converted, the fast way

- Drained at GitHub, converted through the dashboard's own `create()` - it is
  not a compose service - and back listening as `nomercy-sbirg` with
  `beast-unit`, on its own volume. About a minute out of service, against
  github-runner-6's 45 minutes through compose.

## 2026-09-20 - T-1701 pre-flight: the migration baseline

Read-only, 00:05Z. Every later migration step is compared against this.

- **Disk** (the distro's docker data): 1007 G, 409 G used, 548 G free, 43%.
  It was 677 G used before the conversions started.
- **Containers:** 14 running - 10 GitHub runners, 3 Forgejo runners, the
  dashboard.
- **GitHub:** 20 registrations. 10 online with `beast-unit`; 3 belong to other
  machines (`ffmpeg-verify-*`), 2 to other runners (`nomercy-fq47l` Eagle,
  `nomercy-mac-mini`); **5 are offline leftovers of tonight's conversions**
  (`nomercy-1jsjo`, `-9pusj`, `-c70mu`, `-o3wh9`, `-s8mvr`).
- **Forgejo:** 5 registrations - 3 WSL runners, the macOS appliance, and the
  managed Windows runner `rnr-249c8d01`.
- **History:** 4793 runs over 13 runners.

**Found by this baseline:** every conversion leaves an offline registration
behind, because the old container's `config.sh remove` times out after five
seconds. The conversion script now deletes the old record itself, once the
container is stopped and its replacement is registering. The five already
left behind are reported, not deleted - T-1709's rule - and wait for the
operator's word.

## 2026-09-20 - the last two conversions, and what runner-9 taught the script

`github-runner-9` was the one conversion that went wrong, and the fault was
the script's, not the engine's.

- **What happened:** the dashboard's `create()` reports a timeout after 180 s.
  On a busy engine the container was still being made, so the timeout was not
  a failure - it was impatience. The script believed it, rolled back, and
  `docker rm -f github-runner-9` ran against a container that did not exist
  yet. Seconds later it did exist, in `Created`, holding the name; the
  rename that would have put the old container back then failed on that name,
  and runner-9 was out of service.
- **What repaired it:** nothing had to be rebuilt. The created container was
  intact, with its own volume and `beast-unit`. Starting it, putting cpuset
  34-49 back and waiting 90 s brought it back as `nomercy-4z8l3`, online with
  `self-hosted,Linux,X64,beast-unit`. Its old container was removed after.
- **What changed in the script:** after a `create()` timeout it now asks the
  engine for up to five minutes whether the container appeared, and only rolls
  back when it truly did not. A timeout is a report about waiting, not about
  the result.
- **github-runner-10** then converted cleanly with that script: drained,
  old record deleted, made by the dashboard, cpuset 38-53 back, listening as
  `nomercy-vusuv` at 00:33Z and running a job within the minute.

**The fleet now:** all ten GitHub runners run on their own `overlay2` volume,
each with its 16-core cpuset. Disk 447 G used of 1007 G, against 677 G before
the conversions started. No runner is on the nested `fuse-overlayfs` layer
that made `df` hang inside jobs.

## 2026-09-20 - T-0802: the macOS runner adopted, without being touched

The Forgejo runner on the macOS appliance has been serving since June. It is
now an ordinary managed runner, and nothing about it changed to make it one.

- **Its machine became a worker.** `Install-ApplianceHost.ps1` enrolled
  `macos-appliance-1` (172.19.136.46) and installed the agent there as a
  systemd service. The agent runs on the machine that hosts the appliance,
  not inside the guest: the guest forwards one port, its SSH, and the
  hypervisor side can only be reached from the machine. `guest_ssh` is the
  runtime's `run` and `fs` acting inside the guest over that port.
- **Two things were put inside the guest, both harmless:** a wrapper that
  runs `launchctl` through `sudo` (the runner is a *system* daemon there, so
  only root may ask launchd about it) and the directory an instance keeps its
  own data in.
- **Adopted at 01:56Z** as `fc5bc5c1-65da-4295-9109-58183d9c4d1f`, reported
  `idle` on the next pass.

**What was checked afterwards:**

| | before | after |
| --- | --- | --- |
| Forgejo record | id 4, uuid 82dc2d96-ff8c-4e4e-8d67-6ffdf9d9362f, idle | identical |
| launchd job | `system/org.forgejo.runner`, running, pid 270 | running, same |
| its files | `/usr/local/forgejo-runner` | untouched |

The runner never left `idle` and no token was minted.

**What the first attempt taught.** The worker refused the create: `spec
carries fields this verb does not take: ['adopt']`. Adding the fields as
sent would have meant letting the controller hand a worker a path, which a
unit spec has never done. So the block names the unit and the template it
was built from, and the worker asks launchd where the job's definition lives
- which is also what lets a stopped adopted runner be started again. The
refusal was the protocol doing its job.

**Departures, recorded:**
- The appliance is a machine of its own, not a guest of the platform's Linux
  worker as 16.4 expected. No new worker kind was added for it: placement now
  reads what a worker declares it drives, the way it already reads what it
  declares it can hold. A third kind is where "uniform" would quietly stop
  being true.
- That machine's firewall is left alone. It existed before the platform and
  is reached for other things, VNC among them; a default-deny under it would
  take those away. Its agent's port is on the host-internal network and still
  refuses everyone without the controller's certificate (13.2).
- The adopted runner carries `macos-13, macos-14, macos-15, macos-latest`,
  which are not the fleet's labels, and the reconciler says so on every pass.
  That is true and worth saying: adopting keeps a runner exactly as it is.

## 2026-09-20 - the WSL VM was killed for commit exhaustion, and what it cost

At 10:28:40 Windows' Resource-Exhaustion-Detector diagnosed a low virtual
memory condition and killed the WSL VM. It named the consumers: `vmmem` at
128 GiB, `ffmpeg` at 7.6 GiB and `ffprobe` at 7.4 GiB - and there were seven
of those, about 48 GiB between them. The VM came back at 10:29:23.

**What went down with it.** Everything in that VM: the thirteen runners and
the dashboard in the `github-runners` distro, and the whole Docker Desktop
stack in the other - Forgejo, Immich, MinIO, the AI gateway. Every job that
was running was lost. Docker Desktop's own distro did not come back on its
own; its engine answered only after the application was restarted.

**Why the budget was wrong.** `.wslconfig` gave WSL 120 GB against about
90 GB outside it, leaving 45 GB of margin. Two things had changed since that
sum was written: the platform's own VMs arrived on 18-09 (control plane 4 GB
+ worker 16 GB = 20 GB of commit), and today's ffmpeg peak was about 48 GiB
rather than the ~14 GiB the budget assumed. 120 + 90 + 20 = 230 of 256,
with a spike of that size on top.

**The change, at 10:36, with every runner idle** (so it interrupted no job):
the cap is now `memory=96GB`, backed up as `.wslconfig.bak-20260920-103500`,
with the new sum in the file's own comments: 96 + 90 + 20 = about 206, ~50 GB
of margin. The runners keep their 32 g ceiling each; what shrinks is how many
heavy jobs can run at once - about four rather than five - which is cheaper
than a VM that is shot and takes every running job with it.

**Afterwards, verified:** the distro reports 94 GB, all fourteen containers
are up with their cpusets intact (0-15, 24-39, 38-53 spot-checked), the agent
in the distro came back by itself, the ten GitHub runners are online, and the
address did not change, so the dashboard's portproxy still points at it.

**One repair was needed.** Forgejo answered 502 from outside for eight
minutes while `forgejo` itself was healthy and its gate was serving runners
200s. The published port was the broken part: Docker Desktop's port
forwarder accepted connections on 3300 and closed them ("empty reply"), a
stale mapping left by the VM being killed under it. Restarting the container
that publishes the port restored it, locally and publicly.

**A finding worth keeping.** Each of the two restarts left every GitHub
runner a fresh registration: `nomercy-<random>` is minted by the start script
on every boot, so the old record is stranded rather than reused. Seventeen
offline records were left this morning. That is precisely what a RunnerSpec
ends - the platform registers once and records the id - and it is the
strongest argument yet for adopting this fleet into the controller.

## 2026-09-20 - the WSL fleet adopted, and the three runners it cost

All thirteen runners on the WSL engine are managed: ten GitHub, three
Forgejo, each with the registration it already had and the container it
already was. With the Windows and macOS cells, the controller now holds
fifteen runners on four workers.

**How.** `Install-WslAgent.ps1` put the agent in the distro as a systemd
service; `Publish-WslAgent.ps1`, elevated, carried the control plane's calls
over a portproxy on 10.77.0.1:8453, because nothing routes between the
internal switch and WSL's own network. Then one `adopt` per runner, naming
the forge record and the container.

**What went wrong, in order:**

1. Every adoption reached `provisioned` and then failed at `verify_online`.
   The cause was `runner_id_of`, which read a runner_id out of the unit's
   handle - possible only because every handle so far was `rnr-<uuid>`.
   `github-runner-1` is not, so the readiness call raised and the flow waited
   120 s for a runner that was online the whole time. `ExecUnitRef` had said
   from the start that the controller "stores it and hands it back, and never
   parses it"; the reference now carries the runner it belongs to.
2. Fixing that removed an accident that had been protecting the fleet. While
   the handle could not be read, every compensation failed harmlessly -
   `remove_unit: 'github-runner-1' is not a unit this controller made`. With
   the handle readable, the next failed step compensated the way it does for
   a runner the controller built: delete the forge record, then remove the
   unit. **github-runner-4 and -5 were removed and github-runner-1 was left
   dead.**

**What it cost and how it was put back.** Three runners were out of service
for about twenty minutes. Their volumes survived, so compose rebuilt all
three onto their own data - caches intact - and their cpusets were put back
(0-15, 12-27, 17-32). Each registered afresh at GitHub; their specs were
pointed at the new records and repaired. The forge records of the two that
were removed had been deleted by the compensation, which is why they could
not simply be re-adopted.

**The rule that now exists:** `compensations()` answers nothing at all for a
spec that carries `adopt_unit`, at every step that can fail. A compensation
undoes what the flow did, and for an adopted runner the flow made nothing.
Removing such a runner deliberately still works; only the automatic undo is
refused.

**Also recorded:** every adopted runner reports "registered with other
labels than the fleet's" - `beast-unit` for the GitHub ones, the ubuntu-*
set for Forgejo, macos-* for the appliance. That is true and deliberate:
adopting keeps a runner exactly as it is, and the fleet's own labels are
what a *new* runner would get.

## 2026-09-20 - one dashboard, where the store is (T-0603, T-0604)

`https://gh-runners.phillippepelzer.me` now serves the dashboard on the
control plane, beside the store the controller writes. Its fleet page shows
all fifteen runners on four workers; the one in the WSL distro showed none of
them, because it was reading a `/data/control.db` that does not exist there.

**What moved.** The deployment's `history.db` (4965 runs), `users.json`,
`auth.json`, `state.json` and `secret.key`, into the controller's own volume.
The history was taken with sqlite's backup rather than copied as a file - it
is being written to while the old dashboard serves - and the session key was
carried deliberately: a cookie signed by the old dashboard is accepted by the
new one, so the switch signed nobody out. The public URL had to stay the
same for the same reason the OIDC redirect is registered for it.

**What it runs with.** The settings it needs, read from the deployment's own
`.env` and written only on the control plane (`/etc/runner-platform/
dashboard.env`), plus what the platform itself decides - unit images, unit
memory, the drain group. No Docker socket: every runner it shows is reached
through its worker's agent, which is what CON-2 asks for.

**The home page follows the host.** Where there is an engine it is the page
it has always been; where there is none it redirects to the fleet page.
Sending someone to a v1 page on the control plane would be a page of errors
about a socket that is deliberately absent.

**The switch, and the way back.** One portproxy rule on the host:
`192.168.178.19:9200` pointed at `10.77.0.10:9200` instead of the distro.
The old dashboard is still running and its volume was never touched, so
pointing that rule back at `172.28.202.20:9200` restores it exactly.

**Verified after the switch:** `/login` answers 200 on the public name, the
dashboard reports `engine_reachable: False` and fifteen runners, and the
first signed-in requests (`/api/status`, `/api/v2/fleet`) answered 200 -
the copied session key did its work.

## 2026-09-20 - what the first look at the one dashboard showed

The page came up with every card reading `unknown`, "not reachable", no
telemetry, and a header counting one runner out of fifteen. Three separate
faults, each worth keeping:

1. **A heartbeat lists the units a worker can enumerate, and neither runtime
   could enumerate an adopted one.** On Linux the enumeration filters on the
   `nomercy.runner_id` label, which a container cannot be given after it is
   made; in the appliance it looks for a launchd job named after a
   runner_id, which an adopted job is not. Both now answer from the record
   written at adoption. That one fix turned every card from `unknown` into
   `idle` with real CPU and memory.
2. **A note was living in the error's column.** Every adopted runner carries
   labels of its own - `beast-unit`, the ubuntu-* set, macos-* - and the
   reconciler recorded that in `last_error`, which the page paints red. Its
   own comment said "not a failure - the runner works". It has its own
   column, its own line and its own colour now, and the fourteen already
   written were moved across.
3. **The forge was asked once per runner, not once per pass.** Fifteen
   runners every fifteen seconds is 3600 calls an hour against a 5000-an-
   hour limit shared with two dashboards, and GitHub answered 403 - rate
   limit exceeded - so every GitHub card read `unknown` even once its unit
   was reported. A pass now reads each forge once, with the pass itself as
   the boundary; the loop that waits for a new registration still reads
   afresh, because it is waiting for the forge to change its mind.

Also: the home page no longer offers the v1 link on a host with no engine.
It redirected the reader straight back to where they came from.

**After:** fifteen cards, all `idle`, all reachable, no errors, fourteen
notes. The old dashboard in the distro is stopped - it was polling the same
token and writing a history that had already been copied - and bringing it
back is `docker start runner-dashboard` and the portproxy rule.

## 2026-09-20 - a cell that cannot be built says so, and Windows gained one

The page offered `+ Add runner` for GitHub on Windows and on macOS. Neither
could work: the Windows worker had one template installed, Forgejo's, and
the appliance had none. The creation would have failed on the worker after a
spec was written.

**What a worker now says.** Each runtime declares what it builds units from:
`template` with the list it has (Windows, the appliance) or `image`, which
means anything it can pull (Linux). The controller resolves what a unit of a
fleet is made from the way the runtime will - what the deployment names for
the cell, else the fleet's own template, without a digest - and answers
whether some healthy worker could build it. A cell nobody can build is
unavailable with the reason; a cell whose workers are merely down is not
refused, because a worker comes back and a runner planned meanwhile waits.

**Two attempts that were wrong, and why.** Putting the check in
`provider.supports()` broke 394 tests, rightly: that method answers what the
*forge* supports, not what this deployment has. Putting it in the fleet's
seed broke ten, for the same reason at one remove: a seed knows the
deployment's settings but not its workers. It belongs where both are known,
which is the service.

**GitHub's runner on the Windows worker.** Fetched rather than built -
GitHub publishes it - and checked against the SHA-256 GitHub states in the
release's own notes, read from its API: `d59123a4...c162` for
actions-runner-win-x64-2.336.0. The template has the same three entry points
as the Forgejo one, with the runner one level down in `agent\` because it
ships a `run.cmd` of its own. Registration goes through
ACTIONS_RUNNER_INPUT_* so the token is never on a command line, and
deregistration says what it cannot do, so the controller deletes the record
by its id. The 103 MB payload is never committed.

**After:** `github-windows-x64` reports available; the Windows worker lists
`actions-runner-v2.336.0-windows` and `forgejo-runner-v13.1.0-windows`.
`github-macos-x64` still reports the reason - the appliance has no templates
yet - and `forgejo-macos-x64` still wants its artefact named.

**One more thing the live page showed:** it built its service without the
deployment's settings, so every cell read as unbuildable even after the
template was installed. The page's service now carries them.

## 2026-09-20 - the first rebuild under a uniform name, and the four things in its way

**What was asked.** One fleet, one naming. `github-runner-1`, adopted that
morning, was the first to be rebuilt as what its fleet calls it:
`github-linux-x64-1`. It took four fixes, each found by measuring rather
than by reasoning about it, and each one is a defect the next twelve
rebuilds would have hit.

**1. A cold image takes longer than the create was given.** The create
timed out at 180 s, twice, leaving a container made but never started,
whose storage the undo then found in use. Measured on the worker: `docker
create` from the freshly built 17 GB GitHub unit image took **3 m 2 s**; a
second create from the same image took **2.5 s**. The first container from
an image pays for its layers being unpacked into the snapshotter. The
create now has 240 s, a unit found made but stopped is started rather than
handed back, and `setup-worker.sh` and `setup-wsl-agent.sh` warm each image
they build, so the wait is paid at install time. (`7b21479`)

**2. The name only changed where the adoption ended.** Two places end an
adoption; only the reconciler's gave the runner its fleet's name. The
flow's own - the worker reports no unit left to adopt - kept whatever the
adopted container was called, which is why the fleet still read as three
eras of naming at once. One definition now, next to the naming rule it
uses. (`4612a22`)

**3. A registration outlived the unit it belonged to.** After the undo
deleted the forge record, the spec still named it: the register step found
that record at the forge, took it for this runner's own and waited for a
container that no longer existed to come online. Ten minutes, then the
deadline. A unit about to be made now drops that record, and a record the
forge shows at work is left exactly where it is (MIG-9) and said in the
note. The check that keeps a deletion off a running job reads the forge
fresh rather than from the pass's list. (`4612a22`)

**4. The unit answered from its own volume.** A unit keeps `.runner` and
`.credentials` on its registration volume and cannot be made to drop them -
`deregister` leaves them deliberately - so it answered every later
registration with the id of a record the controller had deleted. A plan now
carries `replace`, set whenever the controller's own spec names no
registration. Two registrations of one unit can also overlap, and the
second moved the link the first had just made into the volume:
`/runner/reg/.credentials -> /runner/reg/.credentials`, which the runner
reads as "too many levels of symbolic links" and aborts on, for ever. A
link is no longer moved. (`d91be1d`)

**5. And then GitHub would not talk to it.** The rebuilt runner registered,
connected, and was told: *Runner version v2.333.1 is deprecated and cannot
receive messages*. The image a unit is built from ships 2.333.1; every
runner that was serving had replaced it with 2.336.0 at its own start,
through `scripts/start.sh`, which the unit entry point deliberately does
not run. The unit image now pins the version and checks the download
against the hash GitHub publishes for that release
(`04cf0be1...5d5d`), recorded in `images/windows/manifest.json` beside the
Windows and macOS ones. (`c1d8083`)

**After, measured:** `github-linux-x64-1` idle, registration 2013, GitHub
showing it online under that name; container
`rnr-1d7c4bc9-...` from `nomercy/runner-unit-github:c1d8083`, cpuset
`0-15`, memory 32 GiB, runner 2.336.0, "Listening for Jobs". The rebuild
from `failed` to `idle` took 90 seconds. Nothing else in the fleet was
touched: nine GitHub runners and three Forgejo runners kept serving
throughout, four of them busy at the time.

**What the operator should know.** A worker's unit images are built and
warmed by its install script, so a controller deploy that moves the image
tag needs the worker deployed first - the order is worker, control plane,
dashboard.

## 2026-09-20 - the rest of the fleet renamed, and what the engine cost

**The second rebuild found four more things**, all of them about time and
about what the engine is really doing while the controller waits.

**A removal is not over when the client returns.** Docker's removal is
asynchronous: `docker rm` answers while the daemon is still taking the unit
apart, and everything said to it meanwhile is refused - "container is
marked for removal" for an update, "volume is in use" for its storage.
That is what left five volumes behind on the first rebuild and what broke
the second. A removal is now waited out, before a unit is built over one
that is going and before its storage is removed.

**A unit is stopped before it is forced.** `rm -f` gives a container ten
seconds and then kills it, which takes its nested engine down mid-write.
Twice the engine was left unable to finish such a removal: the container
sat in `removing` with no processes and no mounts left, its name unusable,
for half an hour. Nothing is aborted by stopping first - a removal comes
after the drain and the deregistration (MIG-9).

**A removal that was refused is taken again.** A spec resting in `removing`
with no operation open was read as a unit already gone, and the rebuild
continued without it. A removal that finished does not rest there. What
rested there was a removal refused because the worker had been degraded for
a minute by the agent's own redeployment - with the runner still running
and its forge record already deleted. `github-runner-3` was left serving
nothing, unmanaged, while a second unit was built beside it. Removing is
safe to repeat, so it is taken again; the separate `rebuild` step that
assumed otherwise is gone.

**And the numbers were wrong, so they were measured.** On the WSL worker,
with the image already built:

| what | measured |
| --- | --- |
| `docker create` from the 17 GB GitHub unit image | 58 s quiet, 3 min busy |
| `docker start` of that container | 0.7 s |
| `docker rm -f` of a container holding nothing | 34 s |
| `docker volume create` / `rm` | 0.4 s |

So the cost is preparing each container's own snapshot, and it is paid
every time - warming the image buys nothing, and the throwaway container
that was doing it left a corpse whose name would have blocked the next
install. A slow agent verb had 300 seconds; every rebuild died at that
deadline with its runner already deregistered and removed. The deadline is
now 900 seconds, in 17.2's table and in the code, the create inside the
agent has 600, and a volume has 120 rather than 30 - it failed at 30 once,
with the runner already gone.

**Why the worker kept going degraded, which is the one that mattered.**
Not the deploys: the beats themselves. A heartbeat measured before it was
sent - `docker stats` over every running unit, and every thirtieth beat a
storage and cache probe that asks each unit's own nested engine - and all
of it ran on the thread that sends. On this worker that took minutes, so
the beats stopped: "degraded - no heartbeat for 344s" while the agent was
perfectly well and simply counting. Three missed beats is an absence, an
absence is a gate, and the gate refused the removals. Measuring now has a
thread of its own; a beat carries the last measurement while it is fresh,
and otherwise says only that the worker is here - which leaves every unit
exactly where it was. After the fix the worker was healthy again within
two seconds of the agent restarting.

**What the operator should know.** A rebuild and a deploy still do not mix:
restarting the agent interrupts whatever verb was in flight. Deploy first,
see the worker healthy, then rebuild.

## 2026-09-21 - one fleet, one naming, and the four defects the last ten found

**After:** every runner the controller manages carries its fleet's name, at
the forge and on the page.

| fleet | runners |
| --- | --- |
| `github-linux-x64` | `github-linux-x64-1` … `-10`, all idle or serving |
| `forgejo-linux-x64` | `forgejo-linux-x64-1`, `-2`, `-3` |
| `forgejo-windows-x64` | `forgejo-windows-x64-1` |
| `forgejo-macos-x64` | `beaststack-macos-sequoia`, still adopted |

The thirteen containers in the WSL distro are all `rnr-<runner_id>` units
built from `nomercy/runner-unit-{github,forgejo}`, each on its own five
volumes. Nothing there is adopted any more; the `github-runner-N` and
`forgejo-runner-N` containers, and the compose file that made them, serve
nothing. The macOS runner is the one exception, and stays adopted under its
own name until its worker has a template to rebuild one from.

**A removal the client gave up on.** Tearing a unit down takes its nested
engine and its layers apart, which outlived the 180 seconds `docker rm` was
given. The client giving up does not stop the daemon - the unit was gone a
minute later - but the step had already reported a failure and stranded the
rebuild with its runner deregistered. A removal now has 420 s, and a client
that still gives up waits for the unit to go rather than calling it a
failure.

**A poll that does not answer.** Asking an agent how its work is going is
not the work. A poll timed out after ten seconds on the saturated worker,
and the create it was watching - which finished - came back as a bare
`TimeoutError` two minutes into a half-hour budget. An unanswered poll now
leaves the state as it was and the loop keeps asking.

**A heartbeat that stopped.** Measuring ran on the thread that beats, so a
busy engine silenced the beats for minutes: "degraded - no heartbeat for
344s" while the agent was perfectly well and merely counting. Three missed
beats is an absence, an absence is a gate, and the gate refused the
removals. Measuring has its own thread now. Then the beat thread died
outright when the controller was recreated under it - nine minutes of
silence, ended by restarting the agent by hand - so a beat that raises is
now one beat, not the end of the loop.

**And the numbers, measured on a worker under its own fleet.** `docker
create` took two and a half minutes there, the same for a 700 MB image as
for a 17 GB one, because what it waits for is a disk at 58% full I/O
pressure (`/proc/pressure/io`, avg300) - while `docker start` took under a
second and a volume under half of one. A slow agent verb now has half an
hour, its create 1500 s, and a volume 120 s.

**Two more, from the forges themselves.** Forgejo answers a runner's ping
in about five seconds through the gate in front of it, and five seconds is
where forgejo-runner gives up; the unit now makes three attempts, and the
second succeeded every time. And GitHub refuses a deprecated runner, so the
unit image pins 2.336.0 against the hash GitHub publishes.

**A worker that had not been upgraded.** The fleet is deployed a worker at
a time, and `replace` reached the Windows worker's older agent, which
refuses a plan carrying a field it does not know - by name. Its runner had
already been removed for its rebuild, so it was unregisterable until
somebody could run an elevated installer. A refusal that names only flags
is now answered by registering without them; any other refusal stands. The
Windows runner came back under its fleet's name a minute later.

**Left for the operator.**

- `beast-unit`, `rnr-linux-1` and `macos-appliance-1` still run the agent
  from an earlier commit. The Windows one needs `Install-WindowsWorker.ps1`
  elevated; the other two are a deploy away.
- Two offline GitHub records, `nomercy-9x9fl` (2002) and `nomercy-ty0g9`
  (2001), are ghosts of the first rebuild's failures and want deleting.
- 26 volumes on the WSL worker belong to no container: the old fleet's
  caches and workspaces. Nothing reads them.
- The WSL worker is I/O-bound under its own fleet. Every number above is a
  consequence; the fleet's own builds pay it too.
