#!/usr/bin/env bash
# Run as root on the control plane by Initialize-RunnerPlatform.ps1, from the
# stage directory it copied over. Idempotent: run again, it rebuilds the image
# from the code it was given and restarts the controller; the authority and
# the store, on the controller's volume, are kept.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"

bash scripts/install-docker.sh >/dev/null
usermod -aG docker "${SUDO_USER:-rnr-admin}" || true

docker build -q -t nomercy/runner-dashboard:platform \
  --label "org.opencontainers.image.revision=${VERSION}" dashboard >/dev/null

install -d -m 700 /etc/runner-platform
install -m 600 controller.env /etc/runner-platform/controller.env
install -m 644 controller-compose.yml /etc/runner-platform/compose.yml

compose=(docker compose -f /etc/runner-platform/compose.yml)
# The authority before the controller: `run` will not start without it.
"${compose[@]}" run --rm --no-deps --entrypoint python controller \
  -m control init-pki
"${compose[@]}" up -d --force-recreate controller
echo "controller up at ${VERSION}"
