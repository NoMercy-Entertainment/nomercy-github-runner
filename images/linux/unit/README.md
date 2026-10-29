# Linux runner units

The images the agent's Linux runtime makes units from (design 15.1). Each is
the image the fleet runs today, with three entry points added on top:

| Entry point | Run by | Does |
| --- | --- | --- |
| `/runner/run` | the engine, as the unit's `ENTRYPOINT` | starts the nested engine, waits for the registration, starts the forge's runner, and on SIGTERM passes the signal on and **waits for the runner, with no limit** |
| `/runner/register` | the agent, `docker exec -i` | registers with the plan on standard input and prints `{"registration_id", "registration_uuid"}` |
| `/runner/deregister` | the agent, `docker exec` | says it cannot, and exits 3, because a unit holds no credential that can remove its runner; the controller then deletes the record at the forge by its id |

`RUNNER_KIND` (`github` or `forgejo`) is set by each Dockerfile. Everything
that differs between the two forges sits in one `case` on it.

## Build

Beside the running images, under a tag of their own, so nothing that runs
changes until a unit is made from one:

```sh
docker build -f images/linux/unit/Dockerfile.forgejo \
  --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest \
  -t nomercy/runner-unit-forgejo:<version> images/linux/unit
docker build -f images/linux/unit/Dockerfile.github \
  --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-github-runner:latest \
  -t nomercy/runner-unit-github:<version> images/linux/unit
```

The controller is told which image a cell's units use by
`RUNNER_UNIT_IMAGE_<PROVIDER>_<PLATFORM>`, for example
`RUNNER_UNIT_IMAGE_FORGEJO_LINUX=nomercy/runner-unit-forgejo:<version>`.

The GitHub unit runs `/runner/cleanup` synchronously after each job and at
startup. It removes unused nested Docker build cache and images without an age
limit, plus previous job workspaces, while retaining the active workspace and
installed tools.

Before each job, `/runner/job-started.sh` runs `job_started.py`, which does three
things:

1. **Refuses outside code.** First, before anything else, `runner_guard.py`
   reads the job's event: a pull request whose head repository is owned by
   anyone but the org or an account in `RUNNER_TRUSTED_OWNERS` is
   failed with `::error title=Outside code refused::`, and the job ended. Every other job gets
   one `Origin: ... allowed` line. An event it cannot read lets the job run
   with a warning. See `docs/operations/runner-job-hooks.md`, "Outside code".
