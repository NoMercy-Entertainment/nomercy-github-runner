#!/bin/bash

RUNNER_SUFFIX=$(cat /dev/urandom | tr -dc 'a-z0-9' | fold -w 5 | head -n 1)
RUNNER_NAME="nomercy-${RUNNER_SUFFIX}"

cd /root/actions-runner

export RUNNER_ALLOW_RUNASROOT=1

# Start cron for scheduled cleanup
cron

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

# ── Run loop: register → run one job → repeat ──────────────────────────────
# --ephemeral makes the runner exit after completing one job.
# The loop re-registers and picks up the next job.
# If the container is killed, Docker restart policy brings it back.
while true; do
  register
  ./run.sh
  echo "Job completed. Re-registering for next job..."
done
