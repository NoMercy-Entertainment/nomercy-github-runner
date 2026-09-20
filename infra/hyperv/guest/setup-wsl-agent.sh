#!/usr/bin/env bash
# Run as root inside the github-runners distro, from the stage directory
# Install-WslAgent.ps1 copied over: the agent as a systemd service, with the
# certificate the controller issued it. Idempotent.
#
# The distro already has the engine and the fleet on it; nothing here installs
# either, and nothing here touches a running runner. The agent only answers
# the control verbs and sends a heartbeat.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"

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

# Where the agent remembers which container a runner already was, for the
# runners that were serving long before the controller knew them (T-0802).
install -d -m 700 /var/lib/runner-agent

# --- the unit images this worker's cells are made from -------------------------
# The fleet's own images plus the three entry points the agent drives
# (images/linux/unit). A runner rebuilt on this worker is made from these;
# the images the containers ran before have no /runner/register, which is
# what a rebuild discovered the hard way (2026-09-20).
for cell in github forgejo; do
  base="$(cat "BASE_${cell}" 2>/dev/null || true)"
  [ -n "$base" ] || continue
  docker build -q -f images/linux/unit/Dockerfile."$cell"     --build-arg BASE="$base"     --label "org.opencontainers.image.revision=${VERSION}"     -t "nomercy/runner-unit-${cell}:${VERSION}" images/linux/unit >/dev/null
  echo "unit image nomercy/runner-unit-${cell}:${VERSION}"
done

systemctl daemon-reload
systemctl enable runner-agent >/dev/null
systemctl restart runner-agent
sleep 2
systemctl is-active runner-agent
journalctl -u runner-agent -n 3 --no-pager
echo "wsl worker at ${VERSION}"
