# Forgejo runners in the same dashboard

Status: design agreed. Implementation not started.

## What this adds

The dashboard shows and controls GitHub Actions runners. It should do the same
for the Forgejo runners, with full parity: start, stop, restart, drain, remove,
add, prune, and history with per-run enrichment.

Only these two forges. There is no third one planned, so this builds a thin
seam for exactly two implementations rather than a plugin system for none.

## The obstacle, and why the runners move rather than the dashboard reaching out

The dashboard runs in the `github-runners` WSL distro and mounts that distro's
Docker socket. The Forgejo runner runs on Docker Desktop's engine as part of
BeastStack. Measured: Docker Desktop exposes no TCP daemon (2375 and 2376 are
both closed from the distro), and the distro's engine can see no Forgejo
container. There is currently no path at all between the two.

Three ways across were considered:

**Reach the second engine.** Expose Docker Desktop's daemon over TCP or a
socket proxy and give `docker_ops` a per-runner endpoint. Rejected: a Docker
endpoint is root-equivalent, and this punches the isolation hole in the
direction that matters most - the dashboard would gain reach into Immich,
MinIO and Forgejo itself. A read-only proxy would blunt that, but parity needs
`create`, `rm` and `exec`, which is substantially all of it.

**An agent on BeastStack.** A small authenticated HTTP service beside
`forgejo_runner`, exposing exactly the needed operations. Rejected as the most
work of the three: a second codebase with its own auth, deploy and upkeep.

**Move the runners into the distro.** Chosen. They become ordinary citizens
beside the GitHub runners: same engine, same socket, same lifecycle code. No
new transport, no new service, no new hole.

The move is worth doing on its own merits, independently of the dashboard.
`forgejo_runner` currently mounts BeastStack's own `/var/run/docker.sock`, so
Forgejo CI jobs run as sibling containers on the production engine. That is
precisely the failure `docker-compose.runners.yml` was written to prevent for
GitHub - "A runner filling its disk cannot starve Immich, MinIO or Forgejo".
Today a Forgejo job can do exactly that.

## Runner containers

Each Forgejo runner gets its own nested Docker daemon, the same shape as the
GitHub runners: `dockerd` on `fuse-overlayfs` with the `builder.gc` cap from
`scripts/start.sh`. That preserves per-runner disk containment, and it is why
`_inner_df`, `prune` and the whole `docker exec <name> docker system df` path
keep working untouched.

A new slim image rather than the existing runner image. Forgejo jobs run inside
job containers named by the `ubuntu-*:docker://...` labels, so the runner
container itself never uses the toolchain; carrying it would be gigabytes of
dead weight. The slim image is `docker-ce` + `fuse-overlayfs` + the
`forgejo-runner` binary, with a start script following the same pattern as
`scripts/start.sh`.

### Identity

Containers are named `forgejo-runner-<N>`, matching `github-runner-<N>`.

Provider is carried by a **label**, `nomercy.provider=github|forgejo`, beside
the existing `nomercy.runner=true`. Where the label is absent, the provider
falls back to the name prefix. That fallback is not decoration: the eight
GitHub runners running today predate the label, and without it the entire
existing fleet would vanish from the dashboard until it was rebuilt.

## The provider seam

One new module, `dashboard/providers.py`, holding two objects - `GITHUB` and
`FORGEJO` - that carry everything which differs:

| field | GitHub | Forgejo |
|---|---|---|
| `prefix` | `github-runner-` | `forgejo-runner-` |
| `image` | existing runner image | new slim image |
| `container_env(env)` | `GH_TOKEN`, `GITHUB_ORG`, `RUNNER_LABELS`, `RUNNER_GROUP` | `FORGEJO_INSTANCE_URL`, minted registration token, `FORGEJO_RUNNER_LABELS` |
| `registration_path` | `/root/actions-runner/.runner` | `/data/.runner` |
| `job_state(name)` | log scraping | `ActionRunner.status` from the API |
| `forge_client(env)` | `github_api.GitHub` | `forgejo_api.Forgejo` |

### docker_ops

