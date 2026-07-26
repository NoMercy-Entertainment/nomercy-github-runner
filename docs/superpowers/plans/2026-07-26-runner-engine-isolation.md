# Runner Engine Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the six GitHub Actions runners onto a dedicated WSL distro with its own Docker Engine and its own 1 TB virtual disk at `D:\Docker\GithubRunners\Data`, so they share no disk, daemon, image store, volume namespace or socket with the BeastStack production stack — and add an always-on dashboard for their status.

**Architecture:** A new WSL distro `github-runners` runs its own `dockerd` under systemd. Its `ext4.vhdx` lives on D:, giving physical disk separation from `docker_data.vhdx`. The runner Compose stack moves there, with `deploy.replicas: 6` replaced by six explicit services. A small dashboard container on the same engine serves a live status page. The migration builds the new engine alongside the running fleet and only tears the old one down at the very end.

**Tech Stack:** WSL2 2.7.11, Ubuntu 24.04 rootfs, Docker CE with systemd, Docker Compose v2, bash, a static-HTML + JSON dashboard served by a tiny Python/BusyBox container.

**Spec:** `docs/superpowers/specs/2026-07-26-runner-engine-isolation-design.md`

## Global Constraints

Every task's requirements implicitly include this section. These are hard.

- **BeastStack is production and must not be touched.** It holds the user's photo library (Immich), object storage (MinIO) and git server (Forgejo). Nothing under `D:\docker-compose\BeastStack\` may be modified by this plan.
- **NEVER run host-wide or stack-wide Docker commands on the Docker Desktop engine.** No `docker system prune`, `docker volume prune`, host-level `docker image prune` or `docker builder prune`, no `--remove-orphans`.
- Every command against the **Docker Desktop** engine must name its target container explicitly (`githubrunners-github-runner-N`) or be scoped with `-f d:/docker-compose/GithubRunners/docker-compose.yml`.
- Commands against the **new** engine are addressed as `wsl -d github-runners docker ...` or via `DOCKER_HOST`. Never mix the two in one command.
- **Do not delete the old containers until Task 9**, which is explicitly gated on user approval. Until then they are the rollback.
- The existing fleet must keep serving CI throughout Tasks 1-6. Do not stop, restart or reconfigure the old runners in those tasks.
- `.env` holds a live GitHub PAT and is gitignored. Never commit it, never echo it, never bake it into an image.
- Branch: `runner-engine-isolation`. Commit after each task.
- Distro name: `github-runners`. Disk location: `D:\Docker\GithubRunners\Data`. Max disk size: **1 TB** (WSL default — no explicit resize needed).

---

### Task 1: Create the WSL distro and install Docker Engine

**Files:**
- Create: `scripts/provision-distro.ps1` (repeatable provisioning, so this is not a one-off manual ritual)

**Interfaces:**
- Produces: a running distro named `github-runners` with `dockerd` active, and its `ext4.vhdx` at `D:\Docker\GithubRunners\Data\ext4.vhdx`.

- [ ] **Step 1: Confirm the name is free and the target path is empty**

```powershell
wsl --list --verbose
Test-Path "D:\Docker\GithubRunners\Data"
```

Expected: only `docker-desktop` listed; the path either absent or empty. **If a distro named `github-runners` already exists, STOP** — do not overwrite it.

- [ ] **Step 2: Create the distro at the chosen location**

WSL 2.7.11 supports `--location`. Prefer it over `--import` (no rootfs tarball to source):

```powershell
New-Item -ItemType Directory -Force "D:\Docker\GithubRunners\Data"
wsl --install Ubuntu-24.04 --name github-runners --location "D:\Docker\GithubRunners\Data" --no-launch
```

If `--name`/`--location` are unsupported on this build, fall back to `--import` with a downloaded rootfs and report which path you took. Do not silently install to the default location — the whole point is the disk lives on D:.

- [ ] **Step 3: Verify the virtual disk is actually on D:**

```powershell
Get-ChildItem "D:\Docker\GithubRunners\Data" -Filter *.vhdx -Recurse | Select-Object FullName, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
```

Expected: an `ext4.vhdx` under that path. **If it is not there, STOP and report** — the requirement is unmet and continuing would build on a wrong foundation.

- [ ] **Step 4: Enable systemd**

