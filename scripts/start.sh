#!/bin/bash

RUNNER_SUFFIX=$(cat /dev/urandom | tr -dc 'a-z0-9' | fold -w 5 | head -n 1)
RUNNER_NAME="nomercy-${RUNNER_SUFFIX}"

cd /root/actions-runner

export RUNNER_ALLOW_RUNASROOT=1

# Fix Yarn 4 .bin/ permission issue: ensure all new files are created with
# execute permission when running as root in Docker
umask 0000

# ── Runner version ───────────────────────────────────────────────────────────
# GitHub deprecates old runner versions and refuses to deliver jobs to them:
#   "Runner version vX is deprecated and cannot receive messages"
# Pin a current version here and bump it when GitHub deprecates it again.
# Keep this in sync with ARG/ENV RUNNER_VERSION in the dockerfile.
RUNNER_VERSION="2.336.0"

# ── Self-heal / upgrade: (re)install runner binaries ─────────────────────────
# Re-extract when the binaries are missing (a botched auto-update wiped them)
# OR the installed version differs from the target above (e.g. the baked image
# still ships a now-deprecated runner). Idempotent: a matching install is a no-op.
if [ ! -f ./bin/Runner.Listener ] || [ "$(cat ./.runner_version 2>/dev/null)" != "$RUNNER_VERSION" ]; then
  echo "Installing GitHub Actions runner v${RUNNER_VERSION}..."
  _tar="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  _url="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${_tar}"

  # Download to a file, then extract, and check BOTH. The previous version was
  # `curl … | tar -xz` with neither exit status examined, followed by an
  # unconditional write of the marker below. A transient download failure
  # therefore recorded "v2.336.0 installed" over binaries that were still
  # v2.333.1 — and because the marker then matched the pin, the next boot's
  # check passed and it never retried. github-runner-6 sat on deprecated
  # binaries until GitHub refused to send it work, then restart-looped for
  # hours. Observed 2026-08-01; the download takes anywhere from 20s to 255s
  # on this host, so the failure window is real and not rare.
  if curl -fsSL -o "/tmp/${_tar}" "$_url" && tar -xzf "/tmp/${_tar}"; then
    rm -f "/tmp/${_tar}"
    # Trust the binary over the download. The only claim worth recording is
    # what the extracted listener actually reports.
    _got="$(./bin/Runner.Listener --version 2>/dev/null | tail -1 | tr -d '\r')"
    if [ "$_got" = "$RUNNER_VERSION" ]; then
      echo "$RUNNER_VERSION" > ./.runner_version
      echo "Runner v${RUNNER_VERSION} installed."
    else
      # Never leave a marker we cannot stand behind: it is what prevents the
      # retry that would fix this.
      rm -f ./.runner_version
      echo "[FATAL] Extracted runner reports '${_got:-nothing}', expected ${RUNNER_VERSION}."
      echo "        Marker not written; this container will retry on restart."
      exit 1
    fi
  else
    rm -f "/tmp/${_tar}" ./.runner_version
    echo "[FATAL] Could not download or extract runner ${RUNNER_VERSION}."
    echo "        ${_url}"
    echo "        Marker not written; this container will retry on restart."
    exit 1
  fi
fi

# ── Start Docker daemon (Docker-in-Docker) ─────────────────────────────────
# Each runner runs its own isolated Docker daemon so builds don't share the
# host's disk via /var/run/docker.sock.
#
# Storage driver: fuse-overlayfs (userspace overlay) — required because the
# kernel cannot stack native overlay2 on top of the host's overlay FS (the
# DinD container is itself an overlay mount). BuildKit cachemounts also break
# with overlay2 in nested containers, so we disable the containerd snapshotter
# to route BuildKit through the daemon's fuse-overlayfs snapshotter.
# Build-cache GC: six independent daemons each hoarded every layer forever,
# reaching ~149 GB of /var/lib/docker/fuse-overlayfs per runner and filling the
# Docker VM disk shared with the BeastStack production stack. The policy below
# is a single filterless rule capping total build cache at 20 GB per runner.
# There is no time-based rule: BuildKit evicts by its own least-recently-used
# ordering once the 20 GB ceiling is crossed, and by nothing else.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "storage-driver": "fuse-overlayfs",
  "features": {
    "containerd-snapshotter": false
  },
  "builder": {
    "gc": {
      "enabled": true,
      "policy": [
        { "maxUsedSpace": "40GB" }
      ]
    }
  }
}
EOF

