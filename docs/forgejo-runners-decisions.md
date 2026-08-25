# Decisions taken while adding Forgejo runners

Companion to [the design](superpowers/specs/2026-08-25-forgejo-runners-design.md)
and [the migration runbook](forgejo-runner-migration.md). This records the
decisions made during implementation that the code alone does not explain —
particularly the ones where the implementation plan turned out to be wrong and
was overruled against the design.

The design is the authority. Where the plan and the design disagreed, the
design won; several of the entries below are exactly that.

## Where the plan was wrong

**An unrecognised Forgejo status read as "idle".** The plan's `_forgejo_job_state`
returned `idle` for any status that was not `active`. A status word the code did
not know — a future Forgejo release, a typo — would therefore have licensed a
cache prune against a runner mid-build. `is_idle()` gates prune and the drain
watcher, and the design's rule is that "unknown" never collapses into "idle".
Now `idle` and `offline` read idle; everything else reads unknown.

**The runner script installed a SIGTERM trap and then `exec`ed.** A custom trap
does not survive `exec`, so deregistration could never run. `scripts/start.sh`
documents this exact bug at its own `stop_runner` as the reason it does not
`exec` — the plan reintroduced a failure this repository had already paid for.
The Forgejo script now runs the daemon as a child and stays alive as PID 1.

**`set -euo pipefail` defeated the wait loop that replaced it.** A `wait`
interrupted by a trapped signal returns 128+signum, which errexit treats as
fatal, so the stopped-vs-exited check never ran and `deregister` ran twice.
The wait is now inside an `if`, which suspends errexit.

**The shutdown handler was trapped on EXIT as well as TERM and INT, and exited
itself** — so it re-entered through its own exit and reported a hard-coded 0
regardless of signal. Split now, mirroring `start.sh`: signal handlers that exit
with a real code, and a separate non-exiting EXIT fallback.

**`RUNNER_PID` was read by handlers installed before it was assigned.** A signal
in that window hit `set -u` and exited 1 without signalling the daemon. Declared
before the traps now, as `start.sh` does.

**The build-cache cap used `defaultKeepStorage`,** deprecated in the Docker
version running here. A deprecated key the daemon ignores means no cap at all —
the failure that once filled a 1 TB disk. Uses the `policy`/`maxUsedSpace` shape.

**`daemon --config "$DATA/config.yaml"` pointed at a file nothing creates.** The
plan claimed a missing config file falls back to defaults. It does not: the
daemon exits with `invalid configuration`, and under `restart: unless-stopped`
first boot would have been an infinite restart loop. Verified against the real
binary. The flag is gone.

**`forgejo-runner unregister` does not exist.** The script called it inside
`timeout ... || true`, so its absence was silent, while the comment claimed it
covered the container being stopped by anything other than the dashboard. There
is no such subcommand. Deregistration is the dashboard's job through
`DELETE /api/v1/user/actions/runners/{id}`; a Forgejo runner container stopped
by anything else leaves an `offline` entry to delete in Forgejo's own UI.

**`pending_enrichment` filtered on `ended_at IS NOT NULL`.** A Forgejo run has no
end until the enrichment sweep fetches one, so every Forgejo run would have been
invisible to the only thing that could close it — the history would have shown
starts and nothing else, for ever.

## Where the design was wrong

**The runner endpoints were built against Forgejo's admin surface, not the
owner's own.** `forgejo_api.py` called `/api/v1/admin/actions/runners` and its
siblings — the obvious reading of "manage runners" against Forgejo's API, and
wrong. Queried against the live instance it returned an empty runner list,
which read as "nothing registered yet" and nearly stood as the explanation.
Reading Forgejo's own database settled it: the runners already registered
there all carry `owner_id = 1` (the user `fill`) — they are user-scoped,
which is exactly why the admin endpoint saw none of them. The right
conclusion was "wrong endpoint", not "recreate the runners as global".

The owner is who caught it, by saying the Forgejo runners should be no more
widely available than the GitHub ones, which are scoped to a single org.

A runner registered through the admin path is available to every repository
and every user on the instance. This instance has open registration
(`DISABLE_REGISTRATION=false`), so a global runner would run jobs pushed by
strangers on hardware the owner controls — the exact thing the GitHub side's
org scoping already exists to prevent. Every admin call `forgejo_api.py` made
(`runner_statuses`, `runner_ids`, `registration_token`, `delete_runner`) now
hits Forgejo's user-scoped equivalent under `/api/v1/user/...` instead.
`find_task`'s repository endpoint was never admin-scoped and did not change.
The config key was renamed `FORGEJO_ADMIN_TOKEN` → `FORGEJO_API_TOKEN` to
match: it no longer needs, and must never be minted with, admin scope.