```powershell
wsl -d github-runners -u root -- bash -lc "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf"
wsl --terminate github-runners
wsl -d github-runners -- systemctl is-system-running --wait
```

Expected: `running` or `degraded` (degraded is acceptable — some units do not apply under WSL).

- [ ] **Step 5: Install Docker CE**

```powershell
wsl -d github-runners -u root -- bash -lc "apt-get update && apt-get install -y ca-certificates curl && install -m 0755 -d /etc/apt/keyrings && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc && echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \$VERSION_CODENAME) stable\" > /etc/apt/sources.list.d/docker.list && apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
```

- [ ] **Step 6: Enable and start Docker, confirm it survives a distro restart**

```powershell
wsl -d github-runners -u root -- systemctl enable --now docker
wsl --terminate github-runners
wsl -d github-runners -u root -- bash -lc "sleep 15; docker info --format '{{.ServerVersion}} storage={{.Driver}}'"
```

Expected: a version and `storage=overlay2`. Note this engine uses **overlay2**, not `fuse-overlayfs` — it is a normal Linux Docker install, not nested, so the constraint that forced `fuse-overlayfs` inside the runners does not apply at this level.

The `--terminate` then restart is the point of this step: proving `dockerd` comes back by itself is what makes Task 7's boot task viable.

- [ ] **Step 7: Confirm the two engines are genuinely separate**

```powershell
wsl -d github-runners -- docker ps -a
docker ps --format "{{.Names}}" | Measure-Object -Line
```

Expected: the new engine lists **zero** containers; Docker Desktop still lists **22**. This is the isolation claim in its simplest form — record the output.

- [ ] **Step 8: Capture the provisioning into a script and commit**

Write `scripts/provision-distro.ps1` containing steps 2, 4, 5 and 6 with the halt conditions preserved, so the distro can be rebuilt without archaeology.

```powershell
cd d:/docker-compose/GithubRunners
git add scripts/provision-distro.ps1
git commit -m "feat: provision dedicated WSL distro and Docker engine for runners"
```

---

### Task 2: Prove the isolation, before relying on it

The whole plan rests on the claim that the two engines cannot reach each other. Test it rather than assume it — a false assumption here is the one failure that matters.

**Files:**
- Create: `docs/superpowers/plans/isolation-evidence-2026-07-26.txt`

**Interfaces:**
- Consumes: the distro from Task 1.
- Produces: committed evidence that the isolation holds.

- [ ] **Step 1: Confirm separate data roots**

```powershell
wsl -d github-runners -- docker info --format 'new engine root: {{.DockerRootDir}}'
docker info --format 'desktop root: {{.DockerRootDir}}'
```

Both may print `/var/lib/docker` — that is expected and is not a failure. They are different paths **in different filesystems on different virtual disks**. Establish that by the next step, not by this one.

- [ ] **Step 2: Confirm the filesystems are genuinely different disks**

```powershell
wsl -d github-runners -- bash -lc "df -h /var/lib/docker | tail -1; findmnt -no SOURCE /"
docker exec githubrunners-github-runner-1 df -h /var/lib/docker | tail -1
```

Expected: different device sources and different total sizes. The new engine's filesystem should reflect the new `ext4.vhdx`, not the 1007 GB `docker_data.vhdx`.

- [ ] **Step 3: Confirm neither engine can see the other's containers**

```powershell
wsl -d github-runners -- docker ps -a --format "{{.Names}}"
docker ps -a --format "{{.Names}}"
```

Expected: no overlap whatsoever. The new engine must NOT list `immich_server`, `minio_server`, `forgejo`, or any `githubrunners-*`.

- [ ] **Step 4: Confirm no shared image store or volume namespace**

```powershell
wsl -d github-runners -- docker images --format "{{.Repository}}:{{.Tag}}"
wsl -d github-runners -- docker volume ls
```

Expected: empty or only what Task 1 pulled. None of BeastStack's images or volumes.

- [ ] **Step 5: Fill-test — the decisive proof**

Write a large file inside the new engine's disk and confirm Docker Desktop's free space does not move.

```powershell
docker exec githubrunners-github-runner-1 df -h / | tail -1      # before
wsl -d github-runners -- bash -lc "fallocate -l 20G /var/lib/docker/_isolation_test && df -h /var/lib/docker | tail -1"
docker exec githubrunners-github-runner-1 df -h / | tail -1      # after
wsl -d github-runners -- bash -lc "rm -f /var/lib/docker/_isolation_test && df -h /var/lib/docker | tail -1"
```

