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

Every GitHub runner the platform builds - Linux, Windows and macOS - now
checks where a job's code comes from in its job-started hook, which the
runner starts before any step, checkout included.

### The rule: trust the source, not the person

Like npm's trusted publishers, what is trusted is the repository the code
comes from, not who sent it. A job whose event payload has a `pull_request`
object (`pull_request`, `pull_request_target`, `pull_request_review`,
`pull_request_review_comment`, and any other event whose payload carries one)
**runs only when the pull request's head repository is owned by**:

- the base repository's owner - NoMercy-Entertainment
  (`base.repo.owner.login`); or
- an account in `RUNNER_TRUSTED_OWNERS` (the owner's value:
  `NoMercy-Entertainment,Fill84`).

The owner is `head.repo.owner.login`, or else the part of
`head.repo.full_name` before the `/`, compared without case. **Everything
else is refused, and the job ended**: an outsider's fork, an org member's or
an outside collaborator's own fork alike, and a head repository deleted since
(`head.repo` null). Who opened the pull request, their `author_association`,
and who pushed play no part.

The job fails with

    ::error title=Outside code refused::This pull request comes from
    mallory/app, and these self-hosted runners only run code from
    repositories owned by NoMercy-Entertainment or a trusted owner. Push the
    branch to the NoMercy-Entertainment repository instead.

and nothing else is done for it: no Android SDK restore, no disk check, no
cleanup; the job is ended (below).

**Everything else runs**: push, release, workflow_dispatch, schedule,
workflow_run, an `issue_comment` (whose payload carries no head), and a pull
request from a branch of an org repository or of a trusted owner's. Only
someone with write access to an org repository can cause an event without a
pull request. Each job prints one line:

    Origin: pull_request from NoMercy-Entertainment/app by bob — allowed
    Origin: push from NoMercy-Entertainment/app by bob — allowed

### Giving someone permission

**Give them write access to the org's repository, and have them push their
branch there instead of to a fork.** Their pull request then comes from the
org's own repository and runs. There is no list of people: a collaborator is
trusted exactly as far as the repository's write permission says. A trusted
*owner* is for an account whose own repositories should run here as if they
were the org's - `Fill84` today.

