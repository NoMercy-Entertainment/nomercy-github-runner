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

# ── Register ────────────────────────────────────────────────────────────────
register() {
  echo "Registering runner ${RUNNER_NAME}..."

  local reg_url="https://api.github.com/orgs/${GITHUB_ORG}/actions/runners/registration-token"
  local auth_response
  auth_response=$(curl -sS -X POST -H "Authorization: Bearer ${GH_TOKEN}" "$reg_url")

  local message
  message=$(echo "$auth_response" | jq -r '.message // empty')
  if [ "$message" = "Bad credentials" ]; then
    echo "Error: Bad credentials"
    exit 1
  fi

  REG_TOKEN=$(echo "$auth_response" | jq -r '.token')
  if [ "$REG_TOKEN" = "null" ] || [ -z "$REG_TOKEN" ]; then
    echo "Error: No registration token"
    exit 1
  fi

  # Remove any stale config from a previous run
  ./config.sh remove --token "$REG_TOKEN" 2>/dev/null || true

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
remove() {
  echo "Container stopping — removing runner ${RUNNER_NAME}..."
  # Get a fresh token for removal (the original may have expired)
  local reg_url="https://api.github.com/orgs/${GITHUB_ORG}/actions/runners/registration-token"
  local auth_response
  auth_response=$(curl -sS -X POST -H "Authorization: Bearer ${GH_TOKEN}" "$reg_url" 2>/dev/null)
  local remove_token
  remove_token=$(echo "$auth_response" | jq -r '.token // empty')

  if [ -n "$remove_token" ]; then
    ./config.sh remove --token "$remove_token" 2>/dev/null || true
  elif [ -n "$REG_TOKEN" ]; then
    ./config.sh remove --token "$REG_TOKEN" 2>/dev/null || true
  fi
}

trap 'remove; exit 130' INT
trap 'remove; exit 143' TERM
trap remove EXIT

# ── Register once, run continuously ────────────────────────────────────────
# No --ephemeral: runner stays registered and picks up jobs continuously.
# Only deregisters when the container is stopped/killed (via trap above).
register

janitor &

exec ./run.sh
