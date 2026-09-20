#!/usr/bin/env bash
# Run as root on the machine that hosts the macOS appliance, from the stage
# directory Install-ApplianceHost.ps1 copied over: the agent as a service,
# with the certificate the controller issued it, and the credential it uses
# to reach the guest. Idempotent.
#
# This machine is not a runner worker. It runs no unit of its own: it hosts a
# macOS guest, and its agent drives what is inside that guest over the SSH
# port the guest forwards (T-0802). So there is no engine to install, no unit
# image to build, and nothing here touches the guest or the runner in it.
#
# The firewall is deliberately left alone. This machine existed before the
# platform and is reached for other things - VNC among them - and switching
# on a default-deny firewall under it would take those away. The agent's port
# is on the host-internal network and still refuses everyone without the
# controller's certificate. Recorded as a departure from 13.2.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"

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
# The guest's own password, which is what its SSH asks for. Readable by root
# alone, and passed to sshpass through the environment, so it never appears
# on a command line (agent/runtimes/guest_ssh.py).
if [ -f guest.pass ]; then
  install -m 600 guest.pass /etc/runner-agent/guest.pass
fi
install -m 644 runner-agent.service /etc/systemd/system/runner-agent.service

# sshpass is how the guest's password reaches its SSH, and the guest is the
# only thing this agent drives.
if ! command -v sshpass >/dev/null; then
  apt-get install -y -qq sshpass >/dev/null
fi

systemctl daemon-reload
systemctl enable runner-agent >/dev/null
systemctl restart runner-agent
sleep 2
systemctl is-active runner-agent
journalctl -u runner-agent -n 3 --no-pager
echo "appliance host at ${VERSION}"
