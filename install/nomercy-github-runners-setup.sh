#!/usr/bin/env bash
#
# NoMercy GitHub Actions Runners - Linux and macOS installer
#
# Linux:  runners run as containers on a SECOND Docker daemon with its own
#         data root, so their storage is a separate pool from the Docker you
#         already run. A runaway build cannot fill your existing storage.
#
# macOS:  runners install natively under launchd, with all their data under a
#         path you choose. There is no separate Docker pool on macOS - see the
#         note the installer prints before it does anything.
#
#   curl -fsSLO https://raw.githubusercontent.com/NoMercy-Entertainment/nomercy-github-runner/master/install/nomercy-github-runners-setup.sh
#   chmod +x nomercy-github-runners-setup.sh
#   sudo ./nomercy-github-runners-setup.sh          # Linux needs root
#   ./nomercy-github-runners-setup.sh               # macOS does not
#
# Written for bash 3.2, which is what macOS still ships. No associative
# arrays, no ${var,,}, no mapfile.

set -uo pipefail

RUNNER_VERSION='2.336.0'
RUNNER_IMAGE='ghcr.io/nomercy-entertainment/nomercy-github-runner:latest'
REPO_URL='https://github.com/NoMercy-Entertainment/nomercy-github-runner.git'
MIN_FREE_GB=40
DEFAULT_LABELS='self-hosted,Linux,X64'
DEFAULT_COUNT=2
SERVICE_NAME='nomercy-runners-docker'

# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_CYAN='\033[36m'; C_GREEN='\033[32m'
  C_YELLOW='\033[33m'; C_RED='\033[31m'; C_GREY='\033[90m'; C_WHITE='\033[97m'
else
  C_RESET=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_GREY=''; C_WHITE=''
fi

head_()  { printf "\n${C_CYAN}%s${C_RESET}\n" "$1"
           printf "${C_CYAN}%s${C_RESET}\n" "$(echo "$1" | sed 's/./-/g')"; }
ok_()    { printf "  ${C_GREEN}[ ok ]${C_RESET} %s\n" "$1"; }
warn_()  { printf "  ${C_YELLOW}[warn]${C_RESET} %s\n" "$1"; }
info_()  { printf "  ${C_GREY}%s${C_RESET}\n" "$1"; }
fail_()  { printf "\n  ${C_RED}[FAIL]${C_RESET} %s\n" "$1"
           [ $# -gt 1 ] && printf "         ${C_YELLOW}%s${C_RESET}\n" "$2"
           printf "\n"; exit 1; }

# --------------------------------------------------------------------------
# platform
# --------------------------------------------------------------------------

case "$(uname -s)" in
  Linux)  PLATFORM=linux ;;
  Darwin) PLATFORM=macos ;;
  *) fail_ "Unsupported operating system: $(uname -s)" \
           "This installer supports Linux and macOS. Use the .ps1 script on Windows." ;;
esac

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l|armv6l) ARCH=arm ;;
  *) fail_ "Unsupported architecture: $ARCH_RAW" ;;
esac

if [ "$PLATFORM" = macos ] && [ "$ARCH" = arm ]; then
  fail_ "32-bit ARM is not supported on macOS."
fi

# --------------------------------------------------------------------------
# defaults that depend on platform
# --------------------------------------------------------------------------

if [ "$PLATFORM" = linux ]; then
  DEFAULT_PATH=/opt/nomercy-runners
else
  DEFAULT_PATH="$HOME/NoMercyRunners"
  DEFAULT_LABELS='self-hosted,macOS'
fi

# --------------------------------------------------------------------------
# argument parsing (so the wizard can be scripted)
# --------------------------------------------------------------------------

