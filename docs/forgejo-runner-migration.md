# Migrating the Forgejo runner off BeastStack

Status: not yet performed. Everything below the line "What has and has not
been verified" is the plan, not a record of what happened.

## What moved, and why

BeastStack runs a `forgejo_runner` service that mounts the *host's own*
`/var/run/docker.sock`. Forgejo CI jobs therefore run as sibling containers on
the production engine, alongside Immich, MinIO and Forgejo itself. A job that
fills its disk or runs away with CPU can starve any of them - the same failure
`docker-compose.runners.yml` was written to prevent for the GitHub fleet (see
`docs/superpowers/specs/2026-07-26-runner-disk-containment-design.md` and
`docs/superpowers/specs/2026-07-26-runner-engine-isolation-design.md`).

`forgejo-runner-1` in this compose file closes that gap the same way the
GitHub runners already are: its own nested `dockerd` on `fuse-overlayfs`, on
the `github-runners` WSL distro's engine, which shares no disk, image store or
socket with BeastStack. A runner filling its disk here cannot reach anything
else.

This is worth doing independently of the dashboard. The dashboard gaining a
Forgejo section is a side effect of the runner living somewhere the dashboard
can already see - not the reason to move it.

## Why the runners move instead of the dashboard reaching across

Measured, not assumed: Docker Desktop exposes no TCP daemon. Ports 2375 and
2376 are both closed from the `github-runners` distro, and the distro's
engine can see no BeastStack container. There is no path between the two
engines today.

Exposing Docker Desktop's daemon over TCP so the dashboard could reach it was
rejected - a Docker endpoint is root-equivalent, and that would give the
dashboard reach into Immich and MinIO, the opposite of what the isolation was
for. See "Three ways across were considered" in
`docs/superpowers/specs/2026-08-25-forgejo-runners-design.md` for the full
comparison. Moving the runner is the only option that adds no new hole.

## The new instance URL

`forgejo-runner-1` reaches Forgejo at `https://forgejo.phillippepelzer.me` -
the public URL, not `http://forgejo:3000`. That compose network exists on
Docker Desktop's engine and does not exist on the distro side at all; a
container there cannot resolve it.

