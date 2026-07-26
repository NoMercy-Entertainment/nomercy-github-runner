# Runner Disk Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop six Docker-in-Docker GitHub Actions runners from filling the Docker VM disk shared with the BeastStack production stack, by capping their inner build cache and periodically sweeping dead images and abandoned workspaces.

**Architecture:** Phase A is pure configuration, applied entirely through `scripts/start.sh` — which is already bind-mounted to `/root/start.sh`, so changes take effect on a plain container **restart** with no image rebuild and no container **recreation**. Two mechanisms: a BuildKit garbage-collection policy in the generated `/etc/docker/daemon.json` (caps build cache, the 99.9% consumer), and a backgrounded janitor loop (sweeps dead images, abandoned job workspaces, and diagnostic logs). Rollout is staged one runner at a time because a malformed `daemon.json` prevents `dockerd` from starting.

**Tech Stack:** Docker Desktop (WSL2) on Windows, Docker Compose, inner Docker 29.4.0 / BuildKit v0.29.0 with the `fuse-overlayfs` storage driver, bash.

**Spec:** `docs/superpowers/specs/2026-07-26-runner-disk-containment-design.md`

## Global Constraints

Every task's requirements implicitly include this section. These are hard.

- **Never run stack-wide or host-wide Docker commands.** No `docker system prune`, no `docker volume prune`, no `docker image prune` on the host, no `--remove-orphans`. BeastStack is production and holds the user's photo library, object storage and git server.
- **Every host command names its target explicitly** — `githubrunners-github-runner-N` — or is scoped with `-f d:/docker-compose/GithubRunners/docker-compose.yml`.
- **All prune operations run *inside* a runner** via `docker exec githubrunners-github-runner-N docker ...`. The inner daemon is a separate Docker installation with no socket to the host and cannot reach BeastStack.
- **Do not modify anything under `D:\docker-compose\BeastStack\`.** Its root `docker-compose.yml` is currently broken by uncommitted edits; that is not ours to fix and not a reason to touch it.
- **Do not recreate containers.** `docker restart` only. Recreation drops the writable layer and is the separately-gated destructive step (Task 7).
- **Do not run `docker compose up`** in this project during Phase A — Compose may decide to recreate containers to reconcile state.
- Branch: `runner-disk-containment`. Commit after each task.
- Target: ~20 GB build cache per runner, ~25-30 GB total per runner, ~180 GB fleet-wide (from 911 GB).

---

### Task 1: Capture the pre-change baseline

Without a baseline the verification in Task 6 proves nothing. `docker ps -s` walks every writable layer and takes several minutes on this host — run it in the background and collect it later.

**Files:**
- Create: `docs/superpowers/plans/baseline-2026-07-26.txt` (scratch record, committed for the audit trail)

**Interfaces:**
- Produces: a baseline file that Task 6 diffs against.

- [ ] **Step 1: Record host disk free space**

```bash
docker exec githubrunners-github-runner-1 df -h / | tee /tmp/baseline-df.txt
```

Expected: one `overlay` line near `1007G` total. Record the `Avail` column — at last measurement it was `32G` (97% used).

- [ ] **Step 2: Record inner-daemon usage for all six runners**

```bash
for i in 1 2 3 4 5 6; do
  echo "=== runner-$i ==="
  docker exec githubrunners-github-runner-$i docker system df
