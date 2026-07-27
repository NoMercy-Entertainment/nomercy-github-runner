# Standalone Runner Installer — Design

**Date:** 2026-07-27
**Status:** Design, pending approval
**Scope:** `D:\docker-compose\GithubRunners`. Produces installers other people
run on their own machines.

## Requirement

Any developer should be able to stand up NoMercy GitHub Actions runners on
their own machine with a single guided script, and remove them again with a
single script.

The runners' storage must be **standalone from whatever Docker the developer
already runs**, so a runaway build cannot fill their default Docker pool and
break their own containers. That is exactly the failure this repository already
suffered and fixed by hand; the installer packages that fix.

Stated by the user: *"the runners do not fillup the users default running
docker storage from the runners and that is breaks there running containers"*
and *"i dont want to setup the runners again, but i want that other users
(developers) easly can setup github-runners to expande our runners fleet."*

Docker is **not** being removed. Workflows keep using it — the fleet builds
Docker images (an `ffmpeg-base` image alone is 23.8 GB). What changes is that
the runners get their own Docker storage pool instead of sharing the
developer's.

## Platform support (verified 2026-07-27)

Runner v2.336.0 ships binaries for:

| OS | Architectures | Minimum version |
|---|---|---|
| Windows | x64, arm64 | 10/11, Server 2016+ |
| Linux | x64, arm64, arm32 | Ubuntu 20.04+, Debian 10+, RHEL 8+ |
| macOS | x64, arm64 | 11 Big Sur+ |

So all three platforms are worth supporting. Two installers:
`nomercy-github-runners-setup.ps1` (Windows) and
`nomercy-github-runners-setup.sh` (Linux and macOS, branching internally).

## Design — one idea, three implementations

The guarantee is the same everywhere: **runner storage lives at a path the
operator chooses and is not the developer's default Docker pool.** How that is
achieved differs by platform.

### Windows — dedicated WSL2 distro

A distro named `nomercy-runners` with its own `dockerd`, its virtual disk at
the chosen path. Runners are containers on that engine. This is the arrangement
already proven in this repository.

- Isolation: separate `ext4.vhdx`, separate daemon, separate image store.
- Ceiling: the vhdx maximum size.
- Persistence: a logon-triggered scheduled task holding the distro open.
- Prerequisite: WSL2. Docker Desktop is **not** required — the distro brings
  its own engine, which is what keeps it standalone.

### Linux — second dockerd with its own data root

A second daemon as a systemd unit, with `--data-root`, `--exec-root`,
`--pidfile` and socket all distinct from the system Docker.

- Isolation: separate data root on the chosen path, separate daemon and socket.
- Ceiling: whatever the chosen filesystem provides; the installer warns if the
  path shares a filesystem with the system Docker root, since the isolation is
  then only logical.
- Persistence: systemd.
- Prerequisite: root or sudo (required to install a unit), systemd, and the
  Docker engine binaries.

### macOS — native runners, configurable paths, no Docker isolation

Per the user's decision, macOS gets no isolation layer. Docker on macOS always
runs in a VM, and adding Colima as a hard dependency is not worth it for a
platform whose runners do Xcode work rather than container builds — the
existing `nomercy-mac-mini` is labelled `xcode,apple-silicon`.

- The runner installs natively and registers as a launchd service via the
  runner's own `svc.sh`.
- `_work`, `_tool` and the runner root all live under the chosen path, so the
  operator still controls where the data lands. That is the part of the
  requirement that matters here.
- If a workflow needs Docker on macOS it uses whatever Docker the developer
  has, unisolated. The installer says so plainly rather than implying a
  guarantee it does not provide.

### Summary

| | Windows | Linux | macOS |
|---|---|---|---|
| Runner form | container | container | native process |
| Isolation | WSL distro + own dockerd | second dockerd, own data-root | none (documented) |
| Service | scheduled task keepalive | systemd | launchd |
| Needs root | no (per-user WSL) | **yes** | no |
| Dashboard | yes | yes | no |

## The wizard

