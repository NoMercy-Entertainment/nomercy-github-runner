# Uniform runner platform: evidence

Every step that touches live infrastructure is recorded here: the date, who
decided it, the command, what it gave, and the decision. The plan's rule for
NEVER-AUTO tasks, applied to every live change, so that "which of this was
actually done" has one answer (ACC-19).

## 2026-09-18 - the dashboard image rebuilt with the v2 work

- **Decided by:** the operator ("1: Akkoord", after being told that removing
  a runner or recreating a fleet would need the admin role on v1 as well).
- **Command** (in the `github-runners` distro, project `githubrunners`):
  ```
  docker compose -p githubrunners -f /mnt/d/docker-compose/GithubRunners/docker-compose.runners.yml build dashboard
  docker run --rm -e DASH_DATA=/tmp -e ENV_PATH=/tmp/.env --entrypoint python nomercy/runner-dashboard:local -c "import app, api_v2, cards; ..."
  docker compose -p githubrunners -f /mnt/d/docker-compose/GithubRunners/docker-compose.runners.yml up -d --no-deps dashboard
  ```
- **Output:**
  - the throwaway container imported the app with 69 routes, `/v2` and
    `/runners/<runner_id>` among them;
  - `runner-dashboard` was recreated, and nothing else: all 13 runners kept
    their uptime (25 h, 11 h and 20 h);
  - `/`, `/v2` and `/login` answered, the first two redirecting to sign-in;
  - the backfill ran and the log shows no traceback.
- **Found:** the Flask banner came out as `b'...'`, because the new log
  redactor accepted bytes. It was fixed (`4b6b0f7`), rebuilt and redeployed
  with the same three commands. The banner is clean.
- **Rollback:** `git revert` the commits since `c20b335` and repeat the build.
  The dashboard's data volume was not touched.

## 2026-09-18 - the Linux unit images built and tried in throwaway units

- **Decided by:** the operator ("3: Akkoord ... Je mag dus gewoon doorgaan",
  infrastructure work approved). Nothing that runs was touched: new image tags
  and containers of their own only.
- **Commands** (in the `github-runners` distro):
  ```
  docker build -f images/linux/unit/Dockerfile.forgejo --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest -t nomercy/runner-unit-forgejo:test images/linux/unit
  docker build -f images/linux/unit/Dockerfile.github  --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-github-runner:latest  -t nomercy/runner-unit-github:test  images/linux/unit
  docker run --rm --name unit-smoke-register     ... nomercy/runner-unit-forgejo:test   # register, stand-in forgejo-runner
  docker run --rm --name unit-smoke-register-gh  ... nomercy/runner-unit-github:test    # register, stand-in config.sh
  docker run -d --privileged --restart unless-stopped --name unit-smoke-drain ... nomercy/runner-unit-forgejo:test
  docker exec -i unit-smoke-drain /runner/register            # the plan on stdin
  docker update --restart=no unit-smoke-drain; docker kill --signal=TERM unit-smoke-drain   # the agent's drain, then again after 3 s
  docker update --restart=unless-stopped unit-smoke-drain; docker start unit-smoke-drain    # the agent's start
  docker rm -f unit-smoke-drain
  ```
- **Output:**
  - register printed `{"registration_id":"77","registration_uuid":"u-77"}`.
    A second call printed the same and did not call the runner again.
    deregister exited 3. Outside a container, the entry point refused.
  - The GitHub register passed `ACTIONS_RUNNER_INPUT_TOKEN` to `config.sh`
    in the environment. Its argument list was `--unattended --replace
    --disableupdate`, and the token was in it 0 times. An empty runner
    group was not passed at all.
  - The drain: the unit was `running` 3 s and 5 s after the first SIGTERM,
    with a second SIGTERM between. The stand-in runner logged `TERM
    received`, then `job finished`. The unit exited 0 after 11 s, with
    restart policy `no`, and was still exited 5 s later. The runner's
    configuration carried `shutdown_timeout: 3h`. After the start, the unit
    was `running` with policy `unless-stopped` and its runner started again.
  - Afterwards `docker ps` showed the 13 runners and the dashboard with their
    uptimes unchanged, and no `unit-smoke` container.
- **Found, and not changed (the live fleet is not touched without an
  instruction):** forgejo-runner cancels its jobs at once on SIGTERM unless
  its configuration sets `shutdown_timeout`. Its `config.example.yaml` says so
  ("If unset or zero, the jobs will be canceled immediately"), and its
  poller's `Shutdown` does so. `scripts/start-forgejo.sh` starts the daemon
  without a configuration, so a `docker stop` or a recreate of
  `forgejo-runner-1..3` cancels the job each is running. The unit image does
  not have this problem.
- **Left behind:** the two `:test` image tags, thin layers over the images
  already there. Remove with `docker rmi nomercy/runner-unit-forgejo:test
  nomercy/runner-unit-github:test`.
