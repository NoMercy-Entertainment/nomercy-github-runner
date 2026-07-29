# Runner Detail View — Design

**Date:** 2026-07-28
**Status:** approved, ready for planning

## Goal

Clicking a runner card opens a dedicated page showing that runner's internal
state in depth — container configuration, its own Docker engine, its live
terminal output, and its job history — together with the lifecycle actions that
apply to it.

## Why

The dashboard grid shows eight fields per runner: name, registration, state,
job, uptime, CPU, memory, build cache. That is everything `collect()` returns
and it is enough to answer "is the fleet healthy". It cannot answer the
questions that actually cost time:

- Did `RUNNER_CPU_LIMIT` actually apply to this container, or is the setting
  being silently ignored?
- What is consuming this runner's 40 GB build cache?
- What is it doing right now, and why did the last job fail?
- Has this particular runner failed more often than its siblings?

Each of those currently requires shelling into the host and running docker by
hand. The data is already reachable through the socket the dashboard holds.

## Non-goals

Explicitly out of scope for this spec:

- **Per-runner configuration overrides** (CPU, memory, labels, group differing
  between runners). Deferred to a follow-up spec — it needs a storage model,
  because `create()` reads global `.env` today and every runner is identical by
  construction.
- **Maintenance actions** (prune this runner's cache, clear `_work`, force
  re-register). Same follow-up.
- **Interactive browser shell.** Considered and dropped. The runners are
  `--privileged`, so a browser terminal would convert a dashboard password
  compromise into a root prompt on the host — a different severity from every
  other control on the page, all of which can only do what the dashboard
  already knows how to do safely. `docker exec` from the operator's own
  terminal covers the same need without that exposure.