done 2>&1 | tee /tmp/baseline-systemdf.txt
```

Expected: six blocks. Reference values from 2026-07-26 — build cache 44-101 GB per runner, images 1.4-27 GB, **0 active** everywhere.

- [ ] **Step 3: Start the slow writable-layer measurement in the background**

```bash
docker ps -s --format "{{.Names}}\t{{.Size}}" > /tmp/baseline-ps-s.txt 2>&1 &
```

This takes minutes. Do not block on it. Reference values: runner-4 182 GB, runner-2 163 GB, runner-5 161 GB, runner-1 159 GB, runner-3 156 GB, runner-6 90 GB.

- [ ] **Step 4: Confirm all 22 containers are running, before touching anything**

```bash
docker ps -q | wc -l
```

Expected: `22`. **If this is not 22, stop and report** — something is already wrong and this plan assumes a healthy starting state.

- [ ] **Step 5: Commit the baseline**

```bash
cd d:/docker-compose/GithubRunners
cat /tmp/baseline-df.txt /tmp/baseline-systemdf.txt > docs/superpowers/plans/baseline-2026-07-26.txt
git add docs/superpowers/plans/baseline-2026-07-26.txt
git commit -m "docs: capture pre-change runner disk baseline"
```

---

### Task 2: Determine the correct GC config key (validate, do not guess)

Docker 29 deprecated `defaultKeepStorage` in favour of `reservedSpace` / `maxUsedSpace` / `minFreeSpace`. Writing the wrong key into `daemon.json` prevents `dockerd` from starting, which takes a runner offline. `dockerd --validate` checks a config file and exits **without touching the running daemon** — this is the safe way to find out.

**Files:**
- None modified. This task is pure discovery; its output determines the content written in Task 3.

**Interfaces:**
- Produces: the exact validated JSON `builder.gc` block that Task 3 writes into `scripts/start.sh`.

- [ ] **Step 1: Write the modern-syntax candidate into a scratch file inside runner-1**

```bash
docker exec githubrunners-github-runner-1 sh -c 'cat > /tmp/gc-modern.json <<EOF
{
  "storage-driver": "fuse-overlayfs",
  "features": { "containerd-snapshotter": false },
  "builder": {
    "gc": {
      "enabled": true,
      "policy": [
        { "maxUsedSpace": "20GB" }
      ]
    }
  }
}
EOF'
```

Deliberately minimal — one field, one policy entry. Every extra key is another chance to hit an unknown-field error, and a single `maxUsedSpace` entry is sufficient to cap the cache. Refinements can follow once the syntax is confirmed.

- [ ] **Step 2: Validate it — this does NOT restart or affect the running daemon**

```bash
docker exec githubrunners-github-runner-1 dockerd --validate --config-file /tmp/gc-modern.json
```

Expected on success: `configuration OK`
Expected on failure: an error naming the unknown field.

- [ ] **Step 3: If Step 2 failed, validate the legacy-syntax candidate**

Only run this if Step 2 did **not** print `configuration OK`.

```bash
docker exec githubrunners-github-runner-1 sh -c 'cat > /tmp/gc-legacy.json <<EOF
{
  "storage-driver": "fuse-overlayfs",
  "features": { "containerd-snapshotter": false },
  "builder": {
    "gc": {
      "enabled": true,
      "defaultKeepStorage": "20GB",
      "policy": [
        { "keepStorage": "2GB", "filter": ["unused-for=168h"] },
        { "keepStorage": "20GB", "all": true }
      ]
    }
  }
}
EOF'
docker exec githubrunners-github-runner-1 dockerd --validate --config-file /tmp/gc-legacy.json
```

Expected: `configuration OK`

- [ ] **Step 4: If BOTH failed, stop**

Do not proceed to Task 3. Report both error messages to the user. Capping via `daemon.json` is the load-bearing mechanism of this plan; if neither syntax validates, the design needs revisiting rather than improvising.

- [ ] **Step 5: Record which candidate won**

Note the winning JSON verbatim. Task 3 writes exactly this block — do not retype it from memory or mix the two syntaxes.

---

### Task 3: Add the GC policy to start.sh

**Files:**
- Modify: `scripts/start.sh:42-50` (the `daemon.json` heredoc)

**Interfaces:**
- Consumes: the validated JSON block from Task 2 Step 5.
- Produces: a `start.sh` whose generated `daemon.json` caps build cache. Task 4 adds the janitor to the same file; Task 5 restarts runner-1 to activate both.

- [ ] **Step 1: Read the current heredoc**

Read `scripts/start.sh` lines 33-50. The existing block writes `storage-driver` and the `containerd-snapshotter` feature flag. The comment above it (lines 36-41) explains why `fuse-overlayfs` is required — **preserve that comment**, it documents a real constraint about nested overlay mounts.

- [ ] **Step 2: Replace the heredoc body with the validated config**

Replace only the JSON between `cat > /etc/docker/daemon.json <<'EOF'` and the closing `EOF`. Using the modern syntax as the example (substitute the legacy block if that is what validated in Task 2):

```bash
cat > /etc/docker/daemon.json <<'EOF'
{
  "storage-driver": "fuse-overlayfs",
  "features": {
    "containerd-snapshotter": false
  },
  "builder": {
    "gc": {
      "enabled": true,
      "policy": [
        { "maxUsedSpace": "20GB" }
      ]
    }
  }
}
EOF
```

- [ ] **Step 3: Add an explanatory comment above the heredoc**

Insert after the existing storage-driver comment block, before `mkdir -p /etc/docker`:

```bash
# Build-cache GC: six independent daemons each hoarded every layer forever,
# reaching ~149 GB of /var/lib/docker/fuse-overlayfs per runner and filling the
# Docker VM disk shared with the BeastStack production stack. The policy below
# caps build cache at ~20 GB per runner and drops week-old entries aggressively.
```

- [ ] **Step 4: Verify the file is still valid bash**

```bash
bash -n d:/docker-compose/GithubRunners/scripts/start.sh
```

Expected: no output (exit 0). Any output means a syntax error — fix before continuing.

- [ ] **Step 5: Commit**

```bash
cd d:/docker-compose/GithubRunners
git add scripts/start.sh
git commit -m "fix: cap DinD build cache with a BuildKit GC policy"
```

---

### Task 4: Add the janitor loop to start.sh

BuildKit GC handles build cache only. Each runner also holds 1.4-27 GB of dead images, abandoned job workspaces, and unbounded `_diag` logs. The janitor is **inline in `start.sh`** rather than a separate `scripts/janitor.sh` because a new file would need a new bind mount, and adding a bind mount requires container *recreation* — the destructive step gated behind Task 7.

**Files:**
- Modify: `scripts/start.sh` (insert before the `register` call at line ~148)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a `janitor()` shell function, backgrounded before `exec ./run.sh`.

- [ ] **Step 1: Locate the abandoned workspaces before writing code that deletes things**

The three abandoned `actions_github_pages_*` workspaces were reported at ~1 GB but their exact location was not confirmed. Find them:

```bash
docker exec githubrunners-github-runner-1 sh -c \
  "find /root -maxdepth 4 -name 'actions_github_pages*' 2>/dev/null | head; \
   echo '--- _work layout ---'; \
   ls -la /root/actions-runner/_work 2>/dev/null"
