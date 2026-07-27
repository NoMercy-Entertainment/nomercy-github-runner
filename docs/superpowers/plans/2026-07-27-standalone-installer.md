# Standalone Runner Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a guided installer and matching uninstaller so any developer can stand up NoMercy runners on Windows, Linux or macOS, with runner storage at a path they choose and standalone from their own Docker.

**Architecture:** Two self-contained scripts. `nomercy-github-runners-setup.ps1` builds a dedicated WSL2 distro with its own Docker engine. `nomercy-github-runners-setup.sh` branches: on Linux it installs a second `dockerd` with its own data root as a systemd unit; on macOS it installs the runner natively under launchd with configurable paths and no Docker isolation. Both are wizards, both are matched by an uninstaller that deregisters from GitHub before removing anything.

**Tech Stack:** PowerShell 5.1-compatible (ASCII-only), POSIX shell, WSL2, systemd, launchd, Docker Engine, GitHub Actions runner v2.336.0.

**Spec:** `docs/superpowers/specs/2026-07-27-standalone-installer-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Never assume the operator's machine looks like this one.** No hardcoded `D:\`, no assumption Docker Desktop exists, no assumption of a particular distro or shell. Everything discovered or asked.
- **Nothing is created before the confirmation step.** A wizard that half-installs on Ctrl-C is worse than one that refuses to start.
- **The uninstaller deregisters from GitHub first**, before removing containers. Reversing that order orphans registrations, which was observed repeatedly in this repository's own migration.
- **Never delete the operator's data directory without explicit confirmation**, and show its size first. Default to keeping it.
- **PowerShell files stay ASCII-only.** Windows PowerShell 5.1 reads BOM-less `.ps1` as ANSI, and a stray em dash breaks string parsing — this cost real debugging time here.
- **Every `docker stop` passes an explicit `-t`.** Engine 29.x sets `StopTimeout=1`, which kills runner deregistration mid-flight.
- The PAT is never echoed, never written to a log, never passed where `ps` can see it if avoidable.
- Do not modify anything under `D:\docker-compose\BeastStack\`.
- Branch: `standalone-installer`. Commit after each task.

---

### Task 1: Shared wizard scaffolding and preflight (Windows)

**Files:**
- Create: `install/nomercy-github-runners-setup.ps1`

**Interfaces:**
- Produces: a script that runs preflight and the full prompt sequence, prints the summary, and **exits before creating anything**. Later tasks fill in the actions.

- [ ] **Step 1: Write preflight checks**

Detect and report, refusing with a clear message rather than a stack trace:

```powershell
# WSL2 present and version 2
wsl.exe --status
# WSL emits UTF-16; decode it or string matching silently fails
```

Checks: Windows build supports WSL2; `wsl.exe` present; virtualization enabled; at least 40 GB free at the intended path; whether a distro named `nomercy-runners` already exists (refuse, point at the uninstaller).

- [ ] **Step 2: Write the prompt sequence**

Org, PAT (hidden input), runner group, count, labels, CPU/memory limits, storage path, disk ceiling, dashboard yes/no and port. Each prompt: shown default, validation, re-ask on invalid.

The storage prompt must show free space on the chosen volume and refuse if below the threshold. This prompt is the whole point of the installer — a silent default landing on the wrong drive is the problem being solved.

- [ ] **Step 3: Validate the PAT against the real API before proceeding**

```powershell
# A token that cannot mint a registration token will fail at the last step
# after minutes of setup. Fail in the first 10 seconds instead.
Invoke-RestMethod -Method Post -Uri "https://api.github.com/orgs/$Org/actions/runners/registration-token" -Headers @{Authorization="Bearer $Token"}
```

Expected: 201 with a token. On 401 say "bad token", on 403 say "token lacks admin:org", on 404 say "organisation not found or no access" — not the raw error.

- [ ] **Step 4: Print the confirmation summary and exit**

Every choice, storage path and its free space prominent. Then `exit 0` with "dry run complete — install actions land in Task 2".

- [ ] **Step 5: Verify it is ASCII-only and parses under PowerShell 5.1**

```powershell
# 5.1 specifically: it is what a scheduled task and a default shell will use
$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('install\nomercy-github-runners-setup.ps1',[ref]$null,[ref]$e); $e
```

Expected: no errors. And no non-ASCII bytes:

```bash
grep -P '[^\x00-\x7F]' install/nomercy-github-runners-setup.ps1
```

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add install/nomercy-github-runners-setup.ps1
git commit -m "feat: Windows installer wizard scaffolding and preflight"
```

---

### Task 2: Windows install actions

**Files:**
- Modify: `install/nomercy-github-runners-setup.ps1`

**Interfaces:**
- Consumes: the answers collected in Task 1.
- Produces: a working fleet on a dedicated WSL distro.

- [ ] **Step 1: Create the distro at the chosen path**

```powershell
wsl.exe --install Ubuntu-24.04 --name nomercy-runners --location "<chosen path>" --no-launch
```

Then **verify the vhdx actually landed there** and halt if not — the isolation requirement is unmet otherwise, and continuing would build on a wrong foundation.