`RUNNER_TRUSTED_OWNERS` is set in the controller's environment
(`/etc/runner-platform/controller.env`): account names separated by commas or
spaces, compared without case, default empty (then only the base
repository's owner is trusted). The controller gives it to every GitHub unit
it creates (`dashboard/control/agent_runtime.py`): the Linux unit's container
environment, Windows' `reg\unit.json`, the macOS launchd job's
`EnvironmentVariables`. Forgejo units get nothing. **A change reaches a
runner only through a create**: an idempotent create rewrites the unit's
environment, and the running service or launchd job reads it when it starts
again; on Linux the unit is made anew. It is not a secret: every job on the
runner can read it.

### A refusal ends the job

**A failing hook alone does not stop a job.** The runner treats the
job-started hook as a step: when it fails, the runner still runs every later
step whose `if:` is `always()`, `failure()` or `!cancelled()`
(actions/runner `StepsRunner`) - and a pull request from a fork brings its
own workflow file, so it could mark `actions/checkout` and a `run:` step
`always()` and have its code run after the refusal. So after a refusal the
hook **kills the runner's `Runner.Worker` process**, the one that runs this
job and nothing else:

1. the `::error` is printed and flushed, and the hook's record written;
2. the hook waits `RUNNER_GUARD_KILL_DELAY` seconds (5 by default, at most
   30) while the runner, waiting for the hook, has nothing else running, so
   the line can reach GitHub's log;
3. it finds `Runner.Worker` among its own parents and kills it: on Linux
   `SIGKILL`, from `/proc/<pid>/status` (`PPid`) and the process's `comm` or
   first argument; on Windows `TerminateProcess`, from a Toolhelp snapshot,
   refusing a "parent" created after its child (Windows reuses the pid of a
   dead parent); on macOS `kill -9`, from `ps -o ppid=,comm=,args=`.

It kills a process only when the chain is exactly what the runner builds: at
most the hook's own helpers (its Python, Linux's `timeout`), then the hook
itself (a shell whose arguments name `job-started.sh`, or on Windows the
runner's node), and **directly above it** a process whose file name is
exactly `Runner.Worker` (`.exe` on Windows; on macOS the node of the
runner's `macos-run-invoker.js` may stand between). Never `Runner.Listener`,
which takes the next job, and never anything else - so a test of the hook run
inside a job kills nothing. When there is no such worker, or it cannot be
killed, the hook prints `::warning title=Runner guard::the job could not be
ended ...; steps the workflow marks always() may still run`, and still
fails.

The listener should see its worker die, report the job as failed and take
the next job. The job-completed hook does not run for a killed job; the next
job's cleanup does its work.

**What is proven, and what is not.** Proven here: on Windows, with copies of
node named `Runner.Listener.exe` and `Runner.Worker.exe` starting the real
`job-started.js` with an outside fork's event, `Runner.Worker.exe` is
terminated before anything after the hook runs and `Runner.Listener.exe`
carries on (`agent/tests/test_windows_job_hooks.py`); on macOS, the `ps`
walk and the kill against a table of real processes under Git's bash
(`agent/tests/test_macos_job_hooks.py`). The same proof for Linux, with bash
under the names `Runner.Listener` and `Runner.Worker` and the real
`job-started.sh`, is `agent/tests/test_unit_job_started.py -k
ends_runner_worker`; it needs Linux `/proc` and has not been run yet (the
test says how to run it in a throwaway container). **Not proven**: a real
runner's process tree, how much of the job's log GitHub shows when the worker
dies, what the listener reports for the job, and that it takes the next one.
Watch the first refused fork pull request on each platform.

### Fail-safe

The check refuses only what it has positively identified. A fault of our own
never stops every job:

- no `GITHUB_EVENT_PATH`, a file that cannot be read, JSON that does not
  parse or is not an object, a pull request without a readable head, or one
  whose head or base owner the payload does not say:
  `::warning title=Runner guard::could not read the event; origin not
  checked`, and the job runs;
- a guard that cannot be loaded, an exception inside it, or (macOS) no node
  to read the event with: a `::warning title=Runner guard::...` and the job
  runs.

A missing `GITHUB_EVENT_NAME` alone does not skip the check: the payload
decides, and the line says "an unnamed event".

Values printed from the payload keep only the characters GitHub allows in a
login or a repository name; anything else becomes `?`, so a payload can
never end the line or start a workflow command of its own. Names are
compared as they are, never as printed. The rule reads only the fields it
needs, never the pull request's title or body.

### Where it runs

| | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Hook | `/runner/job-started.sh` | `reg\hooks\job-started.js` | `job-started.sh`, in `/Library/Nomercy/runner-hooks/<runner_id>` for a system launchd job, else in `reg/hooks` |
| The check | `/runner/runner_guard.py`, loaded first by `job_started.py` | `reg\hooks\runner_guard.py`, the Linux file byte for byte, loaded first by `runner_disk.py` | `runner_guard.js` beside it, a line-for-line port, run first by `lib.sh` |
| Reads the event with | Python | the agent's Python (`RUNNER_HOOK_PYTHON`) | node: `RUNNER_HOOK_NODE` when set; else the newest `$RUNNER_REG_DIR/externals/node*/bin/node` (the runner's own); else the newest `externals/node*/bin/node` beside the hooks' directory; else `node` on `PATH` |
| Ends the job with | `SIGKILL` | `TerminateProcess` | `kill -9` |
| Records its last run in | `RUNNER_LOG_DIR/origin-guard.json` | the same | the same |

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
runner's process environment (the unit's, with `RUNNER_TRUSTED_OWNERS`)
does. **Not yet seen on a live runner**: the first real job after rollout
must show the `Origin:` line.

### Tampering

The check runs as the runner's own account, before the job, from files on
the runner. A job the check *allowed* is trusted code, but nothing stops
trusted code from being careless or compromised, so the hook's files are
kept out of a job's reach where the platform allows it, and checked where
it does not:

- **Windows**: every create denies the runner's service account every kind
  of write, delete, re-permission and take-over on `reg\hooks` (inherited by
  each file) and on `reg\unit.json` (which names the hooks and
  `RUNNER_TRUSTED_OWNERS`). LocalSystem, the agent, still rewrites them.
- **macOS**, system launchd job: the hooks are root's
  (`/Library/Nomercy/runner-hooks/<runner_id>`, 0755 and 0644), installed
  with the same `sudo install` as the system plist. In a user's own launchd
  domain there is no privileged path: the hooks stay in `reg/hooks`, which
  that user - and so a job - can change, as it can its own plist.
- **Linux**: a job is root in a privileged unit. Nothing in the unit is out
  of its reach.

On every platform the runner's own software stays the runner account's: an
allowed job could still replace `Runner.Worker`, the node that runs the
macOS check, or (Linux) Python itself, and so run without the check. What
covers that is the next part: the agent looks, and the dashboard says.