ORG=''; TOKEN=''; DATA_PATH=''; RUNNER_GROUP=''; LABELS=''
RUNNER_COUNT=0; CPU_LIMIT=''; MEM_LIMIT=''; DASH_PORT=0
WANT_DASHBOARD=''; NON_INTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --org)          ORG="$2"; shift 2 ;;
    --token)        TOKEN="$2"; shift 2 ;;
    --path)         DATA_PATH="$2"; shift 2 ;;
    --group)        RUNNER_GROUP="$2"; shift 2 ;;
    --labels)       LABELS="$2"; shift 2 ;;
    --count)        RUNNER_COUNT="$2"; shift 2 ;;
    --cpu)          CPU_LIMIT="$2"; shift 2 ;;
    --mem)          MEM_LIMIT="$2"; shift 2 ;;
    --dashboard-port) DASH_PORT="$2"; shift 2 ;;
    --no-dashboard) WANT_DASHBOARD=0; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) fail_ "Unknown option: $1" "Run with --help to see the options." ;;
  esac
done

# Whitespace is never meaningful in any of these. A runner group of " " is
# not blank - it reaches config.sh as a real group name and registration fails
# with "Could not find any self-hosted runner group named ' '", leaving the
# runner in a restart loop. Normalise once, here.
trim_() { printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }
ORG="$(trim_ "$ORG")"; TOKEN="$(trim_ "$TOKEN")"; DATA_PATH="$(trim_ "$DATA_PATH")"
RUNNER_GROUP="$(trim_ "$RUNNER_GROUP")"; LABELS="$(trim_ "$LABELS")"
CPU_LIMIT="$(trim_ "$CPU_LIMIT")"; MEM_LIMIT="$(trim_ "$MEM_LIMIT")"

# --------------------------------------------------------------------------
# input helpers
# --------------------------------------------------------------------------

ask_() {
  # ask_ <prompt> <default> [validator-regex] [hint]
  _prompt="$1"; _default="$2"; _regex="${3:-}"; _hint="${4:-}"
  if [ "$NON_INTERACTIVE" = 1 ]; then
    [ -n "$_default" ] || fail_ "Missing required value: $_prompt" \
        "Pass it as an option when using --non-interactive."
    printf '%s' "$_default"; return
  fi
  while true; do
    if [ -n "$_default" ]; then
      printf "\n  ${C_WHITE}%s${C_RESET} [%s]\n  > " "$_prompt" "$_default" >&2
    else
      printf "\n  ${C_WHITE}%s${C_RESET}\n  > " "$_prompt" >&2
    fi
    read -r _answer
    [ -n "$_answer" ] || _answer="$_default"
    if [ -z "$_answer" ]; then warn_ "A value is required." >&2; continue; fi
    if [ -n "$_regex" ] && ! printf '%s' "$_answer" | grep -Eq "$_regex"; then
      warn_ "$_hint" >&2; continue
    fi
    printf '%s' "$_answer"; return
  done
}

ask_secret_() {
  [ "$NON_INTERACTIVE" = 1 ] && fail_ "Missing token" "Pass --token when using --non-interactive."
  printf "\n  ${C_WHITE}%s${C_RESET}\n  > " "$1" >&2
  read -r -s _secret
  printf "\n" >&2
  printf '%s' "$_secret"
}

ask_yn_() {
  # ask_yn_ <prompt> <default:y|n>  -> echoes 1 or 0
  _p="$1"; _d="$2"
  if [ "$NON_INTERACTIVE" = 1 ]; then
    [ "$_d" = y ] && { printf '1'; return; }; printf '0'; return
  fi
  [ "$_d" = y ] && _hint='Y/n' || _hint='y/N'
  while true; do
    printf "\n  ${C_WHITE}%s${C_RESET} [%s]\n  > " "$_p" "$_hint" >&2
    read -r _a
    _a="$(printf '%s' "$_a" | tr 'A-Z' 'a-z')"
    [ -z "$_a" ] && _a="$_d"
    case "$_a" in
      y|yes) printf '1'; return ;;
      n|no)  printf '0'; return ;;
      *) warn_ "Answer y or n." >&2 ;;
    esac
  done
}

# Free space in GB for a path that may not exist yet - walk up to the nearest
# existing ancestor, since df cannot stat what is not there.
free_gb_() {
  _p="$1"
  while [ -n "$_p" ] && [ ! -d "$_p" ]; do
    _parent="$(dirname "$_p")"
    [ "$_parent" = "$_p" ] && break
    _p="$_parent"
  done
  [ -d "$_p" ] || { printf '0'; return; }
  df -Pk "$_p" 2>/dev/null | awk 'NR==2 {printf "%.1f", $4/1048576}'
}

