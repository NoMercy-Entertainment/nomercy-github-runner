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
RUNNER_VERSION="2.335.1"

# ── Self-heal / upgrade: (re)install runner binaries ─────────────────────────
# Re-extract when the binaries are missing (a botched auto-update wiped them)
# OR the installed version differs from the target above (e.g. the baked image
# still ships a now-deprecated runner). Idempotent: a matching install is a no-op.
if [ ! -f ./bin/Runner.Listener ] || [ "$(cat ./.runner_version 2>/dev/null)" != "$RUNNER_VERSION" ]; then
  echo "Installing GitHub Actions runner v${RUNNER_VERSION}..."
  curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    | tar -xz
  echo "$RUNNER_VERSION" > ./.runner_version
  echo "Runner v${RUNNER_VERSION} installed."
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
        { "maxUsedSpace": "20GB" }
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
# `docker stop` sends SIGTERM and SIGKILLs 10s later, so every network call and
# CLI invocation on the shutdown path is individually bounded and their sum has
# to stay comfortably under 10s. Startup is under no such pressure and uses
# looser bounds.
GH_API_MAX_TIME=10          # curl --max-time, startup
CONFIG_REMOVE_TIMEOUT=30    # hard timeout for ./config.sh remove, startup

SHUTDOWN_API_MAX_TIME=3     # curl --max-time, shutdown
SHUTDOWN_CONFIG_TIMEOUT=3   # hard timeout for ./config.sh remove, shutdown
SHUTDOWN_CHILD_TIMEOUT=2    # how long to wait for run.sh after forwarding TERM
# Worst case shutdown: 2 + 3 + 3 = 8s, ~2s of headroom inside the grace period.

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

  response=$(curl -sS -X POST \
    --max-time "$max_time" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "$url" 2>&1) || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "Error: ${kind} request failed (curl exit ${rc}): ${response}" >&2
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
github_remove_runner() {
  local api_max_time="$1" config_timeout="$2"
  local rm_token rc=0

  if ! rm_token=$(gh_token remove-token "$api_max_time"); then
    echo "Warning: no removal token — server-side removal skipped."
    return 1
  fi

  timeout "$config_timeout" ./config.sh remove --token "$rm_token" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Server-side removal of ${RUNNER_NAME} succeeded."
    return 0
  fi
  if [ "$rc" -eq 124 ]; then
    echo "Warning: './config.sh remove' timed out after ${config_timeout}s."
  else
    echo "Warning: './config.sh remove' failed (exit ${rc})."
  fi
  return 1
}

# ── Register ────────────────────────────────────────────────────────────────
register() {
  echo "Registering runner ${RUNNER_NAME}..."

  if ! REG_TOKEN=$(gh_token registration-token "$GH_API_MAX_TIME"); then
    echo "Error: could not obtain a registration token — aborting."
    exit 1
  fi

  # Clear any stale registration left behind by a previous container start.
  # Server-side removal is attempted and its outcome logged either way, but the
  # local state files are deleted *unconditionally*: with them gone, the
  # `--replace` below cannot abort with "Cannot configure the runner because it
  # is already configured", whether or not the removal call succeeded.
  echo "Clearing stale registration state..."
  github_remove_runner "$GH_API_MAX_TIME" "$CONFIG_REMOVE_TIMEOUT" || true
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

  "${config_cmd[@]}"
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
  github_remove_runner "$SHUTDOWN_API_MAX_TIME" "$SHUTDOWN_CONFIG_TIMEOUT" || true
}

JANITOR_PID=""
RUNNER_PID=""

stop_janitor() {
  if [ -n "$JANITOR_PID" ]; then
    kill "$JANITOR_PID" 2>/dev/null || true
    JANITOR_PID=""
  fi
}

# Forward SIGTERM to run.sh and wait for it, but only up to
# SHUTDOWN_CHILD_TIMEOUT. The bound is deliberate: upstream run.sh only
# forwards signals to Runner.Listener when RUNNER_MANUALLY_TRAP_SIG is set, so
# it can easily outlive the 10s grace period. Deregistration must not be
# starved waiting for it.
stop_runner() {
  [ -n "$RUNNER_PID" ] || return 0
  kill -TERM "$RUNNER_PID" 2>/dev/null || true

  local waited=0
  while kill -0 "$RUNNER_PID" 2>/dev/null; do
    if [ "$waited" -ge "$SHUTDOWN_CHILD_TIMEOUT" ]; then
      echo "run.sh still alive after ${SHUTDOWN_CHILD_TIMEOUT}s — deregistering anyway."
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  wait "$RUNNER_PID" 2>/dev/null || true
  return 0
}

shutdown_handler() {
  local signame="$1" code="$2"
  echo "Received SIG${signame} — shutting down runner ${RUNNER_NAME}..."
  stop_janitor
  stop_runner
  remove
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
register

janitor &
JANITOR_PID=$!

echo "Starting runner ${RUNNER_NAME}..."
./run.sh &
RUNNER_PID=$!

wait "$RUNNER_PID"
RUNNER_STATUS=$?

echo "run.sh exited with status ${RUNNER_STATUS}."
stop_janitor
remove
exit "$RUNNER_STATUS"
