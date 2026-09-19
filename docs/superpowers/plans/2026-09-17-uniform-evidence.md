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