The host gateway `172.28.192.1` also works (measured from a container on the
distro's engine) but was rejected: it can change across a reboot, and the
public URL cannot. Both `.env` (`FORGEJO_INSTANCE_URL`) and the compose
service pass this same public URL through - there is only the one value.

## Where the admin token comes from, and what it's for

`FORGEJO_ADMIN_TOKEN` is an admin API token, minted in Forgejo under
**Settings → Applications**. It must carry admin rights - a token scoped only
to a user account cannot see `/api/v1/admin/actions/runners`, and every
Forgejo feature in the dashboard (status, history enrichment, deregistration,
minting registration tokens for runners the dashboard creates) depends on
that endpoint answering.

This token is separate from a runner *registration* token. Forgejo issues
short-lived registration tokens per runner; the dashboard mints one
automatically through the admin API for any runner it creates, so there is no
static registration token stored anywhere in the dashboard's own
configuration. `forgejo-runner-1`, being declared directly in this compose
file rather than created through the dashboard, is the one exception: it
needs a registration token by hand for its first boot only, taken from
Forgejo's **Site Administration → Actions → Runners** page and placed in
`FORGEJO_RUNNER_REGISTRATION_TOKEN` in `.env`. Once `/data/.runner` exists in
its volume, that value is never read again - `scripts/start-forgejo.sh`
requires it only when that file is absent.

## Deployment sequence

The new runner is brought up beside the old one so nothing stops picking up
jobs while the move happens.

1. **Configure.** Add `FORGEJO_INSTANCE_URL`, `FORGEJO_ADMIN_TOKEN`,
   `FORGEJO_RUNNER_LABELS` (copy the `--labels` mapping from
   `BeastStack/forgejo/docker-compose.yml`) and
   `FORGEJO_RUNNER_REGISTRATION_TOKEN` (from Forgejo's Runners admin page) to
   `.env`. `.env.example` documents each key; `.env` itself is gitignored and
   is not part of this repository's history.

2. **Confirm the admin token works** before starting anything:

   ```bash
   wsl -d github-runners -u root -- bash -lc \
     'curl -s -H "Authorization: token $FORGEJO_ADMIN_TOKEN" \
      https://forgejo.phillippepelzer.me/api/v1/admin/actions/runners | head -c 400'
   ```

   Expect a JSON **array**, including an entry for `beaststack-runner` (the
   existing runner's registered name). A `{"message": ...}` response means the
   token lacks admin rights - fix that before continuing.

3. **Start `forgejo-runner-1` alongside the old runner:**

   ```bash
   wsl -d github-runners -u root -- bash -lc \
     "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml up -d forgejo-runner-1 dashboard"
   ```

   Expect `docker logs forgejo-runner-1` to show the nested daemon starting,
   then a registration against `FORGEJO_INSTANCE_URL`, then the runner daemon
   polling. Both runners are now live. Forgejo distributes tasks between
   registered runners itself, so nothing stalls during this window - this is
   what makes a side-by-side rollout possible at all.

4. **Verify in the dashboard.** A Forgejo section should appear with
   `forgejo-runner-1` in it, state `idle`, flipping to `busy` while a job
   runs. The GitHub section should still show all runners unchanged - they
   carry no `nomercy.provider` label, so this is also the live check that the
   name-prefix fallback in `providers.py` works outside the test suite. After
   a Forgejo job completes, it should appear on the history page within
   roughly 90 seconds (the enrichment sweep interval) with a result, branch
   and link.

   `forgejo-runner-1` should also show up under that exact name in Forgejo's
   own **Site Administration → Actions → Runners** list, not as a hex string.
   Both creation paths - this static compose service and the dashboard's "+
   Add runner" - pass `FORGEJO_RUNNER_NAME` explicitly so a runner always
   registers under its container name; without it, `scripts/start-forgejo.sh`
   falls back to `$(hostname)`, which Docker sets to the container ID.
   Matching between the dashboard and Forgejo does not depend on this (it
   keys on `uuid`), but an operator comparing the two runner lists does.

5. **The point of no return.** Only once step 4 has shown a completed Forgejo
   run in the dashboard's history - proof the new runner is actually taking
   and finishing real jobs, not just registered and idle - stop and remove
   the old runner:

   ```bash
   docker compose -f /d/docker-compose/BeastStack/forgejo/docker-compose.yml stop forgejo_runner
   docker compose -f /d/docker-compose/BeastStack/forgejo/docker-compose.yml rm -f forgejo_runner
   ```

   Then deregister `beaststack-runner` in Forgejo (**Site Administration →
   Actions → Runners → delete**). Skipping this leaves a permanent offline
   entry in the runner list.

   Everything before this step is reversible by simply not doing it - the old
   runner keeps working untouched. This step is the one that isn't: once
   `forgejo_runner` is removed and deregistered, `forgejo-runner-1` is the
   only place Forgejo Actions jobs run.

6. **Housekeeping.** Comment out the `forgejo_runner` service in
   `BeastStack/forgejo/docker-compose.yml` with a note pointing here, rather
   than deleting it - so the next person to read that file learns where the
   runner went instead of concluding there never was one. Then scale
   `forgejo-runner-1` to whatever runner count the workload needs, the same
   way the GitHub fleet is scaled: additional explicit services, not
   `deploy.replicas` (see the comment at the top of
   `docker-compose.runners.yml` for why).

## Rolling back

Before step 5 above, rolling back is just not taking step 5 - `forgejo_runner`
on BeastStack was never stopped, so it never needs restarting.

After step 5, roll back by reversing it:

```bash
docker compose -f /d/docker-compose/BeastStack/forgejo/docker-compose.yml up -d forgejo_runner
```

`forgejo_runner` mounts `./forgejo_runner_data:/data`, a host bind mount, not
a named volume - `docker rm -f` in step 5 cannot lose it. A bind mount is a
directory on disk; only a named or anonymous volume is at risk from `-v`, and
step 5 does not pass one. So the registration this command restores is
exactly the one that existed before step 5, and `forgejo_runner` resumes
picking up jobs immediately, the same way `forgejo-runner-1` does. Stop
`forgejo-runner-1` in this repository if the intent is to fall back
completely rather than run both again:

```bash
wsl -d github-runners -u root -- bash -lc \
  "cd /mnt/d/docker-compose/GithubRunners && docker compose -f docker-compose.runners.yml stop forgejo-runner-1"
```

## What has and has not been verified

Measured, and true as of this writing:

- Docker Desktop's daemon is unreachable from the `github-runners` distro
  (ports 2375/2376 closed).
- A container on the distro's engine can reach
  `https://forgejo.phillippepelzer.me`; the host gateway `172.28.192.1` also
  answers but is rejected for the reboot-stability reason above.
- The slim `nomercy-forgejo-runner` image builds to roughly 600 MB and runs
  `forgejo-runner v12.0.1` with its own nested `dockerd` on `fuse-overlayfs`.
- The dashboard's provider seam, history enrichment, busy/idle detection and
  deregistration-on-remove are implemented and covered by the test suite
  (`dashboard/tests/`) against a mocked Forgejo API.

Not yet verified, because the deployment described above has not happened:

- No `forgejo-runner-1` container has ever registered against the live
  `https://forgejo.phillippepelzer.me` instance. The registration flow in
  `scripts/start-forgejo.sh` has been exercised in tests, not against the real
  API.
- No Forgejo job has been run on the new runner, so the dashboard's busy/idle
  display and history enrichment are unverified end to end - they are
  verified against the test suite's mocks, not against production traffic.
  Step 4 above exists specifically to close that gap before step 5 makes the
  change hard to undo.
- Whether `ActionTask.id` from the API matches the `task N` number the
  forgejo-runner daemon logs is untested against the live instance. Both
  match strategies described in
  `docs/superpowers/specs/2026-08-25-forgejo-runners-design.md` ("History")
  are implemented, so history enrichment has a working path either way, but
  which one actually fires in production is not yet known.