device_of_() {
  _p="$1"
  while [ -n "$_p" ] && [ ! -e "$_p" ]; do
    _parent="$(dirname "$_p")"
    [ "$_parent" = "$_p" ] && break
    _p="$_parent"
  done
  df -P "$_p" 2>/dev/null | awk 'NR==2 {print $1}'
}

# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

check_root_() {
  [ "$PLATFORM" = linux ] || return 0
  [ "$(id -u)" -eq 0 ] && return 0
  # Deliberately not re-executing under sudo. Escalating privileges on
  # someone's behalf without being asked is not this script's decision.
  if command -v sudo >/dev/null 2>&1; then
    fail_ "This needs root on Linux to install a systemd service." \
          "Re-run as:  sudo $0 $*"
  else
    fail_ "This needs root on Linux to install a systemd service, and sudo was not found." \
          "Re-run as root."
  fi
}

preflight_linux_() {
  head_ 'Checking this machine'
  ok_ "Linux, $ARCH"

  command -v systemctl >/dev/null 2>&1 || \
    fail_ "systemd was not found." \
          "This installer manages the runners' Docker engine as a systemd service."
  ok_ 'systemd is available'

  if ! command -v dockerd >/dev/null 2>&1; then
    warn_ 'The Docker engine (dockerd) is not installed.'
    info_ 'The runners need it. Install it first, for example:'
    info_ '  curl -fsSL https://get.docker.com | sh'
    fail_ "dockerd not found." "Install Docker Engine, then re-run this script."
  fi
  ok_ "Docker engine present ($(dockerd --version 2>/dev/null | head -1))"

  if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    fail_ "A service named ${SERVICE_NAME} already exists." \
          "Remove the previous install with nomercy-github-runners-uninstall.sh first."
  fi
  ok_ "The name ${SERVICE_NAME} is free"

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    SYSTEM_DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
    info_ "You already run Docker (root: ${SYSTEM_DOCKER_ROOT:-unknown})."
    info_ 'The runners will NOT use it. They get their own engine and storage.'
  else
    SYSTEM_DOCKER_ROOT=''
  fi
}

preflight_macos_() {
  head_ 'Checking this machine'

  _ver="$(sw_vers -productVersion 2>/dev/null)"
  _major="$(printf '%s' "$_ver" | cut -d. -f1)"
  if [ -n "$_major" ] && [ "$_major" -lt 11 ] 2>/dev/null; then
    fail_ "macOS $_ver is too old." "The runner needs macOS 11 (Big Sur) or later."
  fi
  ok_ "macOS ${_ver:-unknown}, $ARCH"

  command -v curl >/dev/null 2>&1 || fail_ "curl was not found."
  ok_ 'curl is available'
}

# --------------------------------------------------------------------------
# GitHub validation
# --------------------------------------------------------------------------

check_github_() {
  _org="$1"; _tok="$2"
  _code="$(curl -sS -o /tmp/nm_gh_check.$$ -w '%{http_code}' -X POST \
      -H "Authorization: Bearer ${_tok}" \
      -H 'Accept: application/vnd.github+json' \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      --max-time 25 \
      "https://api.github.com/orgs/${_org}/actions/runners/registration-token" 2>/dev/null)"
  rm -f "/tmp/nm_gh_check.$$"
  case "$_code" in
    201) return 0 ;;
    401) GH_WHY='The token was rejected (401). Check it was copied in full and has not expired.'; return 1 ;;
    403) GH_WHY="The token lacks permission (403). It needs admin:org, or manage_self_hosted_runners on '${_org}'."; return 1 ;;
    404) GH_WHY="Organisation '${_org}' was not found, or this token cannot see it (404). Check the spelling."; return 1 ;;
    000) GH_WHY='Could not reach api.github.com. Check network access.'; return 1 ;;
    *)   GH_WHY="GitHub returned HTTP ${_code}."; return 1 ;;
  esac
}

# --------------------------------------------------------------------------
# wizard
# --------------------------------------------------------------------------