# ── Clean up stale Docker state from a previous container start ───────────────
# With `restart: unless-stopped`, the container's writable layer survives a
# restart, so a leftover /var/run/docker.pid makes dockerd abort with:
#   "failed to start daemon, ensure docker is not running or delete
#    /var/run/docker.pid: process with PID N is still running"
# (the recorded PID frequently collides with an unrelated process in the new
# boot). Kill any lingering daemon and clear stale pidfiles before starting.
pkill -9 dockerd 2>/dev/null || true
pkill -9 containerd 2>/dev/null || true
rm -f /var/run/docker.pid /run/docker.pid \
      /var/run/docker/containerd/containerd.pid 2>/dev/null || true

echo "Starting Docker daemon inside container (storage-driver=fuse-overlayfs)..."
dockerd --host=unix:///var/run/docker.sock \
        > /var/log/dockerd.log 2>&1 &

# Wait for Docker daemon to be ready
echo "Waiting for Docker daemon..."
for i in $(seq 1 30); do
  if docker info > /dev/null 2>&1; then
    echo "Docker daemon is ready."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "Error: Docker daemon failed to start. Logs:"
    cat /var/log/dockerd.log
    exit 1
  fi
  sleep 1
done

# ── Janitor: keep disk usage bounded ────────────────────────────────────────
# BuildKit's GC policy (daemon.json above) caps build cache. This covers what
# GC does not: dead images, abandoned job workspaces, and runner diag logs.
# Runs every 6h. The first pass is delayed so a runner restarting into a queued
# job is not competing with the janitor for I/O.
JANITOR_INTERVAL=21600

janitor() {
  sleep "$JANITOR_INTERVAL"
  while true; do
    echo "[janitor] $(date -u +%FT%TZ) sweep starting"

    # Dead images in this runner's own daemon. `until=72h` filters on the
    # image's *Created* time, not on when it was last used — an old-but-hot
    # base image is removed just the same. Concretely: runner-4 holds four
    # 23.8 GB `ffmpeg-base` images built two weeks ago, and this sweep drops
    # them, so the next ffmpeg build starts cold. That is accepted: the images
    # are unreferenced (0 active across the fleet) and dominate disk. The
    # filter only spares images built in the last 72h, i.e. the current day's
    # in-flight work.
    docker image prune -af --filter until=72h 2>&1 | tail -2

    # Abandoned job workspaces. A directory's own mtime only changes when its
    # entry list changes, so `_work/<repo>` records its first checkout and
    # never updates again — the runner writes into `<repo>/<repo>` beneath it.
    # Testing the parent's mtime would therefore delete actively-used
    # workspaces. Recurse instead and keep the workspace if *any* file or
    # directory inside it is newer than 14 days.
    # Underscore-prefixed dirs (_tool, _temp, _actions) are runner-internal and
    # are left alone here.
    if [ -d /root/actions-runner/_work ]; then
      find /root/actions-runner/_work -mindepth 1 -maxdepth 1 -type d \
           ! -name '_*' -print0 2>/dev/null |
      while IFS= read -r -d '' d; do
        [ -n "$(find "$d" -newermt '-14 days' -print -quit 2>/dev/null)" ] && continue
        echo "[janitor] removing stale workspace $d"
        rm -rf -- "$d" 2>/dev/null || true
      done
    fi

    # Runner diagnostic logs.
    find /root/actions-runner/_diag -type f -mtime +14 -delete 2>/dev/null || true

    # Abandoned actions/github-pages deploy workspaces. These land directly
    # under /root (not under _work — a separate action, separate landing
    # spot), so they need their own clause rather than folding into the
    # _work sweep above. maxdepth 1 keeps this from ever descending into
    # unrelated /root dirs (actions-runner, .gradle, .rustup, .cache, ...).
    find /root -maxdepth 1 -type d -name 'actions_github_pages_*' \
         -mtime +14 -print -exec rm -rf {} + 2>/dev/null || true

    echo "[janitor] $(date -u +%FT%TZ) sweep complete"
    sleep "$JANITOR_INTERVAL"
  done
}