- [ ] **Step 2: Enable systemd and install Docker Engine**

Write `/etc/wsl.conf` with `[boot] systemd=true`, terminate, wait for `systemctl is-system-running`, then install `docker-ce` from Docker's apt repo.

Pass the installer as a **file**, not an inline string: quoting a bash script through PowerShell into `wsl.exe` mangles `$(...)` and redirections. Strip CRLF with `tr -d '\r'` before running — the file comes from a Windows filesystem and a CRLF shebang makes the kernel fail to find the interpreter.

- [ ] **Step 3: Verify dockerd survives a distro restart**

```powershell
wsl.exe --terminate nomercy-runners
# then wait for docker info to succeed with no manual start
```

This is what makes the keepalive task viable. If the daemon does not come back on its own, the runners will not return after a reboot.

- [ ] **Step 4: Install the keepalive scheduled task**

Logon-triggered, running as the invoking user (WSL distros are per-user, so a SYSTEM task cannot see it), `ExecutionTimeLimit` unlimited, absolute path to `powershell.exe`.

State in the output that runners return after a reboot only once someone logs in, and how to switch to a startup trigger if unattended recovery is wanted.

- [ ] **Step 5: Create the runner containers**

Fetch `start.sh` from the repository at the pinned ref into the distro's native filesystem. Create N containers directly via the Docker API — `--privileged`, `--restart unless-stopped`, `--stop-timeout 60`, tmpfs `/tmp`, `start.sh` mounted read-only, env from the wizard answers.

- [ ] **Step 6: Verify the fleet registered**

Poll until every runner logs `Listening for Jobs`, or fail with the daemon log after a timeout. Then confirm against the GitHub API that exactly N new runners are online.

- [ ] **Step 7: Optionally install the dashboard**

If chosen: build the image from the repository sources and run it on the chosen port, socket mounted read-write.

- [ ] **Step 8: Print next steps**

Dashboard URL, how to see the runners (`wsl -d nomercy-runners -- docker ps`), where the data lives, and how to uninstall.

- [ ] **Step 9: Commit**

---

### Task 3: Linux installer — wizard, preflight and root check

**Files:**
- Create: `install/nomercy-github-runners-setup.sh`

**Interfaces:**
- Produces: the shared wizard plus a Linux branch that stops before creating anything.

- [ ] **Step 1: Platform detection and dispatch**

```sh
case "$(uname -s)" in
  Linux)  PLATFORM=linux ;;
  Darwin) PLATFORM=macos ;;
  *) echo "Unsupported: $(uname -s)"; exit 1 ;;
esac
```

- [ ] **Step 2: Root check on Linux**

Installing a systemd unit needs root. Check early and explain, rather than failing at the write:

```sh
if [ "$PLATFORM" = linux ] && [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "This needs root to install a systemd unit. Re-run with: sudo $0 $*"
  else
    echo "This needs root to install a systemd unit, and sudo was not found."
  fi
  exit 1
fi
```

Do NOT silently re-exec under sudo — the operator should make that choice knowingly.

- [ ] **Step 3: Linux preflight**

systemd present (`systemctl --version`); Docker engine binaries present or offer to install; the chosen path exists or can be created; free space; no existing `nomercy-runners-docker.service`.

**Warn if the chosen path is on the same filesystem as the system Docker root.** The daemons are separate but the disk is not, so the isolation is only logical — the operator should know before proceeding, since that is precisely the guarantee they came for.

- [ ] **Step 4: Shared prompt sequence**

Same questions as Windows, same validation, same PAT check against the real API, same "nothing is created yet" summary-and-exit.

- [ ] **Step 5: Verify with shellcheck and a POSIX shell**

```bash
shellcheck install/nomercy-github-runners-setup.sh || true
sh -n install/nomercy-github-runners-setup.sh
```

Expected: `sh -n` clean. Address shellcheck warnings that indicate real bugs; note any deliberately ignored.

- [ ] **Step 6: Commit**

---

### Task 4: Linux install actions — isolated dockerd

**Files:**
- Modify: `install/nomercy-github-runners-setup.sh`

- [ ] **Step 1: Write the systemd unit for a second daemon**

Every path distinct from the system daemon, or the two will fight:

```ini
[Unit]
Description=NoMercy runners Docker engine
After=network-online.target

[Service]
ExecStart=/usr/bin/dockerd \
  --data-root <CHOSEN>/data \
  --exec-root <CHOSEN>/exec \
  --pidfile <CHOSEN>/docker.pid \
  --host unix://<CHOSEN>/docker.sock \
  --containerd /run/containerd/containerd.sock
Restart=always

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Start it and confirm it is genuinely separate**

```sh
docker -H "unix://<CHOSEN>/docker.sock" info --format '{{.DockerRootDir}}'
docker info --format '{{.DockerRootDir}}'   # system daemon, if any
```

Expected: different roots. Then confirm the isolated daemon lists **zero** containers — proof it cannot see the operator's.

- [ ] **Step 3: Fill test — prove the isolation**

Allocate a large file inside the isolated data root, confirm the system Docker's free space is unchanged, delete it. This is the requirement demonstrated rather than asserted, and it is cheap.

Skip with a clear message if both roots share a filesystem (already warned at preflight) — the test would fail for a known and accepted reason.

- [ ] **Step 4: Create the runner containers on that socket**

Same spec as Windows, addressed via `-H unix://<CHOSEN>/docker.sock`.

