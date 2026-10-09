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

## Outside code

The runner group `Fillz` lets every repository in NoMercy-Entertainment use
the self-hosted runners, public ones included, and 26 public repositories do.
A pull request from a fork runs the fork's code - its workflow file as well -
so anyone who opens one could run anything on these machines. Until this
check, GitHub's "Approve and run" click (every public repository asks it of
outside contributors) was the only thing in the way.

The owner's rule: code from an org member or a known maintainer runs; code
from anyone else never runs on these runners. Every GitHub runner the
platform builds - Linux, Windows and macOS - now enforces it in its
job-started hook, which the runner starts before any step, checkout
included.

### The rule

A job is **refused** when its event payload has a `pull_request` object
(the events `pull_request`, `pull_request_target`, `pull_request_review`,
`pull_request_review_comment`, and any other whose payload carries one) and
both of these hold:

- its head is a fork: `head.repo.full_name` is not the base repository's
  (compared without case), or `head.repo` is null - a fork deleted since;
- its author is not trusted: `author_association` is not `OWNER`, `MEMBER`
  or `COLLABORATOR`, and the author's login is not in
  `RUNNER_TRUSTED_AUTHORS`.

The job fails with

    ::error title=Outside code refused::Self-hosted runners only run code from
    NoMercy-Entertainment members and known maintainers. Pull request #7 by
    mallory (NONE) comes from fork mallory/app, so this job was stopped before
    any of its code ran. If mallory is a maintainer whose org membership is
    private, add them to RUNNER_TRUSTED_AUTHORS.

and nothing else is done for it: no Android SDK restore, no disk check, no
cleanup. The refusal takes the disk refusal's path (status 75, the hook's
exit 1).

**Everything else runs**: push, release, workflow_dispatch, schedule,
workflow_run, an `issue_comment` (whose payload carries no head), a pull
request from a branch of the repository itself, and a pull request from a
trusted author's fork. It prints one line:

    Origin: pull_request from fork alice/app by alice — allowed
    Origin: push from NoMercy-Entertainment/app by bob — allowed

### Trusted authors

`author_association` is GitHub's own word for the author's relation to the
repository. **A member whose org membership is private reads as
`CONTRIBUTOR`**, not `MEMBER`, to anyone who cannot see the membership - so
a private member's pull request from a fork is refused unless their login is
in `RUNNER_TRUSTED_AUTHORS`. The same list is for a maintainer who is not in
the org at all.

`RUNNER_TRUSTED_AUTHORS` is set in the controller's environment
(`/etc/runner-platform/controller.env`): GitHub logins, separated by commas,
compared without case, default empty. The controller gives it to every
GitHub unit it creates (`dashboard/control/agent_runtime.py`): the Linux
unit's container environment, Windows' `reg\unit.json`, the macOS launchd
job's `EnvironmentVariables`. Like any unit setting, a change reaches a
runner at its next create; on Windows the job host reads `unit.json` when its
service starts, and on macOS launchd reads the job when it is loaded. It is
not a secret: every job on the runner can read it.

### Fail-safe

The check refuses only what it has positively identified. A fault of our own
never stops every job:

- no `GITHUB_EVENT_PATH`, a file that cannot be read, JSON that does not
  parse or is not an object, or a pull request without a readable head:
  `::warning title=Runner guard::could not read the event; origin not
  checked`, and the job runs;
- a guard that cannot be loaded, an exception inside it, or (macOS) no node
  to read the event with: a `::warning title=Runner guard::...` and the job
  runs.

A missing `GITHUB_EVENT_NAME` alone does not skip the check: the payload
decides, and the line says "an unnamed event".

Values printed from the payload (logins, repository names) keep only the
characters GitHub allows in them; anything else becomes `?`, so a payload can
never end the line or start a workflow command of its own. The rule reads
only the fields it needs, never the pull request's title or body.

### Where it runs

| | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Hook | `/runner/job-started.sh` | `reg\hooks\job-started.js` | `reg/hooks/job-started.sh` |
| The check | `/runner/runner_guard.py`, loaded first by `job_started.py` | `reg\hooks\runner_guard.py`, the Linux file byte for byte, loaded first by `runner_disk.py` | `origin_check` in `reg/hooks/lib.sh`, first in `job_started` |
| Reads the event with | Python | the agent's Python (`RUNNER_HOOK_PYTHON`) | the runner's own node, `externals/node*/bin/node` beside the hooks (newest), else `RUNNER_HOOK_NODE`, else `node` on PATH |