2. **Restores the Android SDK.** It copies back any file missing from
   `/usr/local/lib/android` out of `/opt/nomercy/android-sdk.pristine`, a
   real copy made when the image is built. It never overwrites or deletes.
   A unit's root is writable and outlives its jobs, so a workflow that runs
   `free-disk-space` with `android: true` used to take the SDK away from
   every later job on that runner (GitHub #13). The job is told with a
   `::warning`. This takes about 4 s per job when nothing is missing, and
   about 12 s after a full wipe.
3. **Puts the PHP alternatives back.** A job that asks setup-php for another
   PHP installs it into the unit's root and points `php`, `phar`,
   `phar.phar`, `php-config` and `phpize` at it; every later job on that
   unit inherited it (nomercy-tv's `ci` left 8.4 on github-linux-x64-6,
   2026-10-28). Before each job the hook points them at the fleet's series
   again (`RUNNER_PHP_SERIES`, default 8.5) and tells the job with a
   `::warning`. A link nobody registered, or a series whose binary is
   missing, is left alone.
4. **Guards the disk.** It checks the lowest free space of `/runner/work` and
   `/`:
   - under `RUNNER_DISK_CLEAN_BELOW_GB` (15) it runs `/runner/cleanup`;
   - under `RUNNER_DISK_WARN_BELOW_GB` (10) it prints a `::warning`;
   - under `RUNNER_DISK_FAIL_BELOW_GB` (3) it fails the job with an
     `::error`, before a full disk can take the runner down (GitHub #7).

Only those deliberate refusals fail a job. A fault in the hook itself is
reported as a warning.

The running fleet uses `nomercy/runner-unit-github:20261029`, built with
`Dockerfile.cleanup` from `runner-unit-github:20261027.2` (the hooks
refreshed, the toolchain layer untouched). Forgejo has no job
hooks: it runs the cleanup only at unit startup. Windows and macOS GitHub
runners get the same disk guard from the agent itself:
`docs/operations/runner-job-hooks.md`.

## Forgejo job images and the hosted tool cache

Forgejo Linux jobs run in the image each `FORGEJO_RUNNER_LABELS` label names,
inside the unit's nested engine:

| Labels | Image | Dockerfile |
| --- | --- | --- |
| `ubuntu-latest`, `ubuntu-24.04`, `ubuntu-24.04-full` | `nomercy/github-runner-base:tools-<version>` | `Dockerfile.tools` (every tool the GitHub unit has) |
| `ubuntu-22.04`, `ubuntu-22.04-full` | `nomercy/job-ubuntu-22.04:<version>` | `Dockerfile.jammy` (the act 22.04 image, pinned, so a build keeps 22.04's glibc) |

No public registry has them. Each worker serves them from its **job-image
registry** (`infra/hyperv/guest/job-image-registry.sh`): read-only on the
docker0 gateway `172.17.0.1:5000`, which the labels name, and writable only on
the worker's own `127.0.0.1:5001`. The Forgejo unit lets its nested engine pull
from the gateway (`RUNNER_JOB_IMAGE_REGISTRY`, `insecure-registries` in the
engine's daemon.json), and `/runner/job_images.py`:

- before the startup prune, pins every image the registration's labels name
  with a stopped container, so neither `docker image prune -a` at startup nor
  the dashboard's "unused images" clear removes it, and drops pins for images
  no label names any more;
- once the runner is up, pulls any of those images the engine does not have
  (a new unit, a fresh engine volume) and pins it.

Before this, the image was `docker load`ed into each engine by hand, and the
startup prune or a recreate lost it: "pull access denied" (2026-10-27).

Both job images carry the hosted tool cache in `/opt/hostedtoolcache`
(`toolcache/install.sh`, versions and hashes in `toolcache/versions.txt`):
Python 3.10-3.14 and Node 20/22/24/26, the very builds setup-python and
setup-node download, so they find them instead. Forgejo sets
`RUNNER_TOOL_CACHE=/opt/hostedtoolcache` in docker jobs. The GitHub unit's tool
cache is `/runner/work/_tool` on its work volume; `/runner/toolcache.py` copies
each version it lacks there at start, and never touches one a job completed.

Publishing a new build, on the worker after `docker load`:

```sh
sudo bash job-image-registry.sh push nomercy/github-runner-base:tools-<v> nomercy/job-ubuntu-22.04:<v>
```

then name the new tags in `FORGEJO_RUNNER_LABELS` (controller.env) as
`172.17.0.1:5000/<image>` and recreate the Forgejo runners: labels are fixed
at registration.

## Why the entry points are written the way they are

- **The runner is waited for.** `/runner/run` is PID 1, and PID 1 exiting
  ends every process in the unit. The start scripts the fleet runs today stop
  waiting for their runner two seconds after a SIGTERM. A drain through one of
  them would cancel the job the drain exists to let finish.
- **forgejo-runner gets a `shutdown_timeout`.** When it is unset or zero, the
  runner cancels its jobs the moment it is signalled. Its own
  `config.example.yaml` says so, and its poller's `Shutdown` does exactly that.
  The unit uses `2562047h47m16s`, Go's largest whole-second duration, for
  both job execution and graceful draining. Forgejo has no unlimited
  duration: zero restores its three-hour job default or cancels draining
  jobs immediately. The server's `actions.ENDLESS_TASK_TIMEOUT` must use
  the same maximum. Workflows must omit shorter `timeout-minutes` values.
  `scripts/start-forgejo.sh` passes none, so a SIGTERM to the Forgejo
  containers running today cancels their job.
- **The GitHub token is never on a command line.** `config.sh` reads each of
  its arguments from an `ACTIONS_RUNNER_INPUT_<NAME>` variable, and the token
  goes that way. forgejo-runner has no such input. Its token is on
  `register`'s command line while that command runs, as it always has been in
  `start-forgejo.sh`.
- **The GitHub runner is passed the signal.** `RUNNER_MANUALLY_TRAP_SIG=1` makes
  `run.sh` forward it to the listener, instead of dying and leaving the listener
  to be killed with the unit.
- **Registration lives in `/runner/reg` only.** For GitHub, the files
  `config.sh` writes are links into that volume. A unit made again on the same
  volume is the same runner. A recreate drops the volume, so the runner is new.

## Proven

On 2026-09-18, in throwaway units on the WSL engine, recorded in
`docs/superpowers/plans/2026-09-17-uniform-evidence.md`:

- `register` answered with the forge's ids, and a second call registered
  nothing.
- A drain, done exactly as the agent does it (`docker update --restart=no`,
  then SIGTERM, then SIGTERM again), kept the unit up while a stand-in job
  finished. The unit then exited 0 and stayed down. A start put it back in
  service.
- A GitHub registration passed the token to `config.sh` in the environment
  only; it was on no argument list.

`agent/tests/test_unit_image.py` pins each of these in the scripts.
