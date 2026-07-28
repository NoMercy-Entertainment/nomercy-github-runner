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
| Needs admin/root | no | **yes** | only if you enable auto-login |
| Survives an unattended reboot | no | yes | only with auto-login |
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

**Reboots are a question the installer asks you.** The launchd service the
runner installs is a user LaunchAgent, and those load at login rather than at
boot, so a Mac that reboots with nobody logged in comes back with no runners.
Answer yes to the auto-login question and it comes back on its own; answer no
and it does not. Either way the installer tells you which you chose, at the
summary and again at the end. See [Reboots on macOS](#reboots-on-macos).

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
- **Whether to enable auto-login** (macOS), so the runners return after an
  unattended reboot. Defaults to no, and it explains the trade before asking.

Nothing is created until you confirm the summary.

Useful flags when scripting it (`--non-interactive`):

| Flag | Why |
|---|---|
| `--min-free N` | Lower the free-space floor from the default 40 GB |
| `--skip-space-check` | Proceed regardless. Interactive runs can already answer "use it anyway"; this is the same escape hatch for unattended ones |
| `--group ""` | The org default, stated deliberately. Distinct from omitting the flag, which prompts |
| `--auto-login` / `--no-auto-login` | macOS. Answer the reboot question without a prompt |
| `--login-password-stdin` | macOS. Reads the login password from stdin for `--auto-login`. There is deliberately no `--password` flag: `argv` is readable through `ps` by any local user and lands in shell history |

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
registered as a launchd service. With auto-login enabled, also a watchdog
LaunchAgent (`tv.nomercy.runner-watchdog`, script under your chosen path) and a
root LaunchDaemon (`tv.nomercy.autologin-health`) that reports when auto-login
has stopped being able to work.

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

**macOS: reboots are covered only if you asked for it.** `svc.sh` produces a
user LaunchAgent, which loads at login rather than at boot, so a Mac that
reboots with nobody logged in comes back with no runners. The installer offers
auto-login to fix that and defaults to no. See the section below for what it
does and what it costs.

---

## Reboots on macOS

A LaunchAgent loads at user login. A Mac that reboots unattended therefore comes
back with every runner down and nothing in any log saying why, because the thing
that would log it did not start either.

A LaunchDaemon is the obvious fix and the wrong one. The runner requires
`runsvc.sh` as its entry point, and a daemon runs outside the GUI session, so it
loses the keychain, code-signing identities and simulators an Xcode runner needs.
It would look installed and quietly fail the work.

So the installer offers to make the login happen instead:

```bash
./nomercy-github-runners-setup.sh --auto-login
# or, unattended:
./nomercy-github-runners-setup.sh --auto-login --login-password-stdin <<<"$pw"
```

Say yes and three things are installed:

| | What it does |
|---|---|
| Auto-login | The Mac logs itself in at boot, so the LaunchAgents load exactly as they would for a human |
| `tv.nomercy.runner-watchdog` | A LaunchAgent, every 5 minutes, that starts any runner agent that is not loaded. It only ever starts things and finds them by globbing, so it is safe beside runners it did not install and covers ones added later |
| `tv.nomercy.autologin-health` | A root LaunchDaemon that checks auto-login is still able to work and reports when it is not |

**Auto-login is a real security decision.** Anyone who can power-cycle the
machine gets a logged-in session. It also requires FileVault to be **off** — the
installer checks and refuses rather than leaving you believing reboots are
covered. Say no and everything else still installs; the runners simply stay down
after a reboot until somebody logs in.

**Why the health daemon exists.** The watchdog is a LaunchAgent, so it needs the
session auto-login exists to create. If auto-login itself breaks — a macOS update
clearing the preference, someone turning FileVault on — the watchdog is not
running to notice. The daemon runs at boot outside any session, writes an hourly
heartbeat, and logs loudly when any of the three preconditions is gone:

```bash
tail /var/log/nomercy-autologin-health.log
/usr/bin/log show --predicate 'eventMessage BEGINSWITH "nomercy-autologin:"' --last 1d
```

Spell out `/usr/bin/log`: `log` is a zsh builtin that takes no arguments, so the
bare command fails with `too many arguments` in the default macOS shell.

It never repairs anything. Repairing auto-login needs the login password, and a
root daemon holding one would be worse than the fault it reports.

**`sysadminctl -autologin` is broken on macOS 26.** It sets the user preference
and then fails the credential with `SACSetAutoLoginPassword error:22`, so
auto-login looks configured and does not work. The installer checks both halves —
the preference and `/etc/kcpassword` — and writes the credential itself when
`sysadminctl` did not.

The uninstaller removes the watchdog and the health daemon. It leaves auto-login
alone deliberately, since that is a machine-level setting you may want for other
reasons, and prints the two commands that undo it.

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
not be told apart from omitting it.

**The two macOS-specific fixes were re-run on that Mac on 2026-07-28.** Pointing
the installer at a group that does not exist surfaced the real reason from
`config.sh` rather than telling anyone to re-run it by hand:

```
  config.sh reported:
    √ Connected to GitHub
    Could not find any self-hosted runner group named "no-such-group".
[FAIL] Could not configure Mac-mini-van-Stoney-runner-1.
```

and re-running with `--count 2` over a single existing runner added capacity
instead of aborting:

```
  1 runner(s) already configured here - leaving them alone.
  [ ok ] runner-1 already configured, skipped
  [ ok ] Installed and started Mac-mini-van-Stoney-runner-2
```

**Auto-login and the reboot pieces were tested on the same Mac**, by rebooting
it. Auto-login took (`/dev/console` owned by the user), all four runner agents
on the machine loaded — the two the installer created and two it had never
touched — the health daemon ran at boot with exit 0, and a real job then ran on
a runner that had come back on its own:

```
runner name : Mac-mini-van-Stoney-runner-1
workspace   : /Users/stoney/RunnerInstallerTest/runner-1/_work/nomercy-ci/nomercy-ci
uptime      : up 6 mins, 1 user
console     : crw--w--w-  1 stoney  staff  0 Jul 28 04:24 /dev/console
```

Teardown removed both runners, the watchdog and the health daemon, and left no
orphaned registrations in the organisation.
