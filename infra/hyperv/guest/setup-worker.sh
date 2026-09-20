#!/usr/bin/env bash
# Run as root on a Linux worker by Initialize-RunnerPlatform.ps1, from the
# stage directory it copied over: the engine, the agent as a service with the
# certificate the controller issued it, the unit images, and a firewall that
# lets in only the control plane's calls and the host's SSH. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"
CONTROL_PLANE="$(cat CONTROL_PLANE)"
HOST="$(cat HOST_ADDRESS)"
AGENT_PORT="$(cat AGENT_PORT)"

bash scripts/install-docker.sh >/dev/null
usermod -aG docker "${SUDO_USER:-rnr-admin}" || true

# --- the agent ------------------------------------------------------------------
install -d -m 755 /opt/runner-agent
rm -rf /opt/runner-agent/agent.new
cp -r agent /opt/runner-agent/agent.new
rm -rf /opt/runner-agent/agent.old
[ -d /opt/runner-agent/agent ] && mv /opt/runner-agent/agent /opt/runner-agent/agent.old
mv /opt/runner-agent/agent.new /opt/runner-agent/agent
install -d -m 700 /etc/runner-agent
install -m 600 bundle/agent.key /etc/runner-agent/agent.key
install -m 644 bundle/agent.crt /etc/runner-agent/agent.crt
install -m 644 bundle/ca.pem /etc/runner-agent/ca.pem
install -m 600 agent.json /etc/runner-agent/agent.json
install -m 644 runner-agent.service /etc/systemd/system/runner-agent.service

# --- the unit images ----------------------------------------------------------------
# The build unpacks the image it writes, so nothing here has to create a
# container to make the first one faster. A throwaway container for that was
# tried and dropped: it left a corpse the engine could not finish removing,
# whose name then blocked the next install, and creates were measured at
# around a minute either way - the cost is preparing each container's own
# snapshot, not the image (2026-09-20).

if [ -f forgejo-base.tar ]; then
  docker load -q -i forgejo-base.tar
fi
if [ -f github-base.tar ]; then
  docker load -q -i github-base.tar
fi
if [ -s BASE_github ]; then
  docker build -q -f images/linux/unit/Dockerfile.github     --build-arg BASE="$(cat BASE_github)"     --label "org.opencontainers.image.revision=${VERSION}"     -t "nomercy/runner-unit-github:${VERSION}" images/linux/unit >/dev/null
  echo "unit image nomercy/runner-unit-github:${VERSION}"
fi
docker build -q -f images/linux/unit/Dockerfile.forgejo \
  --build-arg BASE=ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest \
  --label "org.opencontainers.image.revision=${VERSION}" \
  -t "nomercy/runner-unit-forgejo:${VERSION}" images/linux/unit >/dev/null
echo "unit image nomercy/runner-unit-forgejo:${VERSION}"

# --- the firewall -------------------------------------------------------------------
# SSH from the host only, the agent's port from the control plane only (13.2).
# SSH is allowed before the firewall is switched on, so this cannot lock the
# host out. Units' own traffic is Docker's and is not filtered here.
apt-get install -y -qq ufw >/dev/null
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow from "$HOST" to any port 22 proto tcp >/dev/null
ufw allow from "$CONTROL_PLANE" to any port "$AGENT_PORT" proto tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable runner-agent >/dev/null
systemctl restart runner-agent
sleep 2
systemctl is-active runner-agent
journalctl -u runner-agent -n 3 --no-pager
