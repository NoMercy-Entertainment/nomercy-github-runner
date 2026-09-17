#!/bin/bash
# Install Docker CE in the github-runners WSL distro.
# Idempotent: safe to re-run.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "== prerequisites =="
apt-get update -qq
apt-get install -y -qq ca-certificates curl >/dev/null

echo "== docker apt repo =="
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

echo "== install docker engine =="
apt-get update -qq
apt-get install -y -qq \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin >/dev/null

echo "== daemon config =="
# live-restore keeps running containers alive across a daemon restart.
#
# Without it every `systemctl restart docker` kills the whole fleet, so the
# daemon can never be restarted while anything is building - and a stuck
# container, a config change or an engine upgrade all need exactly that. On
# 2026-09-17 a container wedged in "removal already in progress" could not be
# cleared for hours for this reason, with seven builds running.
#
# This is one of the few settings dockerd applies on SIGHUP, so enabling it on
# a live engine costs nothing: `systemctl reload docker` suffices and no
# container is touched. Verified on this engine with 13 containers, all of
# which survived.
#
# An existing config is merged, never overwritten: /etc/docker/daemon.json on
# the DISTRO is not the same file the runner entrypoints write inside their
# own containers, and clobbering it has taken the fleet down before.
mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ]; then
  python3 -c 'import json;p="/etc/docker/daemon.json";c=json.load(open(p));c["live-restore"]=True;json.dump(c,open(p,"w"),indent=2)'
else
  printf '{\n  "live-restore": true\n}\n' > /etc/docker/daemon.json
fi

echo "== enable + start =="
systemctl enable --now docker
# reload, not restart: applies live-restore without stopping anything.
systemctl reload docker || true

echo "== result =="
docker info --format 'server={{.ServerVersion}} storage={{.Driver}} root={{.DockerRootDir}} live-restore={{.LiveRestoreEnabled}}'
docker compose version