wizard_() {
  head_ 'GitHub'

  [ -n "$ORG" ] || ORG="$(ask_ 'GitHub organisation' 'NoMercy-Entertainment' \
      '^[A-Za-z0-9._-]+$' 'Use the organisation name as it appears in the URL, not the full URL.')"

  while true; do
    [ -n "$TOKEN" ] || TOKEN="$(ask_secret_ "Personal access token for '${ORG}' (input hidden)")"
    info_ 'Checking the token against GitHub...'
    if check_github_ "$ORG" "$TOKEN"; then
      ok_ 'Token works and can register runners'
      break
    fi
    warn_ "$GH_WHY"
    [ "$NON_INTERACTIVE" = 1 ] && fail_ 'Token validation failed.' "$GH_WHY"
    TOKEN=''
  done

  [ -n "$RUNNER_GROUP" ] || {
    RUNNER_GROUP="$(ask_ 'Runner group (leave blank for the org default)' 'NONE')"
    [ "$RUNNER_GROUP" = 'NONE' ] && RUNNER_GROUP=''
  }

  head_ 'Runners'

  [ "$RUNNER_COUNT" -gt 0 ] 2>/dev/null || RUNNER_COUNT="$(ask_ 'How many runners' \
      "$DEFAULT_COUNT" '^([1-9]|[12][0-9]|3[0-2])$' 'Enter a whole number between 1 and 32.')"

  [ -n "$LABELS" ] || LABELS="$(ask_ 'Runner labels (comma separated)' "$DEFAULT_LABELS")"

  if [ "$PLATFORM" = linux ]; then
    [ -n "$CPU_LIMIT" ] || CPU_LIMIT="$(ask_ 'CPU cores per runner (0 = unlimited)' '0' \
        '^[0-9]+(\.[0-9]+)?$' 'Enter a number such as 4. Use 0 for no limit.')"
    [ -n "$MEM_LIMIT" ] || MEM_LIMIT="$(ask_ 'Memory per runner, e.g. 8g (0 = unlimited)' '0' \
        '^[0-9]+[GgMm]?$' 'Enter a size such as 8g or 8192m. Use 0 for no limit.')"
  else
    CPU_LIMIT=0; MEM_LIMIT=0
  fi

  head_ 'Storage'
  if [ "$PLATFORM" = linux ]; then
    info_ 'This is where the runners keep everything: their Docker images,'
    info_ 'build caches and workspaces. It is a separate pool from the Docker'
    info_ 'you already run, so the runners cannot fill your existing storage.'
  else
    info_ 'This is where the runners keep their binaries, work directories'
    info_ 'and tool caches. Builds can be large, so pick a volume with room.'
  fi

  while true; do
    [ -n "$DATA_PATH" ] || DATA_PATH="$(ask_ 'Where should the runners store their data' "$DEFAULT_PATH")"
    _free="$(free_gb_ "$DATA_PATH")"
    printf "\n"
    info_ "path : $DATA_PATH"
    info_ "free : ${_free} GB"

    _enough="$(awk -v a="$_free" -v b="$MIN_FREE_GB" 'BEGIN{print (a>=b)?1:0}')"
    if [ "$_enough" != 1 ]; then
      warn_ "That volume has ${_free} GB free. At least ${MIN_FREE_GB} GB is recommended."
      [ "$NON_INTERACTIVE" = 1 ] && fail_ 'Not enough free space.' "${_free} GB available."
      if [ "$(ask_yn_ 'Use it anyway' n)" = 0 ]; then DATA_PATH=''; continue; fi
    fi
    break
  done

  # The daemons are separate either way, but if the storage is not, the
  # isolation is only logical - and separating storage is the entire reason
  # someone runs this installer. Say so before they commit.
  if [ "$PLATFORM" = linux ] && [ -n "${SYSTEM_DOCKER_ROOT:-}" ]; then
    _a="$(device_of_ "$DATA_PATH")"
    _b="$(device_of_ "$SYSTEM_DOCKER_ROOT")"
    if [ -n "$_a" ] && [ "$_a" = "$_b" ]; then
      printf "\n"
      warn_ "That path is on the same filesystem ($_a) as your existing Docker."
      info_ 'The runners get their own daemon and their own data root, but they'
      info_ 'share the underlying disk. A runaway build could still fill it.'
      info_ 'Choose a path on a different volume for real isolation.'
      if [ "$NON_INTERACTIVE" != 1 ]; then
        if [ "$(ask_yn_ 'Continue with a shared disk anyway' n)" = 0 ]; then
          fail_ 'Cancelled so you can pick a different volume.' \
                'Re-run and choose a path on another disk.'
        fi
      fi
    fi
  fi

  if [ "$PLATFORM" = linux ]; then
    head_ 'Dashboard'
    [ -n "$WANT_DASHBOARD" ] || WANT_DASHBOARD="$(ask_yn_ 'Install the web dashboard for managing these runners' y)"
    if [ "$WANT_DASHBOARD" = 1 ] && [ "$DASH_PORT" -le 0 ] 2>/dev/null; then
      DASH_PORT="$(ask_ 'Dashboard port' '9200' '^[0-9]{4,5}$' 'Enter a port between 1024 and 65535.')"
    fi
  else
    WANT_DASHBOARD=0
  fi
}

# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

summary_() {
  _free="$(free_gb_ "$DATA_PATH")"
  head_ 'About to install'

  printf "\n  ${C_YELLOW}STORAGE${C_RESET}\n"
  printf "    Location        %s\n" "$DATA_PATH"
  printf "    Free on volume  %s GB\n" "$_free"

  printf "\n  ${C_YELLOW}GITHUB${C_RESET}\n"
  printf "    Organisation    %s\n" "$ORG"
  if [ -n "$RUNNER_GROUP" ]; then
    printf "    Runner group    %s\n" "$RUNNER_GROUP"
  else
    printf "    Runner group    (org default)\n"
  fi
  printf "    Token           validated\n"

  printf "\n  ${C_YELLOW}RUNNERS${C_RESET}\n"
  printf "    Count           %s\n" "$RUNNER_COUNT"
  printf "    Labels          %s\n" "$LABELS"
  printf "    Runner version  %s\n" "$RUNNER_VERSION"

  printf "\n  ${C_YELLOW}ENGINE${C_RESET}\n"
  if [ "$PLATFORM" = linux ]; then
    [ "$CPU_LIMIT" = 0 ] && _c='unlimited' || _c="$CPU_LIMIT cores"
    [ "$MEM_LIMIT" = 0 ] && _m='unlimited' || _m="$MEM_LIMIT"
    printf "    CPU per runner  %s\n" "$_c"
    printf "    Mem per runner  %s\n" "$_m"
    printf "    Isolation       separate Docker daemon, data root under the path above\n"
    printf "    Service         %s.service (systemd)\n" "$SERVICE_NAME"
    if [ "$WANT_DASHBOARD" = 1 ]; then
      printf "    Dashboard       http://localhost:%s\n" "$DASH_PORT"
    else
      printf "    Dashboard       not installed\n"
    fi
  else
    printf "    Runner form     native processes under launchd\n"
    printf "    Isolation       none for Docker (see the note below)\n"
  fi

  if [ "$PLATFORM" = macos ]; then
    printf "\n  ${C_YELLOW}NOTE FOR macOS${C_RESET}\n"
    printf "    The runner installs natively and its data lives under the path\n"
    printf "    you chose. There is NO separate Docker storage pool on macOS.\n"
    printf "    If a workflow uses Docker it will use whatever Docker is\n"
    printf "    installed here and share its storage. Xcode-based workflows\n"
    printf "    are unaffected.\n"
  fi

  printf "\n  ${C_GREY}Nothing has been created yet.${C_RESET}\n"
}

# --------------------------------------------------------------------------
# install - linux
# --------------------------------------------------------------------------

READY_COUNT=0
DOCKER_SOCK=''

