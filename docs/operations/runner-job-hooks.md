# Job hooks on Windows and macOS runners (GitHub #7)

A full disk crashed a runner mid-release with nothing useful in its log. The
Linux unit guards against it with a hook before every job
(`images/linux/unit/README.md`). Windows and macOS GitHub runners now do the
same, with the same thresholds and the same wording, from scripts that ship
**with the agent**: no template, image or appliance change is involved.

## What the hooks do

Before each job, on the volume that holds the runner's work directory:

- under `RUNNER_DISK_CLEAN_BELOW_GB` (15) free, clear what is rebuildable;
- under `RUNNER_DISK_WARN_BELOW_GB` (10), print a `::warning`;
- under `RUNNER_DISK_FAIL_BELOW_GB` (3), fail the job with an `::error`,
  before the build can take the runner down with it.

The thresholds are in GB (1000^3) and can be set in the unit's environment.
Only that deliberate refusal fails a job. A fault in a hook, a missing
Python or `df` included, is a `::warning` and the job runs. GitHub waits for a
hook to finish; nothing in them is bounded by a timeout.

| | Windows | macOS |
| --- | --- | --- |
| Measured | the volume holding `RUNNER_WORK_DIR`: the runner's own VHD on x64 storage workers (mounted at its folder, which a drive letter would miss), the shared volume on ARM64 and plain-directory workers | `df -Pk` on `RUNNER_WORK_DIR`: the Data volume, never the sealed `/` |
| Cleared under 15 GB | earlier workspaces | earlier workspaces, Xcode DerivedData unused for two days, the runner's Gradle build cache (`$GRADLE_USER_HOME/caches/build-cache-*`) |
| Cleared after every job | `_work\_temp` | `_work/_temp`, Xcode DerivedData unused for two days |

What is never touched, on both: the current job's workspace (found under the
work directory or, on Windows, under its short junction alias), `_tool`,
`_actions`, `_temp` before a job (the runner has just emptied it, and what is
there is the job's event payload), `_temp/_runner_file_commands` after one
(the runner reads the hook step's file commands once it has finished), and
every dot entry (the runner account's profile: its `HOME` is under the work
directory). No link, junction or mount point is followed; one is removed as a
link. When the current workspace cannot be placed, no workspace is removed.

DerivedData is the account's, shared by every instance in a guest, so only a
project entry whose own directory has not changed for two days goes. That is
a judgement by modification time, which a build that leaves its entry's top
level untouched for two days would defeat; no build here runs that long.

## How a runner gets them

Only units the controller labels `nomercy.provider=github` get hooks.
**Forgejo runners have none**: forgejo-runner has no hook mechanism. They
keep the existing cleanup paths (the fleet's clear-cache, runbook 5.3).

- **Windows** (`agent/runtimes/windows_process.py`). The scripts are
  `agent\hooks\windows\job-started.ps1` and `job-completed.ps1` in the
  agent's installed copy (`C:\ProgramData\nomercy\agent\app\agent\hooks\windows\`).
  The runtime writes their paths into the unit file's environment
  (`reg\unit.json`, read by the job host), found from where the runtime
  itself runs, along with `RUNNER_HOOK_PYTHON`, the Python the job host runs
  under. GitHub runs only `.ps1`, `.sh` and `.js` hooks, so each `.ps1` hands
  the work to `runner_disk.py` with that Python, which measures the volume
  with `GetDiskFreeSpaceEx` on the path itself. On the ARM64 guest the hook
  waits for PowerShell to start under emulation, which can take minutes; it
  delays the job but cannot fail it.
- **macOS** (`agent/runtimes/macos_appliance.py`, and pool guests through
  it). The guest has none of the agent's files, so every create writes
  `agent/hooks/macos/job-started.sh`, `job-completed.sh` and `lib.sh` into
  the runner's `reg/hooks/`, private to the runner's account, before the
  launchd job. Its `EnvironmentVariables` name both scripts, and
  `RUNNER_HOOK_USER_HOME` the account's own home, where Xcode keeps
  DerivedData. Plain bash 3.2.

## Rolling it out

1. Redeploy the agent on each worker that runs GitHub runners: the Windows
   x64 worker (`rnr-windows-1`) and the ARM64 guest
   (`infra\hyperv\Update-WindowsGuestAgent.ps1 -Name ...`), and the macOS
   appliance's agent.
2. **Recreate each GitHub runner.** The environment is set only when a unit
   is made: on Windows the job host reads `unit.json` when its service
   starts, and `create` writes it; on macOS the hook files and the launchd
   job are written by `create`. A runner that is only restarted keeps
   running without hooks. A recreate rebuilds one runner at a time, only
   when idle.
3. Prove it with a real job: the job's "Set up runner" step prints
   `Disk free before the job: ... GB`. A threshold set high in a test
   runner's environment (`RUNNER_DISK_FAIL_BELOW_GB=100000`) shows the
   refusal without filling a disk.

On Windows the hook paths point into the installed agent. An agent redeploy
swaps `app` for `app.new` in a moment; a job that starts in that moment does
not find its hook. Redeploy while the runners are idle, as with any agent
change.
