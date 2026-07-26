#!/bin/bash
# Deploy the GitHub Actions runners onto the isolated `github-runners` engine.
#
# Run INSIDE the distro:
#   wsl -d github-runners -u root -- bash /mnt/d/docker-compose/GithubRunners/scripts/deploy-runners.sh [compose args...]
#
# Why this script exists rather than calling `docker compose up` directly:
# start.sh cannot be executed from the 9p mount (/mnt/d). Doing so puts each
# container into a register -> SIGTERM -> restart loop about every 20 seconds.
# Measured 2026-07-26 with the identical file (same md5) in both places:
#
#   from 9p (/mnt/d):    6+ registrations, 6+ SIGTERMs in 100s, StartedAt
#                        advancing each cycle, never reached Listening for Jobs
#   from native ext4:    1 registration, 0 SIGTERMs, StartedAt stable,
#                        reached "Listening for Jobs"
#
# Bash reads a script incrementally by file offset; 9p does not give it the
# read consistency that requires. So the repo on D: stays the source of truth
# and this script syncs start.sh to native ext4 before every deploy.

set -euo pipefail

REPO=/mnt/d/docker-compose/GithubRunners
NATIVE=/opt/github-runners
COMPOSE_FILE="$REPO/docker-compose.runners.yml"

[ -r "$REPO/.env" ] || { echo "ERROR: $REPO/.env not readable" >&2; exit 1; }
[ -r "$REPO/scripts/start.sh" ] || { echo "ERROR: start.sh missing" >&2; exit 1; }

echo "== syncing start.sh to native ext4 =="
mkdir -p "$NATIVE/scripts"
# tr -d '\r' guards against CRLF: the source lives on a Windows filesystem and
# git's autocrlf can rewrite line endings. A CRLF shebang makes the kernel fail
# to find the interpreter, which is a confusing way to discover the problem.
tr -d '\r' < "$REPO/scripts/start.sh" > "$NATIVE/scripts/start.sh"
chmod 0755 "$NATIVE/scripts/start.sh"

# Confirm the sync landed on a real local filesystem, not something mounted
# back over 9p — that is the whole point of this step.
fstype=$(findmnt -no FSTYPE --target "$NATIVE/scripts" 2>/dev/null || echo unknown)
case "$fstype" in
  ext4|ext3|xfs|btrfs|overlay) ;;
  *) echo "ERROR: $NATIVE is on '$fstype', expected a native filesystem." >&2
     echo "       Deploying from here would reintroduce the restart loop." >&2
     exit 1 ;;
esac

src_sum=$(tr -d '\r' < "$REPO/scripts/start.sh" | md5sum | cut -d' ' -f1)
dst_sum=$(md5sum "$NATIVE/scripts/start.sh" | cut -d' ' -f1)
[ "$src_sum" = "$dst_sum" ] || { echo "ERROR: checksum mismatch after sync" >&2; exit 1; }
echo "   $NATIVE/scripts/start.sh on $fstype (md5 $dst_sum)"

echo "== deploying =="
cd "$REPO"
if [ "$#" -gt 0 ]; then
  docker compose -f "$COMPOSE_FILE" "$@"
else
  docker compose -f "$COMPOSE_FILE" up -d
fi

echo "== state =="
docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}"
