# Adding runners to the NoMercy fleet

These installers set up self-hosted GitHub Actions runners on your machine and
add them to the organisation's fleet.

**The point of them:** the runners get their own Docker storage, separate from
whatever Docker you already run. A runaway build fills *their* disk, not yours,
and your own containers keep working. That failure is the reason these exist.

| | Windows | Linux | macOS |
|---|---|---|---|
| Runners run as | containers | containers | native processes |
| Storage isolation | own WSL distro + own Docker engine | second Docker daemon, own data root | **none** (see below) |
| Managed by | scheduled task | systemd | launchd |
| Needs admin/root | no | **yes** | no |
| Dashboard | optional | optional | not available |

---

## Windows

**Needs:** Windows 10 build 19041+ or Windows 11, with WSL2. Docker Desktop is
**not** required — the installer brings its own engine.

```powershell
# In PowerShell, in the folder you downloaded the script to:
Set-ExecutionPolicy -Scope Process Bypass -Force
.\nomercy-github-runners-setup.ps1
```

## Linux

**Needs:** systemd, Docker Engine installed, and root.

```bash
chmod +x nomercy-github-runners-setup.sh
sudo ./nomercy-github-runners-setup.sh
```

Root is needed to install a systemd unit. The script checks up front and tells
you rather than failing halfway; it will not silently re-run itself under
`sudo`.

## macOS

**Needs:** macOS 11 (Big Sur) or later. No root, no Docker.

```bash
chmod +x nomercy-github-runners-setup.sh
./nomercy-github-runners-setup.sh
```

**macOS gets no Docker isolation.** The runner installs natively and its data
lives wherever you choose, but if a workflow uses Docker it will use whatever
Docker is installed on the machine and share its storage. Xcode-based workflows
are unaffected. The installer says this again before it does anything.

---

## What it asks you

- **GitHub organisation** and a **personal access token** with `admin:org` (or
  `manage_self_hosted_runners`). The token is checked against the real API
  before anything is created, so a bad one fails in seconds rather than after
  several minutes of setup.
- **Runner group** — leave blank for the org default.
- **How many runners**, and what **labels** they should carry.
- **CPU and memory limits** per runner (Linux and Windows).
- **Where the runners store their data.** Always asked, never assumed. It shows
  free space on the volume you pick, because a default quietly landing on the
  wrong drive is the exact problem this installer exists to prevent.
- **Disk ceiling** (Windows and Linux).
- Whether to install the **dashboard**, and on which port.

Nothing is created until you confirm the summary.

Useful flags when scripting it (`--non-interactive`):

| Flag | Why |
|---|---|
| `--min-free N` | Lower the free-space floor from the default 40 GB |
| `--skip-space-check` | Proceed regardless. Interactive runs can already answer "use it anyway"; this is the same escape hatch for unattended ones |
| `--group ""` | The org default, stated deliberately. Distinct from omitting the flag, which prompts |

Re-running the installer over an existing install is safe: runners that are
already configured are left alone, so raising `--count` adds capacity rather
than failing.

## What it creates

**Windows** — a WSL distribution (default name `nomercy-runners`) at the path
you chose, containing its own Docker engine and your runner containers; and a
logon-triggered scheduled task that keeps the distribution alive.

**Linux** — `/etc/systemd/system/nomercy-runners-docker.service`, a second
Docker daemon with its data root under your chosen path, and your runner
containers on it.

**macOS** — a runner installation per runner under your chosen path, each
registered as a launchd service.

## Removing it

```powershell
.\nomercy-github-runners-uninstall.ps1        # Windows
```
```bash
sudo ./nomercy-github-runners-uninstall.sh    # Linux
./nomercy-github-runners-uninstall.sh         # macOS
```

It deregisters the runners from GitHub **first**, then removes the containers,
the engine and the service. It asks before deleting your data directory and
defaults to keeping it — you chose that path, and it may not be exclusively
ours.

If a runner cannot deregister itself, the uninstaller removes it directly
through the GitHub API. Pass `--org` and set `GH_TOKEN` (or `-Org`/`-Token` on
Windows) so it can. Any it genuinely cannot remove are listed by name at the
end, with the URL to remove them by hand — an orphaned runner that nobody
mentions is how organisations fill with dead entries.

---

## Known issues and things to watch

**Windows: runners return after a reboot only once you log in.** The keepalive
is a logon-triggered task, which avoids storing your Windows password. If the
machine needs to recover unattended, either switch the task to "run whether
user is logged on or not" (Windows will ask for the password) or enable
auto-login.

**Windows: WSL shuts down an idle distribution**, which stops Docker and kills
every runner. The keepalive task exists solely to prevent this. If runners
start cycling every 20-30 seconds, check the task is still running:
`Get-ScheduledTask -TaskName 'NoMercy Runners - Keep nomercy-runners Alive'`.

**Linux: a shared filesystem gives only partial isolation.** If the path you
choose is on the same filesystem as your existing Docker root, the daemons are
separate but the disk is not, and a runaway build could still fill it. The
installer warns you at the time; choose a path on another volume for real
isolation.

**macOS: runners do not survive an unattended reboot.** `svc.sh` only produces
a user LaunchAgent, which loads at login rather than at boot. Confirmed on
hardware. NoMercy-Entertainment/nomercy-ci#1 has a working fix (auto-login plus
a watchdog) that has not yet been ported here.

---

## What has actually been tested

Being straight about this, because an untested script presented as verified is
worse than one honestly labelled.

**Windows — fully tested**, end to end on this machine. Distro creation at a
chosen path, Docker engine install, engine surviving a distro restart,
keepalive task, runner registration, and complete removal including
deregistration.

**Linux — fully tested**, end to end inside a real systemd environment that
already ran Docker, which is the situation you will be in. Isolated daemon,
separate data root, runner registration, and complete removal. Isolation was
verified by observing two containers with the *same name* on the two daemons,
holding different IDs and separate layer stores.

**macOS — tested by @StoneyEagle on 2026-07-28** (Mac mini, Apple M4, macOS
26.5.2, arm64). Install, registration, real job execution and teardown all
work. The workspace landed under the chosen storage path, two runners installed
side by side without colliding, and the uninstaller left no orphaned
registrations across two runs.

Four bugs came out of that run and are now fixed: `config.sh` failures were
swallowed, re-running to add runners aborted instead of scaling, the free-space
floor was fatal in `--non-interactive` with no override, and `--group ""` could
not be told apart from omitting it. **Those fixes have not themselves been
re-run on macOS** — they are covered by tests on Linux where the code is
shared, but the macOS-specific paths (`config.sh` output capture, the
already-configured skip) are unexercised on Darwin.

Known and not fixed here: `svc.sh` produces a user **LaunchAgent**, so runners
do not return after a reboot until someone logs in. See
NoMercy-Entertainment/nomercy-ci#1 for a working approach (auto-login plus a
watchdog) and the traps involved.
