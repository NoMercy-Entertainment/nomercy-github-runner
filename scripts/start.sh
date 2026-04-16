#!/bin/bash

RUNNER_SUFFIX=$(cat /dev/urandom | tr -dc 'a-z0-9' | fold -w 5 | head -n 1)
RUNNER_NAME="nomercy-${RUNNER_SUFFIX}"

cd /root/actions-runner

export RUNNER_ALLOW_RUNASROOT=1

# Fix Yarn 4 .bin/ permission issue: ensure all new files are created with
# execute permission when running as root in Docker
umask 0000

# ── Self-heal: restore runner binaries if a botched auto-update wiped them ──
if [ ! -f ./bin/Runner.Listener ]; then
  echo "Runner binaries missing — re-extracting runner v${RUNNER_VERSION}..."
  curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    | tar -xz
  echo "Runner binaries restored."
fi

# ── Start Docker daemon (Docker-in-Docker) ─────────────────────────────────
# Each runner runs its own isolated Docker daemon so builds don't share the
# host's disk via /var/run/docker.sock.
echo "Starting Docker daemon inside container..."
# fuse-overlayfs works in nested containers; vfs is the universal fallback
STORAGE_DRIVER="fuse-overlayfs"
if ! command -v fuse-overlayfs > /dev/null 2>&1; then
    STORAGE_DRIVER="vfs"
fi
echo "Using storage driver: ${STORAGE_DRIVER}"

dockerd --host=unix:///var/run/docker.sock \
        --storage-driver=${STORAGE_DRIVER} \
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
exec ./run.sh