- [ ] **Step 5: Verify registration, optionally install the dashboard, print next steps**

- [ ] **Step 6: Commit**

---

### Task 5: macOS install actions — native runner under launchd

**Files:**
- Modify: `install/nomercy-github-runners-setup.sh`

**Interfaces:**
- Consumes: the shared wizard. macOS skips the disk-ceiling and dashboard prompts.

- [ ] **Step 1: macOS preflight**

Version 11+, architecture (arm64 vs x64 picks the tarball), chosen path writable, free space. Do **not** require Docker — a macOS runner doing Xcode work does not need it.

- [ ] **Step 2: Say plainly that macOS gets no Docker isolation**

Before the confirmation step, not buried in a log afterwards:

> On macOS the runner installs natively and its data lives under the path you
> chose. There is no separate Docker storage pool — if a workflow uses Docker
> it will use whatever Docker is installed on this machine, sharing its
> storage. Xcode-based workflows are unaffected.

Silence here would imply a guarantee the installer does not provide.

- [ ] **Step 3: Install N runners under the chosen path**

Download `actions-runner-osx-<arch>-2.336.0.tar.gz`, extract to `<chosen>/runner-N`, configure each with a distinct name against the org.

- [ ] **Step 4: Register each as a launchd service**

Use the runner's own `./svc.sh install` and `./svc.sh start`, which handles the plist. Do not hand-write plists — `svc.sh` is the supported path and the runner requires `runsvc.sh` as the entry point.

- [ ] **Step 5: Verify**

`./svc.sh status` for each, plus the GitHub API showing them online.

- [ ] **Step 6: Commit**

---

### Task 6: Uninstallers

**Files:**
- Create: `install/nomercy-github-runners-uninstall.ps1`
- Create: `install/nomercy-github-runners-uninstall.sh`

- [ ] **Step 1: Deregister from GitHub FIRST**

Before removing anything. Read each runner's registration name, obtain a **removal** token from `POST /orgs/{org}/actions/runners/remove-token` — not a registration token, which is a different credential and silently fails — and remove it.

For any that fail, list them and tell the operator to remove them manually, with the URL. Do not fail silently: this repository accumulated orphaned offline registrations exactly this way.

- [ ] **Step 2: Stop and remove the runners**

Containers: `docker stop -t 60` then `rm`. macOS: `./svc.sh stop && ./svc.sh uninstall`.

The `-t 60` is load-bearing given `StopTimeout=1`.

- [ ] **Step 3: Remove the engine and services**

Windows: remove the scheduled task, then `wsl --unregister nomercy-runners`. Linux: stop and disable the unit, remove the unit file, `systemctl daemon-reload`. macOS: nothing beyond step 2.

- [ ] **Step 4: Ask before deleting data — show its size first**

```
The runner data directory is:
  <path>   (184 GB)

Delete it?  [y/N]
```

Default **No**. The operator chose this path and it may not be exclusively ours.

- [ ] **Step 5: Verify the uninstall is complete**

Report what was removed and what remains. Confirm the org no longer lists the runners.

- [ ] **Step 6: Commit**

---

### Task 7: Documentation and end-to-end verification

**Files:**
- Create: `install/README.md`
- Modify: `README.md`

- [ ] **Step 1: Write install/README.md**

Per platform: prerequisites, the one-line download-and-run command, what it will ask, what it creates and where, how to uninstall. State the macOS isolation caveat in the macOS section, not only in the script.

- [ ] **Step 2: Fix the stale claim in the main README**

`README.md` states the container mounts the host Docker socket. It does not, and has not for some time — a reader could make a bad architectural decision on that. Correct it while documenting the new architecture.

- [ ] **Step 3: End-to-end test on Windows**

The only platform available here. Install to a **scratch path**, with a **distinct distro name** and **1 runner**, so it cannot collide with the live fleet. Confirm it registers, runs a job, and appears in the org. Then run the uninstaller and confirm the runner is deregistered, the distro is gone, and the scheduled task is removed.

**This test must not touch the existing `github-runners` distro or the six live runners.** Use different names throughout and verify before starting.

- [ ] **Step 4: Record what could not be tested**

Linux and macOS paths cannot be executed from this machine. Say so explicitly in the README and in the commit — an untested script presented as verified is worse than one honestly labelled. Note what a first Linux/macOS operator should watch for.

- [ ] **Step 5: Commit**

---

## Rollback

Nothing in this plan modifies the existing fleet. The installers are new files; the only edits to existing files are documentation. If an installer misbehaves during testing, its own uninstaller cleans up, and the scratch names keep the blast radius away from the running runners.