Expected: the new engine's free space drops by ~20 GB; Docker Desktop's free space is **unchanged**. That is the requirement demonstrated rather than argued.

**Clean up the test file in the same step** — do not leave 20 GB allocated.

- [ ] **Step 6: Commit the evidence**

```powershell
cd d:/docker-compose/GithubRunners
git add docs/superpowers/plans/isolation-evidence-2026-07-26.txt
git commit -m "docs: record evidence that the two Docker engines are isolated"
```

---

### Task 3: Restructure the Compose file into six explicit services

`deploy.replicas: 6` gives every replica identical mounts. Six Docker daemons sharing one data root corrupt each other within minutes, so replicas are incompatible with per-runner storage. This task does the restructure only — it does not deploy.

**Files:**
- Create: `compose/docker-compose.runners.yml`
- Modify: `.env.example` (document any new variables)

**Interfaces:**
- Consumes: the existing `docker-compose.yml` and `scripts/start.sh` (already carrying the Phase A GC cap, janitor, and both registration fixes — do not alter them).
- Produces: a Compose file defining `github-runner-1` … `github-runner-6` for the new engine.

- [ ] **Step 1: Read the current Compose file and start.sh**

Read `docker-compose.yml` in full and note every key: `image`, `deploy.resources.limits`, `restart`, `privileged`, `environment` (`GH_TOKEN`, `GITHUB_ORG`, `RUNNER_LABELS`, `RUNNER_GROUP`), the `./scripts/start.sh:/root/start.sh` bind mount, and `tmpfs: /tmp`. Every one of these must survive into the new file — behaviour is to remain identical.

- [ ] **Step 2: Write the six-service Compose file**

Create `compose/docker-compose.runners.yml` with services `github-runner-1` through `github-runner-6`. Use a YAML anchor for the shared definition so the six differ only by name, rather than six copy-pasted blocks that will drift:

```yaml
x-runner: &runner
  image: ghcr.io/nomercy-entertainment/nomercy-github-runner:latest
  restart: unless-stopped
  privileged: true
  environment:
    GH_TOKEN: ${GH_TOKEN}
    GITHUB_ORG: ${GITHUB_ORG:-NoMercy-Entertainment}
    RUNNER_LABELS: ${RUNNER_LABELS:-self-hosted,Linux,X64}
    RUNNER_GROUP: ${RUNNER_GROUP:-}
  volumes:
    - ./scripts/start.sh:/root/start.sh:ro
  tmpfs:
    - /tmp
  stop_grace_period: 60s

services:
  github-runner-1:
    <<: *runner
    container_name: github-runner-1
  github-runner-2:
    <<: *runner
    container_name: github-runner-2
  github-runner-3:
    <<: *runner
    container_name: github-runner-3
  github-runner-4:
    <<: *runner
    container_name: github-runner-4
  github-runner-5:
    <<: *runner
    container_name: github-runner-5
  github-runner-6:
    <<: *runner
    container_name: github-runner-6
```

