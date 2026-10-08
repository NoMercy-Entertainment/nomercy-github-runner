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
| Cleared under 15 GB | earlier workspaces | earlier workspaces, Xcode DerivedData entries with nothing changed for two days, the runner's Gradle build cache (`$GRADLE_USER_HOME/caches/build-cache-*`) |
| Cleared after every job | `_work\_temp` | `_work/_temp`, Xcode DerivedData entries with nothing changed for two days |

What is never touched, on both: the current job's workspace (found under the
work directory or, on Windows, under its short junction alias, and matched
without case, since neither APFS nor NTFS cares how it is spelled), `_tool`,
`_actions`, `_temp` before a job (the runner has just emptied it, and what is
there is the job's event payload), `_temp/_runner_file_commands` after one
(the runner reads the hook step's file commands once it has finished), and
every dot entry (the runner account's profile: its `HOME` is under the work
directory). No link, junction or mount point is followed; one is removed as a
link. When the current workspace cannot be placed, no workspace is removed.

DerivedData is the account's, shared by every instance in a guest - the
appliance runs a GitHub and a Forgejo runner as the same user. An incremental
build writes deep inside an entry without touching the entry's own
directory, so an entry goes only when nothing anywhere in it changed for two
days, and a tree `find` cannot read all of is kept. When the agent runs
outside the guest and no `runner_user` is configured, the account's home is
not known: `RUNNER_HOOK_USER_HOME` is left unset and the account's
DerivedData is not touched.

## How a runner gets them

Only units the controller labels `nomercy.provider=github` get hooks.
**Forgejo runners have none**: forgejo-runner has no hook mechanism. They
keep the existing cleanup paths (the fleet's clear-cache, runbook 5.3).

- **Windows** (`agent/runtimes/windows_process.py`). Every create copies
  `agent\hooks\windows\job-started.js`, `job-completed.js`, `run_hook.js`
  and `runner_disk.py` from the agent's copy into the runner's own
  `reg\hooks\`, and writes their paths into the unit file's environment
  (`reg\unit.json`, read by the job host), with `RUNNER_HOOK_PYTHON`, the
  Python the job host runs under.
  - The hooks are `.js` because GitHub runs only `.js`, `.sh` and `.ps1`
    hooks. A `.js` one runs under the runner's own bundled node. A `.ps1` one
    is dot-sourced by PowerShell, which the ARM64 guest's execution policy
    refuses, and which takes minutes to start under emulation.
  - Each `.js` hands the work to `runner_disk.py` with that Python
    (`-X utf8`), which measures the volume with `GetDiskFreeSpaceEx` on the
    path itself.
  - The copies are in the runner's tree, not paths into the agent's folder,
    so an agent deploy, which swaps that folder, never leaves a job without
    its hook.
- **macOS** (`agent/runtimes/macos_appliance.py`, and pool guests through
  it). The guest has none of the agent's files, so every create writes
  `agent/hooks/macos/job-started.sh`, `job-completed.sh` and `lib.sh` into
  the runner's `reg/hooks/`, private to the runner's account, before the
  launchd job. Its `EnvironmentVariables` name both scripts, and
  `RUNNER_HOOK_USER_HOME` the account's own home, where Xcode keeps
  DerivedData: `/Users/<runner_user>`, or the agent's own home when the agent
  runs inside the guest. Plain bash 3.2.

## Rolling it out

1. Redeploy the agent on each worker that runs GitHub runners: the Windows
   x64 worker (`rnr-windows-1`) and the ARM64 guest
   (`infra\hyperv\Update-WindowsGuestAgent.ps1 -Name ...`), and the macOS
   appliance's agent.
2. **Recreate each GitHub runner.** The hooks are set only when a unit is
   made: `create` writes the hook files into the runner's tree and the
   environment that names them - on Windows `unit.json`, which the job host
   reads when its service starts, on macOS the launchd job. A runner that is
   only restarted keeps running without hooks. A recreate rebuilds one
   runner at a time, only when idle.
3. Prove it with a real job: the job's "Set up runner" step prints
   `Disk free before the job: ... GB`. A threshold set high in a test
   runner's environment (`RUNNER_DISK_FAIL_BELOW_GB=100000`) shows the
   refusal without filling a disk.
