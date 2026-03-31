#!/bin/bash

RUNNER_SUFFIX=$(cat /dev/urandom | tr -dc 'a-z0-9' | fold -w 5 | head -n 1)
RUNNER_NAME="nomercy-${RUNNER_SUFFIX}"

cd /root/actions-runner

export RUNNER_ALLOW_RUNASROOT=1

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

  local config_cmd=(./config.sh
    --replace
    --unattended
    --ephemeral
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

# ── Cleanup on signal ───────────────────────────────────────────────────────
remove() {
  if [ -n "$REG_TOKEN" ]; then
    echo "Removing runner ${RUNNER_NAME}..."
    ./config.sh remove --token "$REG_TOKEN" 2>/dev/null || true
  fi
}

trap 'remove; exit 130' INT
trap 'remove; exit 143' TERM
trap remove EXIT

# ── Cleanup after each job ─────────────────────────────────────────────────
cleanup() {
  echo "Cleaning up workspace and caches..."

  # Runner work directories (artifacts are already uploaded before job exits)
  rm -rf /root/actions-runner/_work/*

  # .NET intermediate build output and stale NuGet packages (keep last 3 days)
  rm -rf /root/.nuget/packages
  rm -rf /root/.dotnet/tools/.store

  # Gradle caches (daemon logs, build cache, wrapper dists)
  rm -rf /root/.gradle/daemon
  rm -rf /root/.gradle/caches/build-cache-*
  rm -rf /root/.gradle/caches/transforms-*
  rm -rf /root/.gradle/caches/journal-*
  rm -rf /root/.gradle/wrapper/dists

  # Node / Yarn / npm
  rm -rf /root/.cache/yarn
  rm -rf /root/.yarn/berry/cache
  rm -rf /root/.npm/_cacache

  # Python / pip
  rm -rf /root/.cache/pip

  # Composer
  rm -rf /root/.cache/composer

  # Rust build artifacts
  rm -rf /root/.cargo/registry/cache

  # Docker: prune dangling images + build cache older than 24h
  # (runs against host daemon via mounted socket)
  docker image prune -f --filter "until=24h" 2>/dev/null || true
  docker buildx prune -f --filter "until=24h" 2>/dev/null || true
  docker container prune -f --filter "until=1h" 2>/dev/null || true

  # APT cache
  apt-get clean 2>/dev/null || true

  echo "Cleanup complete."
}

# ── Run loop: register → run one job → cleanup → repeat ───────────────────
# --ephemeral makes the runner exit after completing one job.
# The loop re-registers and picks up the next job.
# If the container is killed, Docker restart policy brings it back.
while true; do
  # Remove stale config from previous ephemeral run
  ./config.sh remove --token "$REG_TOKEN" 2>/dev/null || true
  register
  ./run.sh
  echo "Job completed."
  cleanup
  echo "Re-registering for next job..."
done