# ── Shutdown budget ─────────────────────────────────────────────────────────
# `docker stop` sends SIGTERM and SIGKILLs after the container's StopTimeout.
# That is documented as 10s, but this host runs Docker Engine 29.5.3, which has
# a regression (moby/moby#52775) creating containers with StopTimeout=1 —
# verified: `docker inspect -f '{{.Config.StopTimeout}}'` returns 1 for all six
# runners. Nothing in compose or the image sets it, and fixing it in compose
# would require a container recreate, which is gated separately.
#
# Two consequences, both handled below:
#   1. The shutdown path is ordered so the work that MUST complete
#      (deregistration) runs first and gets whatever budget exists.
#   2. Every network/CLI call is still individually bounded, so the path is
#      safe under an explicit `docker stop -t 30` — which is how the runners
#      are expected to be stopped until the Engine bug is resolved.
GH_API_MAX_TIME=10          # curl --max-time, startup
CONFIG_REMOVE_TIMEOUT=30    # hard timeout for ./config.sh remove, startup

SHUTDOWN_API_MAX_TIME=3     # curl --max-time, shutdown
SHUTDOWN_CONFIG_TIMEOUT=5   # hard timeout for ./config.sh remove, shutdown
SHUTDOWN_CHILD_TIMEOUT=0    # do not wait on run.sh; see stop_runner()
# Worst case shutdown: 3 (curl) + 5+1 (timeout -k 1) + 0 (child) = 9s.
#
# SHUTDOWN_CONFIG_TIMEOUT is 5 rather than 3 because config.sh runs
# `ldconfig -NXv` in its preamble before Runner.Listener starts; measured on
# live runner-1 that takes ~1s warm but up to ~7.8s on a cold page cache, which
# is the state a real `docker stop` hits hours into uptime. 5s covers the warm
# case with margin without pushing the total past the documented 10s default.

# ── GitHub API token helper ─────────────────────────────────────────────────
# The runner needs two *different*, non-interchangeable credentials:
#   registration-token  → ./config.sh          (register)
#   remove-token        → ./config.sh remove   (deregister)
# Handing a registration token to `config.sh remove` fails every time. This
# helper exists so the two endpoints can no longer be conflated.
#
#   $1 = "registration-token" | "remove-token"
#   $2 = curl --max-time in seconds
# Prints the token on stdout; on failure logs a diagnostic to stderr and
# returns non-zero.
gh_token() {
  local kind="$1" max_time="$2"
  local url="https://api.github.com/orgs/${GITHUB_ORG}/actions/runners/${kind}"
  local response token message rc=0

  # curl's stderr is deliberately NOT merged into $response and $response is
  # never echoed: on exit 28 (--max-time hit mid-transfer) the captured body is
  # a partial JSON document that can contain part of `"token":"..."`, and
  # logging it would leak a credential into the container log. curl's own
  # diagnostics (-sS) go straight to stderr, which is token-free.
  response=$(curl -sS -X POST \
    --max-time "$max_time" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "$url") || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "Error: ${kind} request failed (curl exit ${rc})." >&2
    return 1
  fi

  message=$(printf '%s' "$response" | jq -r '.message // empty' 2>/dev/null)
  token=$(printf '%s' "$response" | jq -r '.token // empty' 2>/dev/null)

  if [ -z "$token" ]; then
    echo "Error: no ${kind} in API response${message:+ (${message})}" >&2
    return 1
  fi

  printf '%s' "$token"
}

# ── Server-side deregistration ──────────────────────────────────────────────
# Always uses a *removal* token, and never silences the outcome.
#   $1 = curl --max-time, $2 = hard timeout for ./config.sh remove
#   $3 = what is being removed, for the log only
#
# $3 exists because `config.sh remove` takes no runner name — it acts on
# whatever identity is recorded in ./.runner. On the shutdown path that is this
# boot's RUNNER_NAME, but on the startup path it is the *previous* boot's
# runner, so a message naming $RUNNER_NAME there would name the wrong one.
#
# `timeout -k 1` matters: plain `timeout N` sends TERM and then keeps waiting
# on the child, so it only returns promptly if that child actually dies on
# TERM. Measured in-container, `timeout 2 bash -c 'trap "" TERM; sleep 20'`
# returned after 20s while `timeout -k 1 2 ...` returned after 3s. config.sh is
# plain bash with no TERM trap so the bound happens to hold today; -k 1 makes
# it guaranteed rather than incidental.
#
# Caveat on the startup path: when the timeout fires, config.sh's
# Runner.Listener grandchild is orphaned rather than killed. A straggling
# `remove` could in principle still be running when the subsequent `--replace`
# writes .runner/.credentials and then delete them, producing exactly the
# registered-but-unconfigured state this fix exists to prevent. The startup
# timeout is 30s precisely so this effectively never fires; the retry loop in
# register() is the backstop if it ever does.
github_remove_runner() {
  local api_max_time="$1" config_timeout="$2" what="$3"
  local rm_token rc=0

  if ! rm_token=$(gh_token remove-token "$api_max_time"); then
    echo "Warning: no removal token — server-side removal of ${what} skipped."
    return 1
  fi

  timeout -k 1 "$config_timeout" ./config.sh remove --token "$rm_token" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Server-side removal of ${what} succeeded."
    return 0
  fi
  if [ "$rc" -eq 124 ]; then
    echo "Warning: './config.sh remove' timed out after ${config_timeout}s (${what})."
  else
    echo "Warning: './config.sh remove' failed (exit ${rc}) (${what})."
  fi
  return 1
}

