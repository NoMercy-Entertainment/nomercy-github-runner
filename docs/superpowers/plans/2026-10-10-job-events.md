# Job events: the runner says when a job starts and ends

Status: planned, not started. Agents and hooks are not to be changed until
the Windows ARM64 work in progress has landed and this plan has been agreed.

## Why

A card's job name is sampled: every 10 s the agent tails each unit's runner
log and matches "Running job: …" / "Job … completed with result" (agent/
jobs.py), and the name rides along in the next heartbeat. Between a late
beat and a strict freshness window, a busy card flipped between the job's
name and "running a job - the forge does not say which" (2026-10-10). The
clock fix of 7130441 stops the flicker, but the name is still up to ten
seconds late and still a guess from a log line.

The runner itself knows the exact moments. GitHub runs the job-started hook
before a job's first step and the job-completed hook after its last one, in
the job's own environment: `GITHUB_JOB`, `GITHUB_WORKFLOW`,
`GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`. Every GitHub
runner the platform builds already runs those hooks (GitHub #7), and the
hook already leaves a file the agent reads (`origin-guard.json`,
agent/origin_guard.py). Nothing new has to be invented, only joined up.

## What exists and is reused

| Piece | Where | Reused for |
| --- | --- | --- |
| job-started / job-completed hooks | images/linux/unit/runner/job_started.py, agent/hooks/windows/runner_disk.py, agent/hooks/macos/lib.sh | write the job record |
| hook → agent channel | the runner's log directory, read by the agent's deep pass (origin-guard.json) | the same directory, read on change |
| agent → controller events | agent/link.py `EventSender` → `/v1/event` (retried), controller `Receiver.take` → `operations.apply_event` | a new event kind |
| controller → page | app.py `_status_lock` generation + WebSocket snapshot loop (5 s wait) | woken on a job event |
| job name on the card | cards.py `_job` from telemetry `job` | filled from the event, sampled name as fallback |

## Design

### 1. The hook writes `job.json`

Both hooks, on every platform, write `<logs>/job.json` atomically (write a
sibling, rename):

```json
{"state": "started", "job": "ci / android", "repo": "NoMercy-Entertainment/nomercy-app-kmp",
 "run_id": "37229670936", "run_attempt": "2", "at": "2026-10-10T11:22:30Z"}
```

`job` is `GITHUB_WORKFLOW / GITHUB_JOB` (the shape the forge enricher
already matches on, history.py). The completed hook writes
`{"state": "finished", …same fields…, "at"}`. The hook never fails a job
over this file; a write error is a `::warning` at most (the same rule as
the disk check). Forgejo runners have no hooks and keep the sampled name.

### 2. The agent reads it on change and raises an event

A `JobWatch` thread per runtime, beside the heartbeat sampler:

- Linux: the log directory is a host volume (`/var/lib/runner-data/runners/<rid>/logs`),
  read directly, no `docker exec`. Windows: `D:\runners\<rid>\logs`. Both
  polled by `stat` every second (one syscall per unit; inotify is not worth
  a dependency). macOS: the guest is reached over SSH, so the file is read
  on the existing light beat (10 s) and the event raised then.
- On a changed mtime or content: post `{"kind": "job", "runner_id", "state",
  "job", "repo", "run_id", "run_attempt", "at"}` to `/v1/event` through the
  existing `EventSender` (three attempts).
- The heartbeat's telemetry keeps carrying the current job as well (from
  the file, else from the log tail as today), so a lost event is repaired
  within ten seconds instead of never.

### 3. The controller applies it

`operations.apply_event` is for an operation's progress; a `kind: "job"`
event is routed to `inventory.apply_job_event(host_id, body)`:

- the runner must be placed on `host_id` (the same fence as a heartbeat);
- `started`: telemetry `job`, `job_repo`, `job_run_id`, `job_at`; the spec's
  `forge_state` is not touched (the forge still decides busy/idle), but the
  card may say "busy" from the event when the forge's word is older;
- `finished`: `job` cleared, and history's run for this runner closed at
  `at` (history.py `close_run`), which is what the alarm monitor and the
  Discord "job finished" post (GitHub #9) can hang off later.
- audited as `verb=job`, decision `started`/`finished`, with repo and run.

### 4. The page hears it at once

The controller and the dashboard are two processes on one SQLite file. The
dashboard's WebSocket loop waits up to 5 s on `_status_lock`; add a check
of SQLite's `PRAGMA data_version` every second in `_controller_collector`,
and bump the generation when it changed. A job event then reaches every
open page within about a second, without the page polling the server.

### 5. The card

`cards._job`: the event's name wins; the sampled name is the fallback; a
busy runner with neither still says "running a job - the forge does not
say which". A finished event clears the name even while the forge still
says busy for a pass or two.

## Tests

- hook: writes the record from the environment on all three platforms;
  missing variables mean no file, never a failed job (agent/tests, the
  node and bash hook tests already exist).
- agent: `JobWatch` raises exactly one event per change, none on a
  rewrite with the same content, and a lost post is retried; the heartbeat
  still carries the job.
- controller: `apply_job_event` fences on host, starts and closes history
  runs, audits; a repeated event is idempotent.
- dashboard: the WebSocket loop sends a new snapshot within a second of a
  data_version change (test with a direct DB write).

## Rollout

1. Controller and dashboard image (steps 3-5): no agent needed; harmless
   before the agents send anything.
2. All four agents (step 2), one worker at a time, idle first.
3. The hooks (step 1): Linux by a `Dockerfile.cleanup` overlay and a fleet
   recreate; Windows and macOS by the idempotent create that rewrites a
   registered runner's hooks, then a restart (docs/operations/
   runner-job-hooks.md, "Rolling it out").
4. Prove it: a job's card shows its name within a second of "Set up job"
   and clears within a second of "Complete job".

## Not in this plan

- GitHub `workflow_job` webhooks would also give queued-but-unassigned jobs
  and the result; they need an inbound path to the dashboard (it is on the
  LAN only). Worth it later, through the tunnel the NoMercy API already
  provisions (GitHub #5 discussion).
- The job's result in the completed hook: GitHub does not pass it to the
  hook. The result keeps coming from the log line and the forge.
