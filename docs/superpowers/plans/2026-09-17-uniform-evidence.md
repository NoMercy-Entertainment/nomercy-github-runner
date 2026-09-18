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
