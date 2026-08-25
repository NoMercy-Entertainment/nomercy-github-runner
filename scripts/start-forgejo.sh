#!/usr/bin/env bash
# Forgejo Actions runner with its own Docker daemon.
#
# The daemon is nested rather than the host's socket being mounted. Forgejo
# jobs run in job containers named by the ubuntu-*:docker:// labels, and with a
# shared socket those containers, their images and their build cache land on
# the engine everything else runs on. That is the failure this whole distro
# exists to prevent.
set -euo pipefail

: "${FORGEJO_INSTANCE_URL:?FORGEJO_INSTANCE_URL is required}"
DATA=/data
mkdir -p "$DATA"
cd "$DATA"

# --- nested daemon -------------------------------------------------------
# fuse-overlayfs (userspace overlay): the kernel cannot stack native overlay2
# on top of the host's overlay filesystem when the container is itself an
# overlay mount.
#
# builder.gc caps the build cache. Without it the nested daemons grow without
# limit - the GitHub fleet once filled a 1 TB disk this way.
# BuildKit cachemounts break with overlay2 in nested containers, so disable
# the containerd snapshotter to route BuildKit through the daemon's
# fuse-overlayfs snapshotter.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
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
JSON

# Cache cap is 20GB vs start.sh's 40GB because Forgejo jobs run in containerized
# job containers named by ubuntu-*:docker:// labels, not on the host. Less cache
# is needed; GitHub's 40GB reflects the fleet's heavier build workload.

# A container restart leaves the pid file behind and dockerd then aborts with
# "pidfile is held by another process".
rm -f /var/run/docker.pid
pkill -9 dockerd 2>/dev/null || true

echo "Starting Docker daemon inside container (storage-driver=fuse-overlayfs)..."
dockerd --host=unix:///var/run/docker.sock > /var/log/dockerd.log 2>&1 &

for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "[FATAL] the nested Docker daemon did not come up:"
  cat /var/log/dockerd.log
  exit 1
fi

# --- registration --------------------------------------------------------
# Registration is once per volume. The dashboard mints a fresh token per
# runner, so a re-register after the file is gone needs a new container.
if [ ! -f "$DATA/.runner" ]; then
  : "${FORGEJO_RUNNER_REGISTRATION_TOKEN:?a registration token is required on first boot}"
  echo "Registering runner against ${FORGEJO_INSTANCE_URL} ..."
  forgejo-runner register --no-interactive \
    --instance "$FORGEJO_INSTANCE_URL" \
    --token "$FORGEJO_RUNNER_REGISTRATION_TOKEN" \
    --name "${FORGEJO_RUNNER_NAME:-$(hostname)}" \
    --labels "$FORGEJO_RUNNER_LABELS"
fi

# --- deregistration on the way out ---------------------------------------
# The daemon is started as a background child and waited on rather than exec'd.
# A trap handler does not survive exec — the shell's process image is replaced.
# Keeping this shell alive as PID 1 is what makes deregistration reachable when
# the container stops. The dashboard deregisters through the API when it removes
# a runner, which covers the case it knows about. This covers the other one:
# the container being stopped by anything else. Both are idempotent.
DEREGISTERED=0
deregister() {
  # Guard against double-deregistration: both TERM trap and EXIT trap may reach
  # this function on the same shutdown. Idempotent on the API level, but we run
  # only once to avoid log noise and unnecessary work.
  if [ "$DEREGISTERED" -eq 1 ]; then
    return 0
  fi
  DEREGISTERED=1
  echo "Shutting down runner — deregistering..."
  forgejo-runner unregister 2>/dev/null || true
}

trap 'deregister' TERM
trap 'deregister' INT
trap 'deregister' EXIT

# Job control on. Without it bash adds SIGINT to a background child's ignore
# mask, so the daemon would silently stop being interruptible the moment it
# moved off the foreground.
set -m

echo "Starting Forgejo runner daemon..."
forgejo-runner daemon --config "$DATA/config.yaml" &
RUNNER_PID=$!

# `wait` is looped rather than called once. With job control on, bash's `wait`
# returns when the child changes state, not only when it exits: a `kill -STOP`
# would make it return 128+SIGSTOP while the process is merely paused. A single
# `wait` would then fall straight through to deregister a perfectly healthy
# runner. Re-checking that the PID exists means only a real exit ends the loop.
#
# The `sleep 1` is not decorative: `wait` on an already-reported stopped job
# returns immediately, so without it a stopped child spins this loop at full CPU.
# Polling once a second costs nothing and cannot spin.
#
# `wait` is in `if` context: bash suspends errexit within `if` condition tests,
# so a TERM signal returning 128+SIGTERM does not exit the script prematurely.
# The if statement does not catch/suppress the signal — traps still fire — but
# it prevents errexit from ending the script before the loop logic runs.
RUNNER_STATUS=0
while kill -0 "$RUNNER_PID" 2>/dev/null; do
  if wait "$RUNNER_PID"; then
    RUNNER_STATUS=0
  else
    RUNNER_STATUS=$?
  fi
  kill -0 "$RUNNER_PID" 2>/dev/null && sleep 1
done

exit "$RUNNER_STATUS"
