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
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "storage-driver": "fuse-overlayfs",
  "builder": {
    "gc": {
      "enabled": true,
      "defaultKeepStorage": "20GB"
    }
  }
}
JSON

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
# The dashboard deregisters through the API when it removes a runner, which
# covers the case it knows about. This covers the other one: the container
# being stopped by anything else. Both are idempotent.
trap 'echo "SIGTERM - unregistering"; forgejo-runner unregister 2>/dev/null || true' TERM

exec forgejo-runner daemon --config "$DATA/config.yaml"