install_linux_() {
  DOCKER_SOCK="${DATA_PATH}/docker.sock"

  head_ 'Creating the isolated Docker engine'
  mkdir -p "${DATA_PATH}/data" "${DATA_PATH}/exec" || fail_ "Could not create ${DATA_PATH}."

  # Every path distinct from the system daemon. Sharing any one of data-root,
  # exec-root or the pidfile makes the two daemons fight over the same state.
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=NoMercy runners Docker engine (isolated from the system Docker)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd \\
  --data-root ${DATA_PATH}/data \\
  --exec-root ${DATA_PATH}/exec \\
  --pidfile ${DATA_PATH}/docker.pid \\
  --host unix://${DOCKER_SOCK}
Restart=always
RestartSec=5
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}" >/dev/null 2>&1 || \
    fail_ "Could not start ${SERVICE_NAME}." "Check: journalctl -u ${SERVICE_NAME} -n 40"

  _i=0
  while [ $_i -lt 40 ]; do
    docker -H "unix://${DOCKER_SOCK}" info >/dev/null 2>&1 && break
    _i=$((_i + 1)); sleep 1
  done
  docker -H "unix://${DOCKER_SOCK}" info >/dev/null 2>&1 || \
    fail_ "The isolated engine did not start." "Check: journalctl -u ${SERVICE_NAME} -n 40"

  _root="$(docker -H "unix://${DOCKER_SOCK}" info --format '{{.DockerRootDir}}' 2>/dev/null)"
  ok_ "Engine running, data root: ${_root}"

  # Isolation is the whole point, so demonstrate it rather than assert it.
  _seen="$(docker -H "unix://${DOCKER_SOCK}" ps -aq 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$_seen" = 0 ]; then
    ok_ 'The new engine sees none of your existing containers'
  else
    warn_ "The new engine already lists ${_seen} container(s). That is unexpected."
  fi

  head_ "Creating ${RUNNER_COUNT} runner(s)"

  info_ 'Fetching the runner start script...'
  rm -rf "${DATA_PATH}/repo"
  git clone --depth 1 "$REPO_URL" "${DATA_PATH}/repo" >/dev/null 2>&1 || \
    fail_ "Could not clone ${REPO_URL}."
  install -m 0755 "${DATA_PATH}/repo/scripts/start.sh" "${DATA_PATH}/start.sh"
  ok_ 'Runner sources in place'

  info_ 'Pulling the runner image. This is large and takes a while.'
  docker -H "unix://${DOCKER_SOCK}" pull "$RUNNER_IMAGE" >/dev/null 2>&1 || \
    fail_ "Could not pull ${RUNNER_IMAGE}."
  ok_ 'Runner image pulled'

  _limits=''
  [ "$CPU_LIMIT" != 0 ] && _limits="$_limits --cpus $CPU_LIMIT"
  [ "$MEM_LIMIT" != 0 ] && _limits="$_limits --memory $MEM_LIMIT"

  _n=1
  while [ "$_n" -le "$RUNNER_COUNT" ]; do
    _name="nomercy-runner-${_n}"
    docker -H "unix://${DOCKER_SOCK}" rm -f "$_name" >/dev/null 2>&1 || true
    # --stop-timeout 60: Engine 29.x creates containers with StopTimeout=1
    # (moby/moby#52775). start.sh deregisters the runner on SIGTERM and needs
    # a few seconds; at 1s it is killed mid-flight and the registration is
    # orphaned in the organisation.
    # shellcheck disable=SC2086
    docker -H "unix://${DOCKER_SOCK}" run -d \
      --name "$_name" \
      --label nomercy.runner=true \
      --privileged --restart unless-stopped --stop-timeout 60 \
      --tmpfs /tmp \
      -v "${DATA_PATH}/start.sh:/root/start.sh:ro" \
      -e "GH_TOKEN=${TOKEN}" \
      -e "GITHUB_ORG=${ORG}" \
      -e "RUNNER_LABELS=${LABELS}" \
      -e "RUNNER_GROUP=${RUNNER_GROUP}" \
      $_limits \
      "$RUNNER_IMAGE" >/dev/null 2>&1 || fail_ "Could not create ${_name}."
    ok_ "Created ${_name}"
    _n=$((_n + 1))
  done

  info_ 'Waiting for the runners to register with GitHub...'
  _t=0
  while [ $_t -lt 40 ]; do
    sleep 6
    READY_COUNT=0
    for _c in $(docker -H "unix://${DOCKER_SOCK}" ps --format '{{.Names}}' 2>/dev/null | grep '^nomercy-runner-'); do
      if docker -H "unix://${DOCKER_SOCK}" logs --tail 40 "$_c" 2>&1 | grep -q 'Listening for Jobs'; then
        READY_COUNT=$((READY_COUNT + 1))
      fi
    done
    printf "    ${C_GREY}%s of %s ready${C_RESET}\n" "$READY_COUNT" "$RUNNER_COUNT"
    [ "$READY_COUNT" -ge "$RUNNER_COUNT" ] && break
    _t=$((_t + 1))
  done

  if [ "$READY_COUNT" -lt "$RUNNER_COUNT" ]; then
    warn_ "${READY_COUNT} of ${RUNNER_COUNT} runners registered."
    # The container usually says exactly what is wrong - a bad runner group, a
    # rejected token. Making the operator go digging for it is a poor first
    # experience.
    _err="$(docker -H "unix://${DOCKER_SOCK}" logs --tail 40 nomercy-runner-1 2>&1 |
            grep -iE 'error|could not|denied|not found|invalid' | tail -4)"
    if [ -n "$_err" ]; then
      printf "\n  ${C_YELLOW}The runner reported:${C_RESET}\n"
      printf '%s\n' "$_err" | sed "s/^/    ${C_RED}/; s/\$/${C_RESET}/"
    fi
  else
    ok_ "All ${RUNNER_COUNT} runners are listening for jobs"
  fi

  [ "$WANT_DASHBOARD" = 1 ] && install_dashboard_linux_
  return 0
}