```

Expected: paths under `/root/actions-runner/_work/`. Note whether they are direct children of `_work` or nested deeper — the `find` in Step 2 targets direct children only.

- [ ] **Step 2: Insert the janitor function**

Insert immediately before the `# ── Register ─` comment block (currently line 83):

```bash
# ── Janitor: keep disk usage bounded ────────────────────────────────────────
# BuildKit's GC policy (daemon.json above) caps build cache. This covers what
# GC does not: dead images, abandoned job workspaces, and runner diag logs.
# Runs every 6h. The first pass is delayed so a runner restarting into a queued
# job is not competing with the janitor for I/O.
JANITOR_INTERVAL=21600

janitor() {
  sleep "$JANITOR_INTERVAL"
  while true; do
    echo "[janitor] $(date -u +%FT%TZ) sweep starting"

    # Dead images in this runner's own daemon. The 72h floor deliberately
    # preserves recently-used base images so ordinary builds keep a warm cache.
    docker image prune -af --filter until=72h 2>&1 | tail -2

    # Abandoned job workspaces. An active job touches its workspace
    # continuously, so a 14-day mtime is a safe discriminator against live work.
    # Underscore-prefixed dirs (_tool, _temp, _actions) are runner-internal and
    # are left alone here.
    if [ -d /root/actions-runner/_work ]; then
      find /root/actions-runner/_work -mindepth 1 -maxdepth 1 -type d \
           ! -name '_*' -mtime +14 -print -exec rm -rf {} + 2>/dev/null || true
    fi

    # Runner diagnostic logs.
    find /root/actions-runner/_diag -type f -mtime +14 -delete 2>/dev/null || true

    echo "[janitor] $(date -u +%FT%TZ) sweep complete"
    sleep "$JANITOR_INTERVAL"
  done
}
```

