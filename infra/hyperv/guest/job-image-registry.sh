#!/usr/bin/env bash
# The job-image registry of a Linux worker: where the Forgejo units' nested
# engines pull the images their jobs run in (FORGEJO_RUNNER_LABELS), which no
# public registry has. Before it, those images were `docker load`ed into each
# unit's engine by hand, and a recreate or the unit's own startup prune lost
# them: "pull access denied" (2026-10-27).
#
# Two registries on one store, /var/lib/runner-data/job-images:
#   rnr-job-images       read-only, on the docker0 gateway (172.17.0.1:5000):
#                        what the units pull from. The store is mounted
#                        read-only and the registry runs in read-only mode, so
#                        nothing a job does can change an image.
#   rnr-job-images-push  writable, on 127.0.0.1:5001 of the worker only: where
#                        `push` puts an image the worker's engine has.
# Plain HTTP: the units' engines list the gateway address under
# insecure-registries (images/linux/unit/runner/run), and the worker's engine
# trusts 127.0.0.0/8 by itself.
#
#   job-image-registry.sh up              start both (idempotent)
#   job-image-registry.sh push IMAGE...   copy local images in, same name
#   job-image-registry.sh check IMAGE...  each is served to the units
set -euo pipefail

REGISTRY_IMAGE="${REGISTRY_IMAGE:-registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373}"
STORE="${JOB_IMAGE_STORE:-/var/lib/runner-data/job-images}"
CONF=/etc/runner-job-images
READ_PORT=5000
PUSH_ADDR=127.0.0.1:5001

gateway() {
  docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
}

write_config() {
  local path="$1" readonly="$2"
  cat > "$path.new" <<YAML
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /var/lib/registry
  delete:
    enabled: false
  maintenance:
    uploadpurging:
      enabled: ${3:-true}
      age: 168h
      interval: 24h
      dryrun: false
    readonly:
      enabled: ${readonly}
http:
  addr: :5000
YAML
  mv "$path.new" "$path"
}

# Started, with exactly this image, ports and store; otherwise made again.
ensure() {
  local name="$1" publish="$2" config="$3" mode="$4"
  # The config's hash too: it is bind-mounted, and a rewritten file is a new
  # inode the running registry would never see.
  local want
  want="${REGISTRY_IMAGE}|${publish}|${STORE}:${mode}|$(sha256sum "$config" | cut -c1-16)"
  local have=""
  have="$(docker inspect --format '{{index .Config.Labels "nomercy.job_images"}}' "$name" 2>/dev/null || true)"
  if [ "$have" = "$want" ]; then
    docker start "$name" >/dev/null
    return
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --restart unless-stopped \
    --label "nomercy.job_images=$want" \
    --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
    --read-only --tmpfs /tmp:size=64m \
    -p "$publish" \
    -v "$STORE:/var/lib/registry:$mode" \
    -v "$config:/etc/docker/registry/config.yml:ro" \
    "$REGISTRY_IMAGE" >/dev/null
}

up() {
  install -d -m 755 "$STORE" "$CONF"
  write_config "$CONF/read.yml" true false
  write_config "$CONF/push.yml" false true
  local gw
  gw="$(gateway)"
  [ -n "$gw" ] || { echo "no docker0 gateway" >&2; exit 1; }
  ensure rnr-job-images-push "$PUSH_ADDR:5000" "$CONF/push.yml" rw
  ensure rnr-job-images "$gw:$READ_PORT:5000" "$CONF/read.yml" ro
  # The units reach the gateway through the worker's own input chain, which
  # ufw closes by default; only the bridge may, and only this port.
  if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow in on docker0 to "$gw" port "$READ_PORT" proto tcp \
      comment 'job-image registry for the units' >/dev/null
  fi
  for _ in $(seq 1 30); do
    curl -fsS "http://$gw:$READ_PORT/v2/" >/dev/null 2>&1 \
      && curl -fsS "http://$PUSH_ADDR/v2/" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS "http://$gw:$READ_PORT/v2/" >/dev/null
  curl -fsS "http://$PUSH_ADDR/v2/" >/dev/null
  echo "job-image registry: units pull from $gw:$READ_PORT"
}

manifest_url() {
  local base="$1" image="$2" repo tag
  repo="${image%:*}"; tag="${image##*:}"
  echo "$base/v2/$repo/manifests/$tag"
}

check() {
  local gw
  gw="$(gateway)"
  for image in "$@"; do
    curl -fsS -o /dev/null \
      -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
      -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
      -H 'Accept: application/vnd.oci.image.index.v1+json' \
      "$(manifest_url "http://$gw:$READ_PORT" "$image")"
    echo "served: $gw:$READ_PORT/$image"
  done
}

push() {
  for image in "$@"; do
    case "$image" in */*:*) ;; *) echo "name an image as repo/name:tag: $image" >&2; exit 64 ;; esac
    docker tag "$image" "$PUSH_ADDR/$image"
    docker push -q "$PUSH_ADDR/$image"
    docker rmi "$PUSH_ADDR/$image" >/dev/null
  done
  check "$@"
}

case "${1:-}" in
  up) up ;;
  push) shift; push "$@" ;;
  check) shift; check "$@" ;;
  *) echo "usage: $0 up | push IMAGE... | check IMAGE..." >&2; exit 64 ;;
esac
