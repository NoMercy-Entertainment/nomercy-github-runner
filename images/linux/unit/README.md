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

Before each job, `/runner/job-started.sh` runs `job_started.py`, which does two
things:

1. **Restores the Android SDK.** It copies back any file missing from
   `/usr/local/lib/android` out of `/opt/nomercy/android-sdk.pristine`, a
   real copy made when the image is built. It never overwrites or deletes.
   A unit's root is writable and outlives its jobs, so a workflow that runs
   `free-disk-space` with `android: true` used to take the SDK away from
   every later job on that runner (GitHub #13). The job is told with a
   `::warning`. This takes about 4 s per job when nothing is missing, and
   about 12 s after a full wipe.
2. **Guards the disk.** It checks the lowest free space of `/runner/work` and
   `/`:
   - under `RUNNER_DISK_CLEAN_BELOW_GB` (15) it runs `/runner/cleanup`;
   - under `RUNNER_DISK_WARN_BELOW_GB` (10) it prints a `::warning`;
   - under `RUNNER_DISK_FAIL_BELOW_GB` (3) it fails the job with an
     `::error`, before a full disk can take the runner down (GitHub #7).

Only that deliberate refusal fails a job. A fault in the hook itself is
reported as a warning.

The running fleet uses the `jobhooks-20261008` overlay image, built with
`Dockerfile.cleanup` from `github-unit:toolchain-20260925`. Forgejo has no job
hooks: it runs the cleanup only at unit startup. Windows and macOS GitHub
runners get the same disk guard from the agent itself:
`docs/operations/runner-job-hooks.md`.

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