- [ ] **Step 3: Background the janitor before the runner starts**

The last lines of the file currently read:

```bash
register
exec ./run.sh
```

Change to:

```bash
register

janitor &

exec ./run.sh
```

The janitor must start **after** `register` (so a registration failure exits before spawning it) and **before** `exec` (which replaces the shell — nothing after it runs).

- [ ] **Step 4: Verify the file is still valid bash**

```bash
bash -n d:/docker-compose/GithubRunners/scripts/start.sh
```

Expected: no output (exit 0).

- [ ] **Step 5: Dry-run the destructive find without deleting**

Confirm the workspace sweep would not match anything currently in use. Note this is `-print` only — **no `-delete`, no `-exec rm`**:

```bash
docker exec githubrunners-github-runner-1 sh -c \
  "find /root/actions-runner/_work -mindepth 1 -maxdepth 1 -type d ! -name '_*' -mtime +14 -print"
```

Expected: only genuinely stale workspaces, ideally the `actions_github_pages_*` ones found in Step 1. **If a repo currently building appears here, stop** and widen the mtime threshold.

- [ ] **Step 6: Commit**

```bash
cd d:/docker-compose/GithubRunners
git add scripts/start.sh
git commit -m "fix: add janitor loop for dead images, stale workspaces and diag logs"
```

---

### Task 5: Activate on runner-1 alone, and verify

A malformed config takes a runner offline. Proving it on one runner before touching the other five is the difference between one degraded runner and no CI at all.

**Files:**
- None. This task is execution and observation only.

**Interfaces:**
- Consumes: `scripts/start.sh` from Tasks 3 and 4.
- Produces: confidence to roll to the remaining five in Task 6.

- [ ] **Step 1: Confirm runner-1 is idle**

```bash
docker logs --tail 5 githubrunners-github-runner-1
```

Expected: `Listening for Jobs` as the last meaningful line. **If it shows `Running job:`, wait** — restarting mid-job fails someone's build.

- [ ] **Step 2: Restart runner-1 only**

```bash
docker restart githubrunners-github-runner-1
```

Note: `restart`, not `up`, not `recreate`. This preserves the writable layer and touches nothing else.

- [ ] **Step 3: Confirm the inner Docker daemon actually started**

```bash
sleep 30
docker exec githubrunners-github-runner-1 docker info --format '{{.ServerVersion}}'
```

Expected: `29.4.0`.
**On failure** — this is the scenario the staged rollout exists for:

```bash
docker exec githubrunners-github-runner-1 cat /var/log/dockerd.log | tail -30
```

Revert `scripts/start.sh` (`git revert` the Task 3 commit), restart runner-1, confirm recovery, and report. Do not proceed to Task 6.

- [ ] **Step 4: Confirm the GC policy is live**

```bash
docker exec githubrunners-github-runner-1 docker info 2>/dev/null | grep -A6 -i "gc policy\|builder"
```

Expected: the policy with the ~20 GB cap. If `docker info` does not surface it, confirm instead that the daemon read the file:

```bash
docker exec githubrunners-github-runner-1 cat /etc/docker/daemon.json
```

- [ ] **Step 5: Confirm the runner re-registered and is accepting work**

```bash
docker logs --tail 15 githubrunners-github-runner-1
```

Expected: a new random `nomercy-<5 chars>` name, successful registration, and `Listening for Jobs`. Registration is automatic — `start.sh:148` re-registers unattended with `--replace` on every boot.

- [ ] **Step 6: Confirm the janitor is running**

```bash
docker exec githubrunners-github-runner-1 sh -c "ps aux | grep -c '[s]leep 21600'"
```

Expected: `1`. The janitor sleeps 6h before its first pass, so no sweep output appears in logs yet — this is correct behaviour, not a failure.

- [ ] **Step 7: Confirm BeastStack is unaffected**

```bash
docker ps -q | wc -l
```

Expected: `22`.

- [ ] **Step 8: Commit nothing, report instead**

This task changes no files. Report to the user: daemon version, GC policy status, registration status, container count. **Get confirmation before Task 6.**