`agent/tests/origin_cases.py` is the one table of events and answers all
three are tested against, so the rule and the wording cannot drift apart.

### The event the hook reads

From the runner's source (actions/runner, `JobHookProvider.RunHook` and
`ScriptHandler`): the runner writes the webhook payload to
`_temp/_github_workflow/event.json` before it starts the job-started hook,
and gives the hook the `github` context's variables, `GITHUB_EVENT_NAME` and
`GITHUB_EVENT_PATH` among them, overwriting any value of the same name. A
hook is started with an empty step environment, so a workflow's own `env:`
- which a fork controls in its pull request - does not reach it; the
runner's process environment (the unit's, with `RUNNER_TRUSTED_AUTHORS`)
does. **Not yet proven on a live runner**: the first real job after rollout
must show the `Origin:` line (see "Rolling it out" below).

### What it does not cover

- **A trusted author's fork runs whatever is in it**, commits by others
  included. Trust is in the author of the pull request.
- **`workflow_run`** carries no `pull_request` object; a workflow it starts
  runs the base repository's workflow file, and runs. So does
  `pull_request_target` from a trusted author; from anyone else it is
  refused even though its workflow file is the base branch's, because such
  workflows commonly check the fork out.
- **Someone with write access** pushes to the repository itself, not to a
  fork; that is the org's own code by this rule.
- **Forgejo runners** have no job hooks. Forgejo's own approval for pull
  requests from forks is what stands in front of them.
- **A runner the platform did not build** - one installed by hand, or an
  adopted macOS instance, which keeps its own launchd job - has none of
  these hooks.

### On the dashboard

Every GitHub unit reports, in each heartbeat, which version of the check its
**own** hook carries (`origin_guard`): on Linux the unit image's
`LABEL nomercy.origin_guard`, on Windows `GUARD_VERSION` in the runner's
`runner_guard.py`, on macOS `ORIGIN_GUARD_VERSION` in its `lib.sh`; 0 for
hooks from before the check. The runner's own copy, because hooks are
written at create: a redeployed agent or a rebuilt image says nothing about
a runner made before them.

The fleet heading says it in plain words: "Runner group Fillz: every
repository in NoMercy-Entertainment may use these runners, public ones
included", then "Outside pull requests are refused by the runner (only org
members and trusted maintainers run code)" - **green** only when every
runner in the fleet reports the check. **Red** when public repositories may
use the runners and not every runner refuses ("refused by 1 of 3 runners;
recreate the others"). **Amber** otherwise. Raise `GUARD_VERSION` in
`runner_guard.py`, `ORIGIN_GUARD_VERSION` in `lib.sh` and the
`nomercy.origin_guard` label together when the rule changes; tests hold them
equal.

### Rolling it out

Nothing here reaches a running runner by itself.

1. **Controller and dashboard**: deploy, with `RUNNER_TRUSTED_AUTHORS` in
   `controller.env` if anyone needs it, so new units are created with it
   and the fleet headings can say what the units report.
2. **Linux**: build a new overlay image with `Dockerfile.cleanup` (the
   running fleet's path, see `images/linux/unit/README.md`), make it the
   fleet's unit template, and **recreate** each GitHub Linux runner - one at
   a time, only when idle (`runner-platform.md` 3.2). The heading turns
   green when every unit is made from the new image.
3. **Windows and macOS**: redeploy the agent on each worker (step 1 of the
   disk hooks' rollout above). The hook files are rewritten by every create,
   and the runner reads them afresh for every job, so an **idempotent
   create** on an existing, registered runner puts the check in place for
   its next job without re-registering it. `RUNNER_TRUSTED_AUTHORS` is in
   the unit's environment, which the running service or launchd job read
   when they started: it needs a restart after that create (on macOS a
   reload of the launchd job, which a stop and start does), or a recreate.
   A recreate does all of it.
4. **Prove it with real jobs**: a push shows `Origin: push from ... —
   allowed` in "Set up runner". A pull request from a fork by an account
   that is not a member shows the refusal - try it on a scratch public
   repository first. Read the result counts in history afterwards.