install_dashboard_linux_() {
  head_ 'Installing the dashboard'
  ( cd "${DATA_PATH}/repo/dashboard" &&
    docker -H "unix://${DOCKER_SOCK}" build -t nomercy/runner-dashboard:local . >/dev/null 2>&1 ) || {
      warn_ 'Could not build the dashboard image. Skipping it.'; return 0; }
  docker -H "unix://${DOCKER_SOCK}" rm -f nomercy-runner-dashboard >/dev/null 2>&1 || true
  docker -H "unix://${DOCKER_SOCK}" run -d \
    --name nomercy-runner-dashboard --restart unless-stopped \
    -p "${DASH_PORT}:9200" \
    -v "${DOCKER_SOCK}:/var/run/docker.sock" \
    -v "${DATA_PATH}:/repo" \
    -v nomercy-dashboard-data:/data \
    nomercy/runner-dashboard:local >/dev/null 2>&1 || {
      warn_ 'Could not start the dashboard. Skipping it.'; return 0; }
  ok_ "Dashboard running on http://localhost:${DASH_PORT}"
  info_ 'It will ask you to set a password the first time you open it.'
}

# --------------------------------------------------------------------------
# install - macos
# --------------------------------------------------------------------------

install_macos_() {
  head_ "Installing ${RUNNER_COUNT} runner(s)"

  _tar="actions-runner-osx-${ARCH}-${RUNNER_VERSION}.tar.gz"
  _url="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${_tar}"

  mkdir -p "$DATA_PATH" || fail_ "Could not create ${DATA_PATH}."
  if [ ! -f "${DATA_PATH}/${_tar}" ]; then
    info_ "Downloading runner ${RUNNER_VERSION} for ${ARCH}..."
    curl -fsSL -o "${DATA_PATH}/${_tar}" "$_url" || fail_ "Could not download ${_url}."
  fi
  ok_ 'Runner package ready'

  _n=1
  while [ "$_n" -le "$RUNNER_COUNT" ]; do
    _dir="${DATA_PATH}/runner-${_n}"
    _name="$(hostname -s 2>/dev/null || echo mac)-runner-${_n}"

    mkdir -p "$_dir"
    tar xzf "${DATA_PATH}/${_tar}" -C "$_dir" || fail_ "Could not extract into ${_dir}."

    _tok="$(curl -sS -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/orgs/${ORG}/actions/runners/registration-token" \
        | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    [ -n "$_tok" ] || fail_ "Could not get a registration token for ${ORG}."

    _grp=''
    [ -n "$RUNNER_GROUP" ] && _grp="--runnergroup $RUNNER_GROUP"

    # shellcheck disable=SC2086
    ( cd "$_dir" && ./config.sh --unattended --disableupdate --replace \
        --url "https://github.com/${ORG}" --token "$_tok" \
        --name "$_name" --labels "$LABELS" --work "${_dir}/_work" $_grp ) >/dev/null 2>&1 \
      || fail_ "Could not configure ${_name}." "Try running ./config.sh in ${_dir} by hand to see why."

    # svc.sh writes and loads the launchd plist. Do not hand-write one: the
    # runner requires runsvc.sh as the entry point, and svc.sh is the
    # supported path.
    ( cd "$_dir" && ./svc.sh install >/dev/null 2>&1 && ./svc.sh start >/dev/null 2>&1 ) \
      || fail_ "Could not install the launchd service for ${_name}."

    ok_ "Installed and started ${_name}"
    READY_COUNT=$((READY_COUNT + 1))
    _n=$((_n + 1))
  done
}

