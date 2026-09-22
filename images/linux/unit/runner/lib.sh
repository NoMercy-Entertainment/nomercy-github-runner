# Shared by /runner/run, /runner/register and /runner/deregister.
#
# The unit layout of design 15.1. The agent mounts five volumes, all named from
# the runner_id, and tells the image where they are:
#
#   RUNNER_WORK_DIR   /runner/work    jobs' workspace
#   RUNNER_CACHE_DIR  /runner/cache   tool cache
#   RUNNER_REG_DIR    /runner/reg     the forge registration, and nothing else
#   RUNNER_LOG_DIR    /runner/logs
#   /var/lib/docker                   the nested engine
#
# RUNNER_KIND is baked into each image: github or forgejo. Everything that
# differs between the two forges is in a `case` on it below, and nothing else
# in these scripts knows which forge it serves.

RUNNER_KIND="${RUNNER_KIND:?the image must set RUNNER_KIND}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/runner/work}"
RUNNER_CACHE_DIR="${RUNNER_CACHE_DIR:-/runner/cache}"
RUNNER_REG_DIR="${RUNNER_REG_DIR:-/runner/reg}"
RUNNER_LOG_DIR="${RUNNER_LOG_DIR:-/runner/logs}"
HOME="${HOME:-/runner/work/.home}"
# Writable runner files belong to the quota-backed registration filesystem.
RUNNER_HOME="${RUNNER_HOME:-$RUNNER_REG_DIR/actions-runner}"
FORGEJO_RUNNER_BIN="${FORGEJO_RUNNER_BIN:-forgejo-runner}"

# The files a registration leaves, per forge. They live in RUNNER_REG_DIR, the
# one volume a recreate throws away, so a recreated runner registers afresh.
case "$RUNNER_KIND" in
  github)  REG_FILES=".runner .credentials .credentials_rsaparams" ;;
  forgejo) REG_FILES=".runner" ;;
  *) echo "unknown RUNNER_KIND $RUNNER_KIND" >&2; exit 64 ;;
esac

registered() {
  [ -s "$RUNNER_REG_DIR/.runner" ]
}

prepare_runner_home() {
  [ "$RUNNER_KIND" = github ] || return 0
  mkdir -p "$RUNNER_REG_DIR"
  (
    flock -w 120 8
    if [ ! -f "$RUNNER_HOME/.image-complete" ]; then
      mkdir -p "$RUNNER_HOME"
      cp -a /opt/actions-runner-image/. "$RUNNER_HOME/"
      touch "$RUNNER_HOME/.image-complete"
    fi
  ) 8>"$RUNNER_REG_DIR/.image.lock"
}

refuse_outside_a_container() {
  # These are unit entry points. Run on a worker itself, `run` would rewrite
  # /etc/docker/daemon.json - the worker's own engine - and restart dockerd
  # under every runner there. /.dockerenv exists in every container the engine
  # starts and on no host.
  if [ ! -f /.dockerenv ]; then
    echo "REFUSING: $0 is a unit entry point, not a host script." >&2
    exit 1
  fi
}