Interactive by default, non-interactive with flags or an answer file so it can
be scripted. Every prompt shows a sensible default and validates before moving
on.

1. **Preflight** — OS and architecture, required tooling, disk space at the
   chosen path, and whether an install already exists.
2. **GitHub** — organisation, personal access token (input hidden, validated by
   a real API call before proceeding), runner group.
3. **Runners** — how many, labels, CPU and memory limit per runner.
4. **Storage** — the data location. Defaulted per platform but always asked,
   because a default that silently lands on the wrong drive is the specific
   problem this exists to solve. Shows free space on the chosen volume and
   refuses a path that has too little.
5. **Disk ceiling** — Windows/Linux only.
6. **Dashboard** — install or not, and which port.
7. **Confirm** — a summary of every choice, with the storage path and its free
   space shown prominently, before anything is created.

Nothing is written until the confirmation step is accepted.

## The uninstaller

`nomercy-github-runners-uninstall.ps1` / `.sh`.

Order matters, and the first step is the one most likely to be skipped:

1. **Deregister every runner from GitHub.** Otherwise the org accumulates dead
   offline entries — observed repeatedly during this repository's own
   migration. Needs a token; prompts if not supplied.
2. Stop and remove the runner containers (or unload the launchd service).
3. Remove the isolated engine: unregister the WSL distro / remove the systemd
   unit / remove the runner installation.
4. Remove the scheduled task or service.
5. **Ask** before deleting the data directory, and show its size first.
   Defaults to keeping it — deleting a directory the user pointed at is
   irreversible and might not be exclusively ours.

## Details this must carry, learned the hard way

These come from debugging this repository and a naive installer gets them
wrong:

- **WSL shuts down an idle distro**, stopping Docker and SIGTERMing every
  container, which produced a register/SIGTERM/restart loop every ~20s.
  Enabling systemd does **not** prevent it. A live session must hold the distro
  open.
- **Scheduled tasks do not inherit `PATH`** — a bare `powershell.exe` in a task
  action fails with `0x80070002`.
- **Windows PowerShell 5.1 reads BOM-less `.ps1` as ANSI**, so non-ASCII
  characters break string parsing. Installer scripts stay ASCII-only.
- **Docker Engine 29.x creates containers with `StopTimeout=1`**
  (moby/moby#52775) instead of the documented 10. Runner deregistration needs
  ~3s, so every stop must pass an explicit timeout or registrations orphan.
- **The runner writes `.runner` with a UTF-8 BOM**, which strict JSON parsers
  reject.
- **`deploy.replicas` cannot be used** where runners need distinct storage —
  replicas receive identical mounts and multiple Docker-in-Docker daemons
  sharing a data root corrupt each other.

## Distribution

Each installer is a single self-contained file. It downloads what it needs
(runner tarball from GitHub releases, `start.sh` and dashboard sources from
this repository at a pinned ref) so the user experience is "download one file,
run it" rather than "clone this repo first".

Version pinning is explicit: the installer names the runner version it
installs, so a fleet does not silently drift, and bumping it is a one-line
change. This repository has already been bitten by GitHub deprecating a pinned
runner version, so the installer also warns when its pinned version is behind.

## Rejected alternatives

**Colima on macOS for parity.** Would give real isolation, but adds a hard
dependency for a platform whose runners do Xcode builds. Rejected by the user.

**A single cross-platform script.** PowerShell Core would run everywhere in
principle, but the platform mechanisms (WSL, systemd, launchd) share almost no
code, and requiring `pwsh` on Linux/macOS is a worse dependency than a shell
script.

**Requiring the repository to be cloned first.** Higher friction for the stated
goal of letting other developers set up runners easily.

**Bundling the runner tarball.** Would freeze the version at release time and
bloat the installer. Downloading a pinned version is better.

## Out of scope

- Changing how workflows use Docker. They keep working as they do.
- The dashboard on macOS — native runners are launchd services, not containers,
  and the dashboard is container-oriented.
- Automatic runner version upgrades.
- Anything under `D:\docker-compose\BeastStack\`.