# --------------------------------------------------------------------------
# next steps
# --------------------------------------------------------------------------

next_steps_() {
  if [ "$READY_COUNT" -ge "$RUNNER_COUNT" ]; then
    head_ 'Done'
    printf "\n  ${C_GREEN}Runners      %s, registered to %s${C_RESET}\n" "$RUNNER_COUNT" "$ORG"
  else
    head_ 'Finished with problems'
    printf "\n  ${C_RED}Runners      %s of %s registered to %s${C_RESET}\n" \
        "$READY_COUNT" "$RUNNER_COUNT" "$ORG"
    printf "  ${C_RED}             The environment is installed; the runners are not all up.${C_RESET}\n"
  fi
  printf "  ${C_GREEN}Storage      %s${C_RESET}\n" "$DATA_PATH"

  if [ "$PLATFORM" = linux ]; then
    printf "  ${C_GREEN}Isolation    separate Docker daemon (%s)${C_RESET}\n" "$SERVICE_NAME"
    [ "$WANT_DASHBOARD" = 1 ] && \
      printf "  ${C_GREEN}Dashboard    http://localhost:%s${C_RESET}\n" "$DASH_PORT"
    printf "\n  ${C_YELLOW}Useful commands${C_RESET}\n"
    printf "    See the runners     docker -H unix://%s ps\n" "$DOCKER_SOCK"
    printf "    Follow one          docker -H unix://%s logs -f nomercy-runner-1\n" "$DOCKER_SOCK"
    printf "    Engine status       systemctl status %s\n" "$SERVICE_NAME"
    printf "    Remove everything   ./nomercy-github-runners-uninstall.sh\n"
    printf "\n  ${C_GREY}Your own Docker was not touched. These runners use a separate${C_RESET}\n"
    printf "  ${C_GREY}daemon and a separate data root.${C_RESET}\n"
  else
    printf "  ${C_GREEN}Service      launchd, per runner${C_RESET}\n"
    printf "\n  ${C_YELLOW}Useful commands${C_RESET}\n"
    printf "    Status              cd %s/runner-1 && ./svc.sh status\n" "$DATA_PATH"
    printf "    Stop one            cd %s/runner-1 && ./svc.sh stop\n" "$DATA_PATH"
    printf "    Remove everything   ./nomercy-github-runners-uninstall.sh\n"
    printf "\n  ${C_GREY}Reminder: macOS runners share whatever Docker is installed here.${C_RESET}\n"
  fi
  printf "\n"
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

printf "\n  ${C_CYAN}NoMercy GitHub Actions Runners${C_RESET}\n"
printf "  ${C_GREY}Standalone installer for %s${C_RESET}\n" "$PLATFORM"

check_root_ "$@"
if [ "$PLATFORM" = linux ]; then preflight_linux_; else preflight_macos_; fi
wizard_
summary_

if [ "$(ask_yn_ 'Proceed with the install' y)" = 0 ]; then
  printf "\n"; info_ 'Cancelled. Nothing was created.'; exit 0
fi

if [ "$PLATFORM" = linux ]; then
  install_linux_
else
  install_macos_
fi
next_steps_
exit 0