- `list_runner_names()` becomes `list_runners()`, returning `(name, provider)`.
- `collect()` gains a `provider` field per runner and dispatches `_job_state`
  and `_registration` through the provider.
- `create(index, env)` becomes `create(index, env, provider)`.

Unchanged: `_stats_map`, `_inner_df`, `prune`, `start`/`stop`/`restart`,
`started_at`, `_disk`, `host_info`, and the drain state. They speak only to
Docker and to the nested daemon, and the nested daemon now exists on both
sides.

`remove()` is the one lifecycle call that does gain provider-specific work -
see "Deregistration" below.

### app.py

The three name guards - `_detail_target`, `runner_page` and `_target` - widen
from `github-runner-\d+` to `(?:github|forgejo)-runner-\d+`. They stay a strict
allowlist: the name reaches a command line, so it does not become free-form.

`/api/runner/add` and `/api/recreate` take a required `provider` in the body.
Without it, "Recreate fleet" does not know which fleet it is destroying.

## Busy and idle

Forgejo answers this directly. `GET /user/actions/runners` returns a `status`
field enumerated as `offline | idle | active` - the forge's own account of what
its runner is doing, rather than an inference from a log line.

This is the endpoint scoped to the owner's own account, not Forgejo's global
admin equivalent, `GET /admin/actions/runners`. A runner registered through
the admin path is available to every repository and every user on the
instance; this instance has open registration (`DISABLE_REGISTRATION=false`),
so a global runner would execute jobs pushed by strangers on hardware the
owner controls. The GitHub side of this dashboard is scoped to a single
organisation, and the Forgejo side is scoped equivalently - to the owner's own
account, not to the whole instance. (Confirmed against the live database: the
owner's existing runners all carry `owner_id = 1`, i.e. they are already
user-scoped - which is also why `GET /admin/actions/runners` answers with an
empty array rather than the runners that are actually there.)

Runners are matched on `uuid`, not on name: the API documents `name` as "not
unique". The uuid is in `/data/.runner`, which the provider already reads for
the registration column.

The conservative rule in `_job_state` carries over unchanged. An unreachable
API yields `unknown`, and `unknown` counts as not idle. Prune and drain must
not act on a guess, and this is the direction where guessing wrong is merely
slow rather than destructive.

`_outlived_by_container` is not used for Forgejo. It exists to stop a stale
`Running job:` line from pinning a runner busy forever; an API that reports
idle cannot get stuck that way.

## History

The forgejo-runner daemon logs a start, with the repository already in it:

```
time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is FiLL/nomercy-torrent-plugin ..."
```

It logs no corresponding completion. So the division of labour inverts relative
to GitHub:

| | GitHub | Forgejo |
|---|---|---|
| start | log | log (`task N repo is owner/repo`) |
| end and result | log | API: `ActionTask.status`, `updated_at` |
| repo | API search across org repos | already in the start line |
| workflow, branch, sha, url | API | API, direct lookup |

This makes Forgejo the simpler of the two. `github_api.find_job` has to walk
recently-pushed org repos and match on `runner_name` because the log does not
say which repository a job belonged to. The Forgejo log does, so enrichment is
one `GET /repos/{owner}/{repo}/actions/tasks` rather than a search.

`ActionTask` carries `name`, `status`, `head_branch`, `head_sha`, `event`,
`display_title`, `url`, `run_started_at` and `updated_at` - which is the set
the `runs` table already stores.

**One thing to verify against the live instance during implementation:**
whether `ActionTask.id` is the same number as the `task 830` in the log. If it
is not, the match falls back to `run_started_at` plus repository, which the
exact start timestamp from the log makes sufficiently discriminating. Either
way there is a working path; the check decides which one is used.

### Schema

Two columns on `runs`:

- `provider TEXT NOT NULL DEFAULT 'github'`
- `forge_task_id INTEGER`

The default labels all existing history correctly in one step.

The `gh_*` columns keep their names, with a comment recording that the name is
historical and the columns now carry both forges. Renaming them would read more
honestly, but it is a migration over a live history database for no functional
gain, and the standing requirement on this work is that everything keeps
working.