- **Cross-fleet federation** (issue #5). Unrelated.

## Architecture

### Route and page

```
GET /runner/<name>   ->  templates/runner.html
```

`<name>` is validated with the same rule `_target()` already applies —
`re.fullmatch(r"github-runner-\d+", name)` — then checked against
`list_runner_names()`. A name that fails either check renders a 404 page, not a
traceback.

Grid cards become links to this route. Their existing action buttons call
`stopPropagation()` so that pressing STOP does not also navigate.

The page header repeats what the card already displays — name, state badge,
registration, uptime — sourced from the `/api/status` payload the grid is
already polling. The page therefore paints immediately, before any
detail-specific request returns.

The footer carries the lifecycle actions, all of which already exist at
`POST /api/runner/<action>`:

| Button | Shown when | Endpoint action |
|---|---|---|
| DRAIN | running, not draining | `drain` |
| CANCEL DRAIN | draining | `canceldrain` |
| START | stopped | `start` |
| STOP | running | `stop` |
| RESTART | running | `restart` |
| REMOVE | always | `remove` |

`start` and `canceldrain` are already implemented server-side and are simply
not surfaced by the grid today.

### Module boundary

New file: `dashboard/runner_detail.py`, holding only read-only introspection of
a single runner.

`docker_ops.py` is 331 lines covering fleet lifecycle and fleet telemetry.
Adding five detail collectors would push it past 550 and mix two
responsibilities — "change the fleet" and "examine one runner". The new module
depends on `docker_ops` for its `_docker()` helper and name validation, and
nothing depends on it except `app.py`.

It follows the same failure contract as `docker_ops._run()`: never raises,
returns data or an error string for the caller to render. A wedged runner
degrades one tab, not the page.

## The four tabs

### Overview — container internals

Source: `docker inspect <name>`, cached 10 seconds.

Rendered fields:

- image reference and resolved digest
- created timestamp, restart count
- last exit code and when it exited
- **applied** CPU and memory limits, read from `HostConfig.NanoCpus` and
  `HostConfig.Memory` — the point being to show what the daemon enforced, not
  what `.env` requested
- restart policy, stop timeout
- mounts (source, destination, mode)
- network mode, IP, PID
- environment, with secrets masked (see Security below)

Above the fields, three live sparklines — CPU, memory, build cache — drawn from
the in-memory series described under Data flow.

### Engine — inside its Docker

Source: four `docker exec` calls into the runner, fetched when the tab is
opened and on an explicit refresh button. Never on a poll loop.

```
docker exec <name> docker system df --format json
docker exec <name> docker images     --format '{{json .}}'
docker exec <name> docker ps -a      --format '{{json .}}'
docker exec <name> docker volume ls  --format '{{json .}}'
```

The first is already proven in `_inner_df()`. It supplies totals including
build cache size and reclaimable space, which are shown against the 40 GB
`maxUsedSpace` cap from `start.sh` so the headroom is legible.

The other three supply the per-item breakdown: which images this runner has
pulled and how large they are, what containers it is running right now, what
volumes exist.

Each call is independent. If one fails or times out, its section shows
"unavailable" with the error and the others still render.

### Logs — live terminal output

Source: `docker logs --timestamps --since <cursor> <name>`, polled every 2
seconds.

The client holds an RFC3339 cursor, initially "5 minutes ago". Each response
returns new lines plus an updated cursor taken from the last line's timestamp.
Using timestamps rather than a fixed tail means a verbose build cannot scroll
content past the window between polls, and requires no de-duplication.

Client behaviour:

- new lines are **appended**; the pane is never rebuilt
- buffer capped at 2000 lines, oldest dropped
- auto-scrolls to the bottom, unless the operator has scrolled up, in which
  case a "jump to latest" affordance appears
- a filter box hides non-matching lines without refetching

This covers job output as well as runner output, because the Actions runner
writes job progress to stdout.

### History — this runner's work, and GitHub's view of it

Two sources, both cheap.

Local: `history.list_runs(runner=<name>)`, backed by the existing
`idx_runs_runner ON runs(runner, started_at DESC)` index. Selecting a run calls
`history.get_run(id)`, which already returns `samples_data` — so the per-job CPU
and memory graph requires no new query and no schema change.

Remote: a new `GitHub.runners()` method on `github_api.py`, calling
`GET /orgs/{org}/actions/runners` with `per_page=100`, cached 60 seconds. The
container is matched to its GitHub record by the `agentName` that
`_registration()` already reads from `.runner`. Displays runner id, labels,
runner group, and GitHub's own `status` and `busy` flags.

Showing GitHub's view beside the local view makes one specific failure visible:
a container that is up while GitHub considers it offline, which is the shape of
a registration that silently broke.

## API surface

All routes are covered by the existing `@app.before_request` guard at
`app.py:100`; no per-route decorator is needed.

| Route | Freshness | Notes |
|---|---|---|
| `GET /runner/<name>` | — | page |
| `GET /api/runner/<name>/inspect` | cached 10s | container internals |
| `GET /api/runner/<name>/engine` | on open + refresh | slow, never polled |
| `GET /api/runner/<name>/logs?since=<ts>` | 2s poll | returns `{lines, cursor}` |
| `GET /api/runner/<name>/series` | 5s poll | in-memory ring |
| `GET /api/runner/<name>/github` | cached 60s | remote call |
| `GET /api/runner/<name>/history` | on open | wraps `list_runs` |

Every response is `{ok: true, data: …}` or `{ok: false, error: "…"}`. Name
validation failures return 400, unknown runners 404.

All caching in that table is **server-side**, held in `runner_detail.py` and
keyed by runner name. It exists to stop repeated docker and GitHub calls when
several browser tabs or a re-opened page ask for the same thing, so it must not
be an HTTP cache header — the existing frontend already sends `cache: no-store`
with a cache-buster, as `index.html:271` does.

## Data flow

### Polling discipline

The engine tab shells into the runner; `_inner_df()` already allows it 20
seconds. Anything that slow on a poll loop makes the page feel wedged — the
same failure that per-container `docker stats` caused before `_stats_map()`
batched it.

So: only `logs` (2s) and `series` (5s) poll. `inspect` refreshes on a 10-second
cache when the overview tab is visible. `engine`, `github` and `history` are
fetched when their tab is first opened, with a refresh control.

Switching tabs does not refetch data already held.

### Live sparklines

The collector thread already computes all three metrics every 5 seconds for the
grid — CPU and memory from `_stats_map()`, build cache from `_inner_df()`. It
gains a 120-entry ring buffer per runner — ten minutes of history — held in
memory beside `_status` and guarded by the existing `_status_lock`. Because the
values are already computed each cycle, the ring costs one append per runner
per tick and no additional docker calls.

Entries are `{t, cpu, mem, cache}`. A runner that disappears has its ring
dropped, so the structure cannot grow across add/remove cycles.

No schema change, no disk writes, and it resets on dashboard restart, which is
acceptable for a live view.

The SQLite `samples` table is deliberately left as it is: keyed to a run,
written only while a job is executing. That is what the history graphs consume,
and widening it to record continuously would grow the database for data the
live view holds in memory for free.

## Security

`docker inspect` returns `Config.Env`, which contains `GH_TOKEN`. Rendering it
would make this page a token-display surface, reachable by anyone holding the
dashboard password.

The token is masked **server-side**, in `runner_detail.py`. The real value never
enters the JSON response. Masking in CSS or in the template would leave it in
the payload, readable in devtools and in any cached response.

`mask()` and `SECRET_KEYS` currently live in `app.py`. `app.py` imports
`runner_detail`, so `runner_detail` importing them back would be a circular
import. They move into `runner_detail.py` and `app.py` imports them from there
— `runner_detail` depends only on `docker_ops`, which has no application
dependencies, so the cycle cannot form. The settings page keeps working
unchanged; only the import line moves.

The masking applies to any environment key in `SECRET_KEYS`, so a secret added
to `.env` later is masked by default rather than requiring this file to be
updated.

## Rendering

The page follows the rule established in `templates/index.html:159` — elements
are created once and their values patched. Rebuilding `innerHTML` on each poll
is what caused the visible flashing the grid used to have, and a detail page
polling twice as often would be worse.

Concretely: sparklines update in place, log lines append, field values are
compared before assignment, and a tab's DOM is built on first open and reused.

## Error handling

Every collector returns data or an error, never an exception. Per-section
degradation:

- runner stopped: overview renders from inspect (which still works), engine and
  logs show "runner is not running", history still renders
- runner wedged: the affected call times out and its section shows the timeout;
  other sections are unaffected
- GitHub unreachable or token invalid: the GitHub panel shows the error, local
  history still renders
- runner removed while the page is open: next poll 404s, the page shows a
  "this runner no longer exists" state with a link back to the grid

## Testing

- name validation rejects `../`, `github-runner-`, `github-runner-1x`, and
  accepts `github-runner-1`
- masking: an inspect payload containing a token value produces a response in
  which that value does not appear, asserted against the raw JSON string
- log cursor: two sequential calls across a boundary return no duplicated and
  no skipped lines
- collector failure: a call returning non-zero yields `{ok: false}` with the
  stderr text, and does not raise
- ring buffer: bounded at 120 entries per runner and does not grow when runners
  are added and removed repeatedly
- rendering: a repeated poll with identical data performs no DOM writes

## Files

| File | Change |
|---|---|
| `dashboard/runner_detail.py` | new — read-only single-runner collectors |
| `dashboard/templates/runner.html` | new — the page and its four tabs |
| `dashboard/app.py` | seven routes; ring buffer in the collector thread |
| `dashboard/github_api.py` | add `runners()` |
| `dashboard/templates/index.html` | cards link to the page; `stopPropagation` on action buttons |