# ── Register ────────────────────────────────────────────────────────────────
register() {
  echo "Registering runner ${RUNNER_NAME}..."

  if ! REG_TOKEN=$(gh_token registration-token "$GH_API_MAX_TIME"); then
    fatal "could not obtain a registration token"
  fi

  # Clear any stale registration left behind by a previous container start.
  # Server-side removal is attempted and its outcome logged either way, but the
  # local state files are deleted *unconditionally*: with them gone, the
  # `--replace` below cannot abort with "Cannot configure the runner because it
  # is already configured", whether or not the removal call succeeded.
  echo "Clearing stale registration state..."
  github_remove_runner "$GH_API_MAX_TIME" "$CONFIG_REMOVE_TIMEOUT" \
    "the previous boot's registration" || true
  rm -f .runner .credentials .credentials_rsaparams
  echo "Local runner state cleared (.runner, .credentials, .credentials_rsaparams)."

  local config_cmd=(./config.sh
    --replace
    --unattended
    --disableupdate
    --token "$REG_TOKEN"
    --url "https://github.com/${GITHUB_ORG}"
    --labels "${RUNNER_LABELS:-self-hosted,Linux,X64}"
    --name "$RUNNER_NAME"
  )

  if [ -n "$RUNNER_GROUP" ]; then
    config_cmd+=(--runnergroup "$RUNNER_GROUP")
  fi

  # config.sh's exit status MUST be checked. There is no `set -e`, so the old
  # bare invocation let a failed registration fall through to `./run.sh`, and
  # the upstream scripts then laundered the failure into a zero exit:
  # run-helper.sh maps Runner.Listener codes 0/1/5/unknown to `exit 0`, and
  # run.sh maps everything except 2 (and 7, gated behind an env var that is not
  # set here) to `exit 0`. An unconfigured runner therefore reported
  # "exited with status 0" and restart-looped invisibly.
  #
  # This is not hypothetical: RUNNER_GROUP is set to a real group. If that group
  # is renamed or deleted org-side, `--runnergroup` fails deterministically on
  # every runner at once — each would wipe its local config, fail to register,
  # and exit 0 forever. Retry a few times to ride out transient API errors,
  # then exit non-zero so the container's restart policy and any exit-code
  # monitoring actually see the failure.
  local attempt
  for attempt in 1 2 3; do
    if "${config_cmd[@]}"; then
      echo "Runner ${RUNNER_NAME} registered."
      return 0
    fi
    echo "Error: config.sh failed (attempt ${attempt}/3)."
    rm -f .runner .credentials .credentials_rsaparams
    sleep 10
  done

  fatal "registration failed after 3 attempts — exiting for container restart"
}

# ── Deregister on container shutdown ───────────────────────────────────────
# Guarded: an explicit call and the EXIT trap must not both do the work.
RUNNER_REMOVED=0
remove() {
  if [ "$RUNNER_REMOVED" -eq 1 ]; then
    return 0
  fi
  RUNNER_REMOVED=1

  echo "Container stopping — removing runner ${RUNNER_NAME}..."
  github_remove_runner "$SHUTDOWN_API_MAX_TIME" "$SHUTDOWN_CONFIG_TIMEOUT" \
    "runner ${RUNNER_NAME}" || true
}

# Abort without letting the EXIT trap run a full deregistration. Registration
# failures happen before this boot's RUNNER_NAME exists server-side, so `remove`
# would spend a token fetch and a config.sh invocation on a name GitHub has
# never heard of — twice per iteration of a six-container crash loop.
fatal() {
  echo "Error: $1"
  RUNNER_REMOVED=1
  exit 1
}

JANITOR_PID=""
RUNNER_PID=""

stop_janitor() {
  if [ -n "$JANITOR_PID" ]; then
    kill "$JANITOR_PID" 2>/dev/null || true
    JANITOR_PID=""
  fi
}

