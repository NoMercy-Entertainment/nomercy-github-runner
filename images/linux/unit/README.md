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

## Why the entry points are written the way they are

- **The runner is waited for.** `/runner/run` is PID 1, and PID 1 exiting
  ends every process in the unit. The start scripts the fleet runs today stop
  waiting for their runner two seconds after a SIGTERM. A drain through one of
  them would cancel the job the drain exists to let finish.
- **forgejo-runner gets a `shutdown_timeout`.** When it is unset or zero, the
  runner cancels its jobs the moment it is signalled. Its own
  `config.example.yaml` says so, and its poller's `Shutdown` does exactly that.
  The unit writes a configuration with 3h, the same as the job timeout.
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
