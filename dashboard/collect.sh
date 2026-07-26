#!/bin/bash
# Collect runner status from the isolated engine and emit JSON on stdout.
#
# Runs inside the dashboard container with the runners' Docker socket mounted
# read-only. That socket belongs to the `github-runners` engine, NOT Docker
# Desktop, so this can see the runners and structurally cannot see BeastStack.
#
# Every docker exec is wrapped in `timeout` so one wedged runner cannot hang
# the whole collection.

set -uo pipefail

RUNNERS=$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^github-runner-' | sort)

# docker stats is the only source of live CPU/memory and costs ~1s per call,
# so make ONE call covering every container rather than one per runner.
STATS=$(timeout 20 docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null || true)

# Measure /data, not /var/lib/docker: that path does not exist inside this
# container. /data is the dashboard-data volume, which lives on the engine's
# own ext4 - the same filesystem backing D:\Docker\GithubRunners\Data\ext4.vhdx
# - so df reports exactly the disk we care about, with no extra mounts.
disk_line=$(df -PB1 /data 2>/dev/null | tail -1)
disk_used=$(awk '{print $3}' <<<"$disk_line")
disk_total=$(awk '{print $2}' <<<"$disk_line")
: "${disk_used:=0}" "${disk_total:=0}"

emit_runner() {
  local c="$1"

  local stat_line cpu mem_used mem_limit
  # awk, not `grep -P`: this runs on Alpine, whose busybox grep has no -P and
  # exits with a usage dump, which silently emptied every CPU reading.
  stat_line=$(awk -F'\t' -v n="$c" '$1==n {print; exit}' <<<"$STATS")
  cpu=$(awk -F'\t' '{gsub(/%/,"",$2); print $2}' <<<"$stat_line")
  mem_used=$(awk -F'\t' '{split($3,a," / "); print a[1]}' <<<"$stat_line")
  mem_limit=$(awk -F'\t' '{split($3,a," / "); print a[2]}' <<<"$stat_line")
  : "${cpu:=0}"

  local status uptime
  status=$(docker ps --filter "name=^${c}$" --format '{{.Status}}' 2>/dev/null)
  uptime="${status#Up }"; uptime="${uptime%% (*}"

  # Busy detection: take the LAST job event of either kind and see which it is.
  # Grepping only for "Running job" is fooled by a completion that scrolled out
  # of view, which would report a finished job as still running.
  local last_evt state job
  last_evt=$(timeout 5 docker logs --tail 200 "$c" 2>&1 \
             | grep -E 'Running job:|Job .* completed' | tail -1)
  if grep -q 'Running job:' <<<"$last_evt"; then
    state="busy"
    job=$(sed -E 's/.*Running job: *//' <<<"$last_evt" | tr -d '\r')
  else
    state="idle"
    job=""
  fi

  # A runner mid-restart has no .runner file yet; that is normal, not an error.
  local reg
  reg=$(timeout 5 docker exec "$c" cat /root/actions-runner/.runner 2>/dev/null \
        | jq -r '.agentName // empty' 2>/dev/null)
  : "${reg:=unknown}"

  # Inner daemon usage: build cache is what the 20GB GC cap acts on.
  local dfj cache_b images_b
  dfj=$(timeout 15 docker exec "$c" docker system df --format json 2>/dev/null | head -1)
  cache_b=$(jq -r '(.BuildCache // "0B")' <<<"${dfj:-{\}}" 2>/dev/null)
  images_b=$(jq -r '(.Images // "0B")' <<<"${dfj:-{\}}" 2>/dev/null)

  jq -n \
    --arg name "$c" --arg reg "$reg" --arg state "$state" --arg job "$job" \
    --arg uptime "$uptime" --argjson cpu "${cpu:-0}" \
    --arg mem_used "${mem_used:-0B}" --arg mem_limit "${mem_limit:-0B}" \
    --arg cache "${cache_b:-0B}" --arg images "${images_b:-0B}" \
    '{name:$name, registration:$reg, state:$state, job:$job, uptime:$uptime,
      cpu_percent:$cpu, mem_used:$mem_used, mem_limit:$mem_limit,
      build_cache:$cache, images:$images}'
}

runners_json="[]"
for c in $RUNNERS; do
  r=$(emit_runner "$c") || continue
  runners_json=$(jq -c ". + [$r]" <<<"$runners_json")
done

jq -n \
  --arg generated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson used "$disk_used" --argjson total "$disk_total" \
  --argjson runners "$runners_json" \
  '{generated:$generated,
    disk:{used_bytes:$used, total_bytes:$total,
          percent: (if $total > 0 then (($used * 100) / $total | floor) else 0 end)},
    runners:$runners}'