### On the dashboard

On each deep pass (about every five minutes) the agent reports for every
GitHub unit (`agent/origin_guard.py`):

- **the version** of the check its hook carries, 0 when the hooks are
  missing, from before the check, or changed. Linux: the unit image's
  `LABEL nomercy.origin_guard`, and `docker diff` for any change since the
  unit was made to `runner_guard.py`, `job_started.py`, `job-started.sh` or
  `run`. Windows and macOS: every hook file in the directory the runner's
  `unit.json` or launchd job names must be the agent's own copy - so after an
  agent deploy that changes the hooks, runners made before it read as changed
  until their next create;
- **its last run**, from `origin-guard.json` in the runner's log directory:
  allowed, refused, unread (its event could not be read) or failed, and
  when. A record left by another version of the check is ignored.

The controller keeps each report with the time it was measured. The fleet
heading says, in plain words: "Runner group Fillz: every repository in
NoMercy-Entertainment may use these runners, public ones included", then
"Pull requests from forks outside NoMercy-Entertainment and trusted owners
are refused by the runner".

- **Green** only when every runner in the fleet is *proven*: it carries the
  check, unchanged, and its last job read its event.
- **Red** when public repositories may use the runners and a runner is
  *missing* the check (no hooks, old ones, changed ones) or *unknown* (no
  report in 20 minutes: a stopped guest, an agent that cannot read it):
  "refused by 1 of 3 runners; recreate the others".
- **Amber** otherwise: no public repositories, no runners, or runners that
  carry the check but have not run a job with it yet, or whose last job's
  event could not be read - each said on the heading, with the runners'
  notes on hover.

None of this is proof against a determined job: the report and the record
are the runner's own files. It tells when the check is absent, old, changed
or not working, which is what drift and mistakes look like.

Raise `GUARD_VERSION` in `runner_guard.py` and `runner_guard.js` and the
`nomercy.origin_guard` label together when the rule changes; tests hold them
equal.

### What it does not cover

- **A trusted owner's repository runs whatever is in it**, and so does a
  branch of an org repository: anyone with write access there is trusted.
- **`workflow_run`** carries no `pull_request` object; a workflow it starts
  runs the base repository's workflow file, and runs. `pull_request_target`
  from an untrusted owner's repository is refused even though its workflow
  file is the base branch's, because such workflows commonly check the fork
  out.
- **Forgejo runners** have no job hooks. Forgejo's own approval for pull
  requests from forks is what stands in front of them.
- **A runner the platform did not build** - one installed by hand, or an
  adopted macOS instance, which keeps its own launchd job - has none of
  these hooks; it shows as unknown on the heading.

### Rolling it out

Nothing here reaches a running runner by itself.

1. **Controller and dashboard**: deploy, with
   `RUNNER_TRUSTED_OWNERS=NoMercy-Entertainment,Fill84` in `controller.env`,
   so units are created with it and the fleet headings can say what the
   units report. Restart the dashboard for its own copy of the setting.
2. **Linux**: build a new overlay image with `Dockerfile.cleanup` (the
   running fleet's path, see `images/linux/unit/README.md`); it carries
   `runner_guard.py` and the `nomercy.origin_guard` label. Make it the
   fleet's unit template, redeploy the Linux agent (it reports the check),
   and **recreate** each GitHub Linux runner - one at a time, only when idle
   (`runner-platform.md` 3.2).
3. **Windows**: redeploy the agent on each worker that runs GitHub runners.
   Then an **idempotent create** of each existing, registered runner - the
   same create, driven again - rewrites its hooks (the runner reads them
   afresh for every job, so the check applies from its next job), its
   `unit.json` with `RUNNER_TRUSTED_OWNERS`, and the deny entries, without
   re-registering it; **restart its service** afterwards so the runner runs
   with the new environment. A recreate does all of it.
4. **macOS**: redeploy the appliance's agent, then the same idempotent create
   and a **stop and start** (a launchd job reads its plist when it is
   loaded), or a recreate. A system launchd job's runner moves its hooks to
   `/Library/Nomercy/runner-hooks/<runner_id>` on that create: the agent's
   sudo rights must allow `/usr/bin/install -d` and `/bin/rm -f` as root.
5. **Prove it with real jobs** on each platform: a push shows `Origin: push
   from ... — allowed` in "Set up runner". A pull request from a personal
   fork (any account but the org and Fill84) on a scratch public repository
   shows the refusal - and **no later step runs, `always()` ones included**,
   and the runner is online for the next job. Watch the fleet heading turn
   green as each runner's report comes in.
