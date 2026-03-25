#!/bin/bash
# Docker cleanup — runs on the HOST via the mounted docker.sock
# Safe to run while containers are active; only removes unused data.

echo "[$(date -Iseconds)] Docker cleanup starting..."

# Remove stopped containers older than 1 hour
docker container prune -f --filter "until=1h"

# Remove dangling images (untagged layers left by builds)
docker image prune -f

# Remove unused images not referenced by any container (older than 24h)
docker image prune -a -f --filter "until=24h"

# Remove unused build cache older than 24h
docker builder prune -f --filter "until=24h"

# Remove unused volumes (not attached to any container)
docker volume prune -f

# Remove unused networks
docker network prune -f --filter "until=1h"

# Clean up GitHub Actions runner work directories (completed job leftovers)
if [ -d "/root/actions-runner/_work" ]; then
    find /root/actions-runner/_work -mindepth 1 -maxdepth 1 -type d -mmin +120 -exec rm -rf {} + 2>/dev/null
fi

# Clean up runner diagnostic logs older than 7 days
if [ -d "/root/actions-runner/_diag" ]; then
    find /root/actions-runner/_diag -name "*.log" -mtime +7 -delete 2>/dev/null
fi

echo "[$(date -Iseconds)] Docker cleanup complete."
docker system df