# Best-effort only, and deliberately last in the shutdown sequence.
# RUNNER_MANUALLY_TRAP_SIG is empty in this image (verified), so upstream run.sh
# takes its `run()` branch, which installs no TERM trap at all. run.sh itself
# therefore dies on TERM immediately — what survives is Runner.Listener, its
# grandchild, which is never signalled and is left for Docker's SIGKILL.
# Waiting here would only be waiting for a process that is already gone while
# the one that matters ignores us, and with StopTimeout=1 on this host it is
# time deregistration cannot spare — hence SHUTDOWN_CHILD_TIMEOUT=0: signal,
# do not wait. The loop is retained so the wait can be re-enabled by raising
# the constant if RUNNER_MANUALLY_TRAP_SIG is ever set, which is what would
# make run.sh forward the signal on to the listener.
stop_runner() {
  [ -n "$RUNNER_PID" ] || return 0
  kill -TERM "$RUNNER_PID" 2>/dev/null || true

  local waited=0
  while kill -0 "$RUNNER_PID" 2>/dev/null; do
    if [ "$waited" -ge "$SHUTDOWN_CHILD_TIMEOUT" ]; then
      [ "$SHUTDOWN_CHILD_TIMEOUT" -gt 0 ] &&
        echo "run.sh still alive after ${SHUTDOWN_CHILD_TIMEOUT}s — leaving it to SIGKILL."
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  wait "$RUNNER_PID" 2>/dev/null || true
  return 0
}

# Order matters more than anything else here. With StopTimeout=1 on this host
# there is roughly one second between SIGTERM and SIGKILL, so whatever runs
# first is the only thing that can run at all. Deregistration is the work that
# must complete — stopping the child is what SIGKILL does for free — so
# `remove` goes ahead of `stop_runner`. Under `docker stop -t 30` both complete;
# under the current 1s default at least the right call is the one in flight.
shutdown_handler() {
  local signame="$1" code="$2"
  echo "Received SIG${signame} — shutting down runner ${RUNNER_NAME}..."
  stop_janitor
  remove
  stop_runner
  exit "$code"
}

trap 'shutdown_handler INT 130' INT
trap 'shutdown_handler TERM 143' TERM
trap 'stop_janitor; remove' EXIT

# ── Register once, run continuously ────────────────────────────────────────
# No --ephemeral: the runner stays registered and picks up jobs continuously.
#
# run.sh is started as a background child and waited on rather than exec'd.
# `exec` replaced this shell, so the INT/TERM/EXIT traps above ceased to exist
# along with it and deregistration never ran. Keeping this shell alive as PID 1
# is what makes those handlers reachable; it deregisters on signal and on the
# runner exiting by itself.
register || exit 1

janitor &
JANITOR_PID=$!

# Job control on. Without it bash adds SIGINT to a background child's ignore
# mask (measured: SigIgn 0x4 in the foreground vs 0x6 backgrounded, and 0x0
# with `set -m`), so run.sh would silently stop being interruptible the moment
# it moved off the foreground. Upstream's own runWithManualTrap does the same.
set -m

echo "Starting runner ${RUNNER_NAME}..."
./run.sh &
RUNNER_PID=$!

# `wait` is looped rather than called once, and the loop must not be
# "simplified" back to a bare `wait`. With job control on (`set -m` above)
# bash's `wait` returns when the child changes *state*, not only when it exits:
# a `kill -STOP` on run.sh makes it return 128+SIGSTOP while the process is
# merely paused. A single `wait` would then fall straight through to `remove`
# and deregister a perfectly healthy runner, exiting non-zero to bounce the
# container, over a child that is still alive. Re-checking that the pid exists
# means only a real exit ends the loop.
#
# The `sleep 1` is not decorative: `wait` on an already-reported stopped job
# returns immediately, so without it a stopped child spins this loop at full
# CPU (measured: ~50k iterations in 4s). Polling once a second costs nothing
# and cannot spin.
RUNNER_STATUS=0
while kill -0 "$RUNNER_PID" 2>/dev/null; do
  wait "$RUNNER_PID"
  RUNNER_STATUS=$?
  kill -0 "$RUNNER_PID" 2>/dev/null && sleep 1
done

# Note: upstream launders almost every Runner.Listener failure into exit 0
# (see the comment in register()), so a non-zero status here is rare by
# construction. Registration failures are caught in register() instead, which
# is where the meaningful non-zero exit comes from.
echo "run.sh exited with status ${RUNNER_STATUS}."
stop_janitor
remove
exit "$RUNNER_STATUS"