---

### Task 6: Roll out to the remaining five runners

**Files:**
- None. Execution and observation only.

**Interfaces:**
- Consumes: a verified-healthy runner-1 from Task 5.

- [ ] **Step 1: For each of runners 2, 3, 4, 5, 6 in turn — confirm idle, restart, verify**

One at a time. Do not loop blindly over all five; a failure on runner-2 must stop the rollout before runner-3.

```bash
# Repeat this block per runner, substituting N=2, then 3, 4, 5, 6
docker logs --tail 3 githubrunners-github-runner-N     # expect "Listening for Jobs", not "Running job"
docker restart githubrunners-github-runner-N
sleep 30
docker exec githubrunners-github-runner-N docker info --format '{{.ServerVersion}}'   # expect 29.4.0
docker logs --tail 5 githubrunners-github-runner-N     # expect registration + "Listening for Jobs"
```

**If any runner fails to bring up its daemon, stop the rollout** and report. The already-migrated runners keep working; the fleet degrades rather than dies.

- [ ] **Step 2: Confirm the full fleet is healthy**

```bash
for i in 1 2 3 4 5 6; do
  echo -n "runner-$i: "
  docker exec githubrunners-github-runner-$i docker info --format '{{.ServerVersion}}' 2>&1
done
```

Expected: `29.4.0` six times.

- [ ] **Step 3: Confirm all 22 containers are still running**

```bash
docker ps -q | wc -l
```

Expected: `22`. This is the constraint that matters most — BeastStack must be untouched.

- [ ] **Step 4: Record the post-change state**

```bash
for i in 1 2 3 4 5 6; do
  echo "=== runner-$i ==="
  docker exec githubrunners-github-runner-$i docker system df
done 2>&1 | tee /tmp/after-systemdf.txt

docker exec githubrunners-github-runner-1 df -h /
```

Compare against `docs/superpowers/plans/baseline-2026-07-26.txt`.

**Set expectations honestly:** immediately after restart, usage will look almost unchanged. BuildKit GC prunes lazily — it enforces the cap as new builds arrive, and the janitor's first pass is 6h out. The meaningful measurement is 24-48h later, in Task 8.

- [ ] **Step 5: Commit the results**

```bash
cd d:/docker-compose/GithubRunners
cp /tmp/after-systemdf.txt docs/superpowers/plans/after-phase-a-2026-07-26.txt
git add docs/superpowers/plans/after-phase-a-2026-07-26.txt
git commit -m "docs: record post-Phase-A runner disk state"
```

---

### Task 7: Immediate reclaim (DESTRUCTIVE — requires explicit user approval)

**Do not execute this task without the user explicitly approving it.** Present the options and wait.

Phase A caps *future* growth but does not by itself release the ~911 GB already committed. Two ways to reclaim it, in increasing order of risk.

**Files:**
- None.

**Interfaces:**
- Consumes: a fully-migrated fleet from Task 6.

- [ ] **Step 1: Present the two options to the user and wait for a decision**

**Option 1 — in-place prune (non-destructive, recommended first).** Every runner reports 0 active images and 0 running containers, so essentially the whole footprint is unused cache. Pruning inside each runner reclaims most of it **without recreating containers, without dropping registration, and without any risk to BeastStack** — the inner daemon cannot see it.

Caveat to state plainly: this frees space *inside* the container's writable layer, but a writable layer does not shrink on the host when files are deleted inside it. The space is reused by that runner rather than returned to the shared disk. It therefore stops further growth but may not immediately restore free space on the VM disk.

**Option 2 — recreate containers (destructive, fully effective).** Recreating drops the writable layer entirely and genuinely returns all ~911 GB to the shared disk. Registration survives automatically (verified in `scripts/start.sh:3-4`, `:148`, `:141-143`). Costs: any in-flight job is killed, and each runner re-downloads base images and rebuilds cache from cold, so the first builds after are slow.

- [ ] **Step 2: If Option 1 approved — prune inside each runner, one at a time**