### Backfill and interrupted runs

`_backfill` reads a week of logs at startup. For Forgejo that yields starts
without ends, so those runs are closed from the API by task id on the first
enrichment sweep. `close_interrupted` still applies unchanged - a container
that restarted mid-job cannot still be running it, whichever forge it serves.

The enricher loop dispatches on `runs.provider` to pick the forge client.

## Deregistration

`scripts/start.sh` deregisters a GitHub runner on SIGTERM. `forgejo-runner
daemon` does not; the registration survives in `/data/.runner` and at Forgejo.
A removed container would leave a ghost runner sitting at `offline`.

So `remove()` for Forgejo calls `DELETE /user/actions/runners/{id}` first, then
`docker rm`. If that call fails the removal still proceeds and the UI reports
it: a container the operator wants gone must be removable even when Forgejo is
not answering.

## Configuration

New keys in `.env`:

| key | purpose |
|---|---|
| `FORGEJO_INSTANCE_URL` | `https://forgejo.phillippepelzer.me` |
| `FORGEJO_API_TOKEN` | user-scoped API: runner status, tasks, deregistration, minting registration tokens - scoped to the owner's own account, not admin |
| `FORGEJO_RUNNER_LABELS` | the `ubuntu-*:docker://...` mapping currently inline in BeastStack's compose file |

`FORGEJO_RUNNER_REGISTRATION_TOKEN` is not carried over. The dashboard mints a
fresh token per runner from `GET /user/actions/runners/registration-token`,
which is what makes "+ Add runner" work for Forgejo without a token being
copied by hand.

Two places must follow:

- `EDITABLE` in `app.py` gains the three keys, or they cannot be set from the
  settings page.
- `SECRET_KEYS` in `runner_detail.py` becomes `{"GH_TOKEN",
  "FORGEJO_API_TOKEN"}`. Without that the API token renders unmasked on
  the detail page.

## Network

The runner reaches Forgejo at `https://forgejo.phillippepelzer.me`, not at
`http://forgejo:3000` - that compose network does not exist on the distro side.

Measured from the distro: distro to host `:3300`, distro to the public URL, and
a container on the distro's engine to the public URL all answer. The host
gateway `172.28.192.1` is rejected as an option even though it works: it can
change across a reboot, and the public URL cannot.

## Presentation

Two sections on the status page, headed GitHub and Forgejo, each with its own
card grid and its own action row. The Running/Busy counters and the disk meter
stay fleet-wide at the top.

Separate action rows rather than one grid with a filter: `Recreate fleet` and
`Clear all cache` are destructive and fleet-specific, and a global button above
a mixed grid leaves it ambiguous what it is about to do.

## Migration, without a gap

1. Build the slim image and start `forgejo-runner-1` in the distro alongside
   the existing `forgejo_runner`.
2. Let both run. Forgejo distributes tasks itself, so nothing stalls.
3. Only once the new runner has demonstrably completed a job: stop
   `forgejo_runner` in BeastStack and deregister it, so no ghost is left.
4. Then scale to the desired count.

Backing out means not doing step 3. The old runner is still there.

## Testing

The standing requirement on this work is that the existing dashboard keeps
working, so that is made checkable rather than promised.

- The 16 existing test files stay unmodified and must stay green. If one has to
  change, that is a behaviour change and gets called out rather than bent to
  fit.
- The eight GitHub runners running today must not need rebuilding. The
  prefix fallback covers their missing `nomercy.provider` label, and that gets
  its own test using an unlabelled container.
- New tests: provider dispatch; the Forgejo API client; the `provider` default
  in the schema; closing a run from the API; deregistration on remove; the
  widened name guard still rejecting malformed names.

Test-first, per the practice of this repository.

## Order

1. `providers.py` and the seam in `docker_ops` - GitHub only, behaviour
   unchanged, existing tests still green.
2. `forgejo_api.py` against the live instance, including the `ActionTask.id`
   check.
3. The slim runner image and its start script.
4. Schema columns, history and enrichment for Forgejo.
5. Routes, then the two-section page.
6. The migration.
