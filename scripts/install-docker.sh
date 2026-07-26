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

echo "== enable + start =="
systemctl enable --now docker

echo "== result =="
docker info --format 'server={{.ServerVersion}} storage={{.Driver}} root={{.DockerRootDir}}'
docker compose version