## What the tests could not see

**`forgejo-runner` logs to stderr; the GitHub runner logs to stdout.** Every log
read in `docker_ops` used only stdout, so for a Forgejo runner it returned the
three lines of shell echo from the start script and nothing else. The busy card
showed no job name, and — far worse — `logs_since` fed the history parser an
empty log, so **no Forgejo run was ever recorded**. Measured on the live
deployment after two real jobs had completed: `stdout 3 lines, 0 task lines;
stderr 10 lines, 2 task lines`, and `rows by provider: [('github', 2105)]`.

339 tests, five fix rounds, a whole-branch review and a scoped re-review all
missed it, because every test hands the parsers ready-made log text and so never
crosses the stream split. One real job made it visible in seconds.

Log reads now go through `_docker_logs`, which merges the two streams at the OS
level with `stderr=subprocess.STDOUT` rather than concatenating them afterwards,
so ordering survives. The lesson generalises: a stub that stands in for the thing
under test cannot fail the way the real thing does. The same shape produced the
`--config` and `unregister` defects above — both survived review by never being
run against the real binary.

## Deliberate behaviour changes

**`/api/recreate`, `/api/prune-all` and `/api/runner/add` now require a
`provider` and return 400 without one.** With two fleets on one engine, a
destructive endpoint that guesses its fleet acts on the wrong one. This is why
each fleet has its own action row rather than one global button above a mixed
grid.

Six pre-existing tests in `dashboard/tests/test_routes.py` were amended to send
`{"provider": "github"}`. Nothing else in them changed — no assertion, fixture
or name. That was the only edit to a pre-existing test file in this work, and it
was made deliberately rather than to make a failing test pass.

**The settings page gained per-fleet recreate buttons.** Once Forgejo
configuration lived on that page, a single "Save & recreate fleet" that
hardcoded GitHub meant editing `FORGEJO_RUNNER_LABELS` and recreating nothing
that used them.

**Forgejo result words are normalised at the API boundary** to the vocabulary the
history page already uses. `skipped` and the give-up case are kept distinct
rather than folded into the existing three, because each fold misreports —
inflating the success tile, inventing a failure, or asserting a cancellation
nobody made.

## Things that stayed as they were, on purpose

**The `gh_*` columns keep their names.** They now carry both forges. Renaming
them is a migration over a live history database for no functional gain.

**The provider comes from a container label, falling back to the name prefix.**
The eight `github-runner-N` containers in production predate the label. Without
the fallback the entire existing fleet would vanish from the dashboard until it
was rebuilt. A container must match both a known provider *and* that provider's
name prefix to be listed, so a stray label cannot enrol something into a fleet
whose destructive routes would then act on it.

**Forgejo runners are matched on `uuid`, never on name.** Forgejo's API documents
runner names as not unique.

## An incident

While testing the runner script's signal handling, the test was moved out of a
container "because the trap logic does not need Docker". True of the trap logic;
false of the rest of the script. `scripts/start-forgejo.sh` writes
`/etc/docker/daemon.json` and runs `pkill -9 dockerd` to set up its nested
daemon, so running it on the distro overwrote the distro's own Docker config
with one demanding `fuse-overlayfs` — a binary present only inside runner images
— and killed the daemon. Every runner and the dashboard were down for about
25 minutes.

The distro's dockerd deliberately has **no** `/etc/docker/daemon.json`; nothing
in `scripts/provision-distro.ps1` or `scripts/install-docker.sh` writes one, so
it runs on Docker defaults. If one appears there, it does not belong, and it is
the first thing to check when the fleet is down.

To exercise either start script, run it **inside** a container built from the
runner image, with `dockerd`, `docker` and the runner binary stubbed onto `PATH`
ahead of `/usr/bin`.

## Not yet done

No Forgejo runner has ever registered against the live instance from the distro.
The deployment in [the migration runbook](forgejo-runner-migration.md) has not
been performed. It needs a Forgejo API token with **repository** scope as
well as the user-scoped runner scope: `find_task` uses repository endpoints,
and a token missing repository scope makes busy/idle work and history look
fine while every run is silently closed as `Unknown` a day later.
