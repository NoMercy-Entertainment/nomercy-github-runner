#!/bin/bash
# Dashboard entrypoint: refresh status.json on a loop and serve the page.
#
# Deliberately minimal - a background refresh loop plus python's built-in HTTP
# server. No framework, no build step, nothing to keep patched.

set -uo pipefail

DATA_DIR=/data
INTERVAL="${REFRESH_INTERVAL:-5}"
PORT="${PORT:-9200}"

mkdir -p "$DATA_DIR"

refresh_loop() {
  while true; do
    # Write to a temp file and move it into place. Without this the page can
    # fetch a half-written file and render garbage, which looks like a bug in
    # the collector rather than a race.
    if /app/collect.sh > "$DATA_DIR/.status.tmp" 2>"$DATA_DIR/collect.err"; then
      mv "$DATA_DIR/.status.tmp" "$DATA_DIR/status.json"
    else
      echo "[serve] collect.sh failed; leaving previous status.json in place" >&2
    fi
    sleep "$INTERVAL"
  done
}

refresh_loop &

# Serve from $DATA_DIR, not /app. /app is bind-mounted read-only, so nothing
# can be linked or written into it - an earlier attempt to symlink the data
# directory under /app failed silently and left status.json 404ing while the
# page itself loaded fine.
cp /app/index.html "$DATA_DIR/index.html"

cd "$DATA_DIR"
echo "[serve] listening on :${PORT}, refreshing every ${INTERVAL}s"
exec python3 -m http.server "$PORT" --bind 0.0.0.0