Two deliberate additions over the old file, both justified:
- `stop_grace_period: 60s` — Docker Engine 29.x defaults `StopTimeout` to 1s (moby/moby#52775), which kills deregistration mid-flight. Today's registration fix needs ~3s. Without this the fix silently stops working on the new engine.
- `:ro` on the start.sh mount — nothing should write to it, and the old read-write mount was an unnecessary hazard.

Note the bind-mount path is **relative**, so it resolves inside the distro. Task 4 decides where the repo lives.

- [ ] **Step 3: Validate the Compose file parses and expands correctly**

```powershell
wsl -d github-runners -- bash -lc "cd <repo path in distro> && docker compose -f compose/docker-compose.runners.yml config" | Select-String "container_name|stop_grace_period"
```

Expected: six distinct `container_name` values and six `stop_grace_period: 60s`. **Confirm the anchor actually expanded** — a broken `<<: *runner` yields services missing `image`, which Compose reports late and confusingly.

- [ ] **Step 4: Commit**

```powershell
cd d:/docker-compose/GithubRunners
git add compose/docker-compose.runners.yml .env.example
git commit -m "feat: six explicit runner services for the isolated engine"
```

---

### Task 4: Bring up ONE runner on the new engine, alongside the old fleet

The old six keep running throughout. This adds a seventh runner to the GitHub org temporarily — that is fine and intentional, and it is how we prove the new engine works without risking CI.

**Files:**
- None modified. Execution and observation.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: a working `github-runner-1` on the new engine.

- [ ] **Step 1: Decide and record where the repo lives inside the distro**

Two options — pick one and state why in the report:
- **Bind from Windows** (`/mnt/d/docker-compose/GithubRunners`): single source of truth, edits on Windows apply directly. Costs 9p filesystem access, but `start.sh` is a small file read once at boot, so the cost is negligible.
- **Clone into the distro** (`/opt/github-runners`): native filesystem, but now there are two copies to keep in sync.

Recommend the bind mount for a single source of truth. Verify it is readable:

```powershell
wsl -d github-runners -- bash -lc "ls -l /mnt/d/docker-compose/GithubRunners/scripts/start.sh"
```

- [ ] **Step 2: Confirm `.env` is readable and NOT copied anywhere**

```powershell
wsl -d github-runners -- bash -lc "test -r /mnt/d/docker-compose/GithubRunners/.env && echo readable"
```

Expected: `readable`. **Do not copy `.env`** — it holds a live PAT. Compose reads it in place.

- [ ] **Step 3: Start exactly one runner**

```powershell
wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f compose/docker-compose.runners.yml up -d github-runner-1"
```

- [ ] **Step 4: Verify it came up fully**

```powershell
wsl -d github-runners -- docker logs --tail 20 github-runner-1
wsl -d github-runners -- docker exec github-runner-1 docker info --format '{{.ServerVersion}} {{.Driver}}'
wsl -d github-runners -- docker exec github-runner-1 sh -c "ps aux | grep -c '[s]leep 21600'"
```

Expected: a fresh `nomercy-<5 chars>` registration reaching `Listening for Jobs`; inner daemon version with `fuse-overlayfs`; janitor count 1.

**If the inner daemon fails to start**, capture `docker exec github-runner-1 cat /var/log/dockerd.log | tail -40` and report. Nested DinD inside a WSL distro is the most plausible failure point in this plan — `.wslconfig` already has `nestedVirtualization=true`, but verify rather than assume.

- [ ] **Step 5: Confirm the GC cap is live on the new engine**

```powershell
wsl -d github-runners -- docker exec github-runner-1 cat /etc/docker/daemon.json
```

Expected: the `builder.gc.policy[].maxUsedSpace` block. A prior task established that `dockerd --validate` accepts unknown keys silently, so config presence alone is not proof of enforcement — Task 10 confirms enforcement by measurement.

- [ ] **Step 6: Prove it runs a real job**

Ask the user to trigger a workflow, or wait for one. Confirm in the logs that this runner picks up and completes a job. **Do not proceed to Task 5 until a job has actually run on the new engine** — registration alone does not prove the runner is useful.

- [ ] **Step 7: Confirm BeastStack is untouched**

```powershell
docker ps -q | Measure-Object -Line
```

Expected: 22 (the old six plus 16 BeastStack). The new runner is on the other engine and does not appear here.

---

### Task 5: Cut over — stop the old fleet, start all six on the new engine

This is the switchover. The old containers are **stopped, not removed**, so rollback stays available.

**Files:**
- None modified.

- [ ] **Step 1: Confirm all old runners are idle**

```powershell
1..6 | ForEach-Object { docker logs --tail 1 githubrunners-github-runner-$_ }
```

**If any shows `Running job:` without a completion, WAIT.** Do not kill a running build.

- [ ] **Step 2: Stop the old fleet with a grace period**

```powershell
1..6 | ForEach-Object { docker stop -t 30 githubrunners-github-runner-$_ }
```

`-t 30` is load-bearing: the host defaults `StopTimeout` to 1s, and today's registration fix needs ~3s to deregister cleanly. A bare `docker stop` leaves six offline entries in the org.

- [ ] **Step 3: Confirm they deregistered**

```powershell
# Uses GH_TOKEN from .env; read-only.
curl -sS -H "Authorization: Bearer $env:GH_TOKEN" "https://api.github.com/orgs/NoMercy-Entertainment/actions/runners?per_page=100"
```

Expected: the six old `nomercy-*` names gone. Any that remain are offline orphans — note them for cleanup in Task 9.

- [ ] **Step 4: Start all six on the new engine**

```powershell
wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f compose/docker-compose.runners.yml up -d"
```

- [ ] **Step 5: Verify all six**

```powershell
wsl -d github-runners -- docker ps --format "{{.Names}}`t{{.Status}}"
1..6 | ForEach-Object { wsl -d github-runners -- docker logs --tail 3 github-runner-$_ }
```

Expected: six running, each registered and `Listening for Jobs`.

- [ ] **Step 6: Confirm the org sees exactly six plus the Mac mini**

Expected total: 7 (`nomercy-mac-mini` is a separate machine and unrelated to this work).

- [ ] **Step 7: Confirm BeastStack is untouched and the old containers still exist for rollback**

```powershell
docker ps -a --filter "name=githubrunners" --format "{{.Names}}`t{{.Status}}"
docker ps -q | Measure-Object -Line
```

Expected: six old containers in `Exited` state (**not removed**), and 16 running BeastStack containers.

**Rollback at this point** is `docker start githubrunners-github-runner-1..6` plus stopping the new ones — still available, and it stays available until Task 9.

---

### Task 6: Make the distro survive a reboot

WSL distros do not auto-start. Without this the runners are down after every Windows restart — and this scheduled task becomes a new single point of failure, so it must be tested with a real reboot, not assumed.

**Files:**
- Create: `scripts/install-boot-task.ps1`

- [ ] **Step 1: Write a scheduled task that starts the distro at boot**

```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d github-runners -u root -- systemctl start docker"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "Start GitHub Runners Engine" -Action $action -Trigger $trigger -Principal $principal -Force
```

Note: starting the distro is what starts `dockerd` (systemd enabled it in Task 1). Compose services carry `restart: unless-stopped`, so the runners come back on their own once the daemon is up.

- [ ] **Step 2: Test WITHOUT rebooting first**

```powershell
wsl --terminate github-runners
Start-ScheduledTask -TaskName "Start GitHub Runners Engine"
Start-Sleep -Seconds 45
wsl -d github-runners -- docker ps --format "{{.Names}}"
```

Expected: six runners running. This catches a broken task before betting a reboot on it.

- [ ] **Step 3: Ask the user to reboot, then verify**

This step needs the user — do not reboot their machine unprompted. Ask them to reboot at a convenient moment, then confirm:

```powershell
wsl --list --running
wsl -d github-runners -- docker ps --format "{{.Names}}`t{{.Status}}"
docker ps -q | Measure-Object -Line
```

Expected: distro running, six runners up, BeastStack's 16 back as normal.

**If the runners do not come back**, the fallback is documented, not improvised: start them manually with `wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f compose/docker-compose.runners.yml up -d"` and report that the boot task needs rework.

- [ ] **Step 4: Commit**

```powershell
cd d:/docker-compose/GithubRunners
git add scripts/install-boot-task.ps1
git commit -m "feat: start the runner engine at boot"
```

---

### Task 7: Dashboard — collector

The dashboard is split into a collector (produces JSON) and a page (renders it). Splitting them means the data format can be tested without the UI, and the UI can be developed against a fixture.

**Files:**
- Create: `dashboard/collect.sh`

**Interfaces:**
- Produces: `status.json` with this exact shape, which Task 8's page consumes:

```json
{
  "generated": "2026-07-26T14:30:00Z",
  "disk": { "path": "D:\\Docker\\GithubRunners\\Data", "used_gb": 187, "total_gb": 1024, "percent": 18 },
  "runners": [
    {
      "name": "github-runner-1",
      "registration": "nomercy-hxlgk",
      "state": "busy",
      "job": "jvm-android",
      "uptime": "4h12m",
      "cpu_percent": 71.2,
      "mem_used_gb": 3.2,
      "mem_limit_gb": 16.0,
      "build_cache_gb": 12.4,
      "images_gb": 1.3,
      "layer_gb": 4.1
    }
  ]
}
```

- [ ] **Step 1: Write the collector**

`dashboard/collect.sh` runs on the new engine with access to its Docker socket. Data sources:

- `docker stats --no-stream --format "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"` — CPU and memory. This is the only source for live CPU; it takes ~1s per call, so call it **once for all containers**, not per runner.
- `docker ps --format "{{.Names}}\t{{.Status}}"` — uptime.
- `docker logs --tail 20 <name>` — parse `Running job: X` / `Job X completed` to derive busy/idle and the current job name. Busy means the last job line is a `Running job:` with no matching completion after it.
- `docker exec <name> cat /root/actions-runner/.runner` — the registered `agentName`. Guard with `2>/dev/null` and fall back to `"unknown"`; a runner mid-restart has no `.runner` file.
- `docker exec <name> docker system df --format json` — inner build cache and image totals.
- `df` on the engine's data root — disk usage for the whole fleet.

**Do NOT use `docker ps -s`** for per-container layer size. It walks every layer and took over an hour on this host under contention. Report layer size from the inner `docker system df` instead, or omit it.

Every `docker exec` must be wrapped so one unresponsive runner cannot hang the whole collection — use `timeout 5`.

- [ ] **Step 2: Run it and validate the output is real JSON**

```bash
wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && ./dashboard/collect.sh | jq -e '.runners | length'"
```

Expected: `6`. A `jq -e` failure means malformed JSON — fix before building any UI on top.

- [ ] **Step 3: Verify the busy/idle detection against reality**

Compare the collector's `state` for each runner against `docker logs --tail 1`. If a runner is genuinely mid-job, it must report `busy`. **This is the field most likely to be subtly wrong** — a naive "last line contains Running job" is fooled by a completed job whose completion line scrolled past `--tail`. Test with a real job in flight if possible, and state in the report whether you were able to.

- [ ] **Step 4: Commit**

```powershell
cd d:/docker-compose/GithubRunners
git add dashboard/collect.sh
git commit -m "feat: dashboard status collector"
```

---

### Task 8: Dashboard — page and container

**Files:**
- Create: `dashboard/index.html`
- Create: `dashboard/serve.sh`
- Modify: `compose/docker-compose.runners.yml` (add the `dashboard` service)

**Interfaces:**
- Consumes: `status.json` from Task 7.

- [ ] **Step 1: Write the page**

A single self-contained `dashboard/index.html` — no CDN, no external fonts, no build step. It fetches `status.json` every 5 seconds and re-renders.

Requirements:
- One card per runner: name, registration name, busy/idle with an obvious visual difference, current job and its duration when busy.
- CPU and memory as labelled bars with the numeric value beside them — a bar alone is not readable at a glance.
- Build cache per runner against the 20 GB cap, so it is visible when GC is doing its job.
- A fleet-wide disk bar: used vs the 1 TB ceiling.
- Dark and light via `prefers-color-scheme`.
- A visible "last updated" timestamp, and an obvious stale/error state if the fetch fails. **A dashboard that silently shows stale data is worse than one that shows an error** — this is the single most important UI behaviour here.

- [ ] **Step 2: Add the dashboard service to Compose**

```yaml
  dashboard:
    image: python:3.12-alpine
    container_name: runner-dashboard
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./dashboard:/app:ro
      - ./dashboard/data:/data
    ports:
      - "9200:9200"
    working_dir: /app
    command: sh -c "apk add --no-cache docker-cli jq bash coreutils && ./serve.sh"
```

Note the socket is mounted **read-only** and this is the **runners' own engine**, not Docker Desktop — the dashboard can see the runners and cannot see BeastStack. That property is the reason this is safe; state it in a comment in the file.

- [ ] **Step 3: Write `serve.sh`**

Loop the collector into `/data/status.json` every 5 seconds in the background, and serve `/app` and `/data` over HTTP on 9200. Keep it to `python3 -m http.server` plus a background loop — no framework.

- [ ] **Step 4: Start it and verify end to end**

```powershell
wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f compose/docker-compose.runners.yml up -d dashboard"
Start-Sleep -Seconds 30
curl.exe -s http://localhost:9200/data/status.json | Select-String "runners"
```

Expected: JSON with six runners. Then open `http://localhost:9200` and confirm it renders.

- [ ] **Step 5: Verify the failure mode**

Stop the collector loop or corrupt `status.json`, and confirm the page shows a clear stale/error state rather than silently displaying old numbers. Restore afterwards.

- [ ] **Step 6: Commit**

```powershell
cd d:/docker-compose/GithubRunners
git add dashboard/ compose/docker-compose.runners.yml
git commit -m "feat: live runner dashboard on port 9200"
```

---

### Task 9: Tear down the old fleet (DESTRUCTIVE — requires explicit user approval)

**Do not execute without the user explicitly approving.** Until this runs, rollback is available.

**Files:**
- Modify: `docker-compose.yml` (retire the old replica-based definition)

- [ ] **Step 1: Confirm the new fleet has been healthy for a meaningful period**

Do not tear down on the same day the new fleet came up unless the user asks. Confirm all six have run real jobs and the dashboard shows them healthy.

- [ ] **Step 2: Present the reclaim figure and get approval**

Expected reclaim: **~673 GB** of writable layers, of which ~610 GB is confirmed orphaned `fuse-overlayfs` data unreachable by any prune (471 directories on disk versus 4 images the daemon knows about).

- [ ] **Step 3: Remove the old containers — scoped, never `--remove-orphans`**

```powershell
cd d:/docker-compose/GithubRunners
docker compose -f docker-compose.yml down
```

`docker compose down` without `-v` removes the containers this project defines. **Never add `--remove-orphans`** — it would target containers Compose does not recognise as part of this project, which on this host means BeastStack.

- [ ] **Step 4: Verify the reclaim and that BeastStack survived**

```powershell
docker ps -q | Measure-Object -Line          # expect 16 (BeastStack only)
docker exec immich_server df -h / | tail -1  # expect a large jump in free space
```

Expected: 16 containers, and free space up by several hundred GB.

- [ ] **Step 5: Retire the old Compose file**

Replace `docker-compose.yml` with a short stub pointing at `compose/docker-compose.runners.yml` and explaining that runners now live on the `github-runners` engine. Do not silently delete it — someone will look for it.

- [ ] **Step 6: Check for orphaned registrations**

Confirm the org lists exactly the six new runners plus `nomercy-mac-mini`. Remove any offline stragglers.

- [ ] **Step 7: Commit**

```powershell
cd d:/docker-compose/GithubRunners
git add docker-compose.yml
git commit -m "chore: retire the shared-engine runner stack"
```

---

### Task 10: Verify the whole thing, including the cap on the new engine

- [ ] **Step 1: Confirm the isolation still holds after migration**

Re-run Task 2's fill test now that the new engine is carrying real load. Expected: unchanged — Docker Desktop's free space does not move.

- [ ] **Step 2: Confirm the GC cap is enforcing, by measurement not config**

```powershell
1..6 | ForEach-Object { wsl -d github-runners -- docker exec github-runner-$_ docker buildx du }
```

Take two readings several minutes apart after builds have run. Expected: build cache trending toward and holding at ~20 GB. A prior task established that config presence proves nothing — only the falling number does.

- [ ] **Step 3: Confirm the janitor runs on the new engine**

After 6h+ of uptime:

```powershell
wsl -d github-runners -- docker logs github-runner-1 | Select-String "janitor"
```

Expected: at least one `sweep starting` / `sweep complete` pair.

- [ ] **Step 4: Confirm deregistration works on the new engine**

```powershell
wsl -d github-runners -- docker stop -t 30 github-runner-6
wsl -d github-runners -- docker logs --tail 20 github-runner-6 | Select-String "Received SIG|Server-side removal"
wsl -d github-runners -- docker start github-runner-6
```

Expected: `Received SIGTERM` and `Server-side removal … succeeded`. Confirms today's registration fix survived the migration.

- [ ] **Step 5: Record the outcome in the spec and commit**

Append a "Measured outcome" section with real figures: disk before/after, the reclaim, the isolation test result, and the fleet's steady-state usage on the new engine.

---

## Rollback

Rollback is available at every stage until Task 9:

```powershell
# Stop the new fleet
wsl -d github-runners -- bash -lc "cd /mnt/d/docker-compose/GithubRunners && docker compose -f compose/docker-compose.runners.yml down"
# Restart the old one
1..6 | ForEach-Object { docker start githubrunners-github-runner-$_ }
```

The old containers are only *stopped* in Task 5, never removed until Task 9 — that is deliberate and is what makes this migration reversible. `scripts/start.sh` is unchanged by this plan, so the old fleet comes back exactly as it is today, carrying the Phase A cap, the janitor, and both registration fixes.

After Task 9 the rollback is a rebuild rather than a restart, which is why Task 9 is gated.