```bash
# Repeat per runner, N=1..6. Confirm idle first.
docker logs --tail 3 githubrunners-github-runner-N     # expect "Listening for Jobs"
docker exec githubrunners-github-runner-N docker builder prune -af
docker exec githubrunners-github-runner-N docker image prune -af
docker exec githubrunners-github-runner-N docker system df
```

Note `docker exec ... docker ...` — these run against the **inner** daemon. Never run `docker builder prune` or `docker image prune` on the host shell.

- [ ] **Step 3: If Option 2 approved — drain, then recreate**

Drain first. Two runners were observed executing `jvm-android` jobs during investigation; do not kill mid-job.

```bash
# 1. Confirm every runner is idle
for i in 1 2 3 4 5 6; do echo -n "runner-$i: "; docker logs --tail 1 githubrunners-github-runner-$i; done

# 2. Recreate — scoped to this compose project by the -f flag. No --remove-orphans.
cd d:/docker-compose/GithubRunners
docker compose -f docker-compose.yml up -d --force-recreate

# 3. Verify registration and fleet health
for i in 1 2 3 4 5 6; do docker logs --tail 5 githubrunners-github-runner-$i; done
docker ps -q | wc -l    # expect 22
```

**`--remove-orphans` must never be added to that command.** It would target containers Compose does not recognise as part of this project.

- [ ] **Step 4: Verify the reclaim and confirm BeastStack survived**

```bash
docker exec githubrunners-github-runner-1 df -h /
docker ps -q | wc -l
```

Expected: substantially more free space, and `22`.

- [ ] **Step 5: Check for orphaned runner registrations**

Recreation deregisters via the `EXIT`/`TERM` traps, but an ungraceful kill can leave stale entries. Ask the user to check the org runner list at
`https://github.com/organizations/NoMercy-Entertainment/settings/actions/runners`
and remove any offline `nomercy-*` entries.

---

### Task 8: Confirm the cap holds (24-48h later)

The real test is whether usage plateaus after the runners have done real work. Phase B is only worth starting if Phase A proves insufficient.

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-runner-disk-containment-design.md` (record the measured outcome)

- [ ] **Step 1: Re-measure after at least 24h of normal CI activity**

```bash
for i in 1 2 3 4 5 6; do
  echo "=== runner-$i ==="
  docker exec githubrunners-github-runner-$i docker system df
done

docker exec githubrunners-github-runner-1 df -h /
```

Expected: build cache at or below ~20 GB per runner. Compare against baseline values of 44-101 GB.

- [ ] **Step 2: Confirm the janitor has run at least once**

```bash
docker logs githubrunners-github-runner-1 2>&1 | grep '\[janitor\]' | tail -10
```

Expected: at least one `sweep starting` / `sweep complete` pair. **If absent after 24h**, the janitor is not running — investigate before trusting the cap.

- [ ] **Step 3: Confirm writable layers have stopped growing**

```bash
docker ps -s --format "{{.Names}}\t{{.Size}}" | grep githubrunners
```

Slow (minutes). Compare against the baseline of 90-182 GB per runner.

- [ ] **Step 4: Record the outcome in the spec and decide on Phase B**

Append a "Measured outcome" section to the design doc with the real numbers. Then make an evidence-based call:

- Cap holding, usage plateaued → **Phase B is optional.** Say so rather than building it out of momentum.
- Usage still climbing, or a single job overshoots the cap badly → **Phase B is justified.** Its hard cap is the answer, and the spec's Phase B section is the starting point.

- [ ] **Step 5: Commit**

```bash
cd d:/docker-compose/GithubRunners
git add docs/superpowers/specs/2026-07-26-runner-disk-containment-design.md
git commit -m "docs: record measured Phase A outcome"
```

---

## Rollback

If anything goes wrong at any point, `scripts/start.sh` is bind-mounted and every change to it is a committed, revertible diff:

```bash
cd d:/docker-compose/GithubRunners
git log --oneline scripts/start.sh          # find the commit to undo
git revert <commit-sha>
docker restart githubrunners-github-runner-N    # per affected runner, one at a time
```

No image rebuild is needed. Nothing in Phase A modifies the image, the compose file, or any container's mounts — which is precisely why it is restart-only and reversible.
