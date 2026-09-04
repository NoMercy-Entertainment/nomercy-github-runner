#!/usr/bin/env bash
#
# NoMercy GitHub Actions Runners - Linux and macOS uninstaller
#
# Removes the runners, and on Linux the isolated Docker engine and its systemd
# unit. Asks before deleting your data.
#
#   sudo ./nomercy-github-runners-uninstall.sh     # Linux
#   ./nomercy-github-runners-uninstall.sh          # macOS
#
# Written for bash 3.2 (macOS).

set -uo pipefail

SERVICE_NAME='nomercy-runners-docker'
DATA_PATH=''
DELETE_DATA=''
NON_INTERACTIVE=0
ORG=''
# macOS only. --auto-login installs one root-owned LaunchDaemon, so removing it
# needs root even though nothing else here does. Kept as a password read from a
# prompt or stdin rather than argv, exactly like the installer.
SUDO_PASSWORD=''; SUDO_PASSWORD_FROM_STDIN=0

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

case "$(uname -s)" in
  Linux)  PLATFORM=linux ;;
  Darwin) PLATFORM=macos ;;
  *) printf "Unsupported OS: %s\n" "$(uname -s)"; exit 1 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --path)  DATA_PATH="$2"; shift 2 ;;
    --org)   ORG="$2"; shift 2 ;;
    --delete-data) DELETE_DATA=1; shift ;;
    --keep-data)   DELETE_DATA=0; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --sudo-password-stdin) SUDO_PASSWORD_FROM_STDIN=1; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf "Unknown option: %s\n" "$1"; exit 1 ;;
  esac
done

# Deregister through the API rather than trusting the container to do it on
# shutdown. The graceful path depends on the runner's start.sh handling
# SIGTERM correctly, and an uninstaller cannot assume the version installed
# does. This fallback makes the removal reliable either way.
api_remove_() {
  _org="$1"; _tok="$2"; _agent="$3"
  [ -n "$_org" ] && [ -n "$_tok" ] && [ -n "$_agent" ] || return 1

  _json="$(curl -sS \
      -H "Authorization: Bearer ${_tok}" \
      -H 'Accept: application/vnd.github+json' \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      --max-time 25 \
      "https://api.github.com/orgs/${_org}/actions/runners?per_page=100" 2>/dev/null)"
  [ -n "$_json" ] || return 1

  # Strip newlines BEFORE splitting on '{'. The API pretty-prints, so each
  # runner record spans several lines; splitting alone leaves "id" and "name"
  # on different lines and a line-oriented grep can never see both at once.
  # Flattening first makes each record exactly one line.
  # No jq - it is not guaranteed on the machines this runs on.
  _id="$(printf '%s' "$_json" | tr -d '\n' | tr '{' '\n' \
        | grep -E "\"name\": ?\"${_agent}\"" \
        | grep -oE "\"id\": ?[0-9]+" | grep -oE '[0-9]+' | head -1)"

  # Not found means it is already gone - the outcome we wanted. That must be
  # distinguished from a failed lookup, which must NOT report success.
  if [ -z "$_id" ]; then
    printf '%s' "$_json" | grep -q '"total_count"' && return 0
    return 1
  fi

  _code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
      -H "Authorization: Bearer ${_tok}" \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      --max-time 25 \
      "https://api.github.com/orgs/${_org}/actions/runners/${_id}" 2>/dev/null)"
  [ "$_code" = 204 ]
}

ask_yn_() {
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
    case "$_a" in y|yes) printf '1'; return ;; n|no) printf '0'; return ;; esac
  done
}

if [ "$PLATFORM" = linux ] && [ "$(id -u)" -ne 0 ]; then
  printf "\n  This needs root on Linux to remove the systemd service.\n"
  printf "  Re-run as:  sudo %s %s\n\n" "$0" "$*"
  exit 1
fi

printf "\n  ${C_CYAN}NoMercy GitHub Actions Runners${C_RESET}\n"
printf "  ${C_GREY}Uninstaller${C_RESET}\n"

FAILED=''

# --------------------------------------------------------------------------
# locate the install
# --------------------------------------------------------------------------

head_ 'Looking for the installation'

if [ "$PLATFORM" = linux ]; then
  _unit="/etc/systemd/system/${SERVICE_NAME}.service"
  if [ -z "$DATA_PATH" ] && [ -f "$_unit" ]; then
    # Read the path out of the unit rather than guessing it.
    DATA_PATH="$(sed -n 's|.*--data-root \([^ ]*\)/data.*|\1|p' "$_unit" | head -1)"
  fi
  if [ -z "$DATA_PATH" ]; then
    warn_ "No ${SERVICE_NAME} service found and no --path given."
    info_ 'Nothing to remove. Pass --path if you installed to a custom location.'
    printf "\n"; exit 0
  fi
  ok_ "Install found at ${DATA_PATH}"
  SOCK="${DATA_PATH}/docker.sock"
  RUNNERS="$(docker -H "unix://${SOCK}" ps -a --format '{{.Names}}' 2>/dev/null | grep '^nomercy-runner-' || true)"
else
  [ -n "$DATA_PATH" ] || DATA_PATH="$HOME/NoMercyRunners"
  if [ ! -d "$DATA_PATH" ]; then
    warn_ "No installation found at ${DATA_PATH}."
    info_ 'Pass --path if you installed somewhere else.'
    printf "\n"; exit 0
  fi
  ok_ "Install found at ${DATA_PATH}"
  RUNNERS="$(ls -d "${DATA_PATH}"/runner-* 2>/dev/null || true)"
fi

_count="$(printf '%s' "$RUNNERS" | grep -c . || true)"
info_ "Runners found: ${_count}"

# --------------------------------------------------------------------------
# 1. deregister from GitHub FIRST
# --------------------------------------------------------------------------

head_ 'Deregistering runners from GitHub'
info_ 'This happens first. Removing the runners before deregistering leaves'
info_ 'dead entries in the organisation with no way to identify them.'

if [ -z "$RUNNERS" ]; then
  info_ 'No runners to deregister.'
elif [ "$PLATFORM" = linux ]; then
  for _r in $RUNNERS; do
    _raw="$(docker -H "unix://${SOCK}" exec "$_r" cat /root/actions-runner/.runner 2>/dev/null || true)"
    # The runner writes .runner with a UTF-8 BOM; strip it before parsing.
    _agent="$(printf '%s' "$_raw" | tr -d '\357\273\277' |
              sed -n 's/.*"agentName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ -z "$_agent" ]; then
      warn_ "$_r - could not read its registration (may already be deregistered)"
      continue
    fi
    # Stopping runs start.sh's shutdown handler, which deregisters properly.
    # -t 60 matters: Engine 29.x sets StopTimeout to 1s, which kills
    # deregistration mid-flight and orphans the registration.
    docker -H "unix://${SOCK}" stop -t 60 "$_r" >/dev/null 2>&1 || true
    if docker -H "unix://${SOCK}" logs --tail 20 "$_r" 2>&1 |
         grep -qE 'removal of runner .* succeeded|Runner removed successfully'; then
      ok_ "$_r ($_agent) deregistered"
    elif api_remove_ "$ORG" "${GH_TOKEN:-}" "$_agent"; then
      ok_ "$_r ($_agent) removed via the GitHub API"
    else
      warn_ "$_r ($_agent) could not be deregistered"
      info_ 'Pass --org and set GH_TOKEN to remove it automatically.'
      FAILED="$FAILED $_agent"
    fi
  done
else
  for _d in $RUNNERS; do
    [ -d "$_d" ] || continue
    _agent="$(sed -n 's/.*"agentName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
              "${_d}/.runner" 2>/dev/null | head -1)"
    ( cd "$_d" && ./svc.sh stop >/dev/null 2>&1 && ./svc.sh uninstall >/dev/null 2>&1 ) || true

    # Native runners need an explicit removal token - a registration token is
    # a different credential and config.sh remove silently fails with it.
    if [ -n "$ORG" ] && [ -n "${GH_TOKEN:-}" ]; then
      _rm="$(curl -sS -X POST -H "Authorization: Bearer ${GH_TOKEN}" \
             "https://api.github.com/orgs/${ORG}/actions/runners/remove-token" \
             | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
      if [ -n "$_rm" ] && ( cd "$_d" && ./config.sh remove --token "$_rm" >/dev/null 2>&1 ); then
        ok_ "${_agent:-$_d} deregistered"
        continue
      fi
    fi
    if api_remove_ "$ORG" "${GH_TOKEN:-}" "$_agent"; then
      ok_ "${_agent} removed via the GitHub API"
      continue
    fi
    warn_ "${_agent:-$_d} not deregistered (pass --org and GH_TOKEN to do it automatically)"
    FAILED="$FAILED ${_agent:-$(basename "$_d")}"
  done
fi

# --------------------------------------------------------------------------
# 2/3. remove runners and the engine
# --------------------------------------------------------------------------

if [ "$PLATFORM" = linux ]; then
  head_ 'Removing containers and the isolated engine'
  for _r in $RUNNERS; do
    docker -H "unix://${SOCK}" rm -f "$_r" >/dev/null 2>&1 || true
    ok_ "Removed $_r"
  done
  docker -H "unix://${SOCK}" rm -f nomercy-runner-dashboard >/dev/null 2>&1 || true

  if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    ok_ "Removed the ${SERVICE_NAME} service"
  else
    info_ 'No systemd service to remove.'
  fi
else
  head_ 'Removing runner services'
  ok_ 'launchd services stopped and uninstalled'

  # The reboot-survival pieces, if --auto-login installed them. Both are removed
  # because both exist only to keep these runners up.
  _wd_label='tv.nomercy.runner-watchdog'
  _health_label='tv.nomercy.autologin-health'

  # The watchdog and the health daemon are machine-wide by design - the
  # watchdog covers every actions.runner agent it finds, including ones this
  # installer never created. So they only come off when there is nothing left
  # for them to watch. Removing them while another installation is still here
  # would silently take that one's reboot cover away.
  _remaining=0
  for _left in "$HOME"/Library/LaunchAgents/actions.runner.*.plist; do
    [ -e "$_left" ] && _remaining=$((_remaining + 1))
  done

  if [ "$_remaining" -gt 0 ]; then
    info_ "${_remaining} other runner agent(s) still here, so the watchdog stays."
  elif [ -f "$HOME/Library/LaunchAgents/${_wd_label}.plist" ]; then
    launchctl bootout "gui/$(id -u)/${_wd_label}" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/${_wd_label}.plist"
    ok_ 'Removed the runner watchdog'
  fi

  if [ "$_remaining" -gt 0 ]; then
    info_ 'The auto-login health daemon stays for the same reason.'
  elif [ -f "/Library/LaunchDaemons/${_health_label}.plist" ]; then
    # The installer took a password to create this, so the uninstaller takes one
    # to remove it. Leaving a root LaunchDaemon behind on someone's machine and
    # printing two commands is not a clean uninstall.
    _can_root=0
    if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then
      _can_root=1
      root_() { sudo "$@"; }
    else
      if [ "$SUDO_PASSWORD_FROM_STDIN" = 1 ]; then
        IFS= read -r SUDO_PASSWORD
      elif [ "$NON_INTERACTIVE" != 1 ]; then
        info_ 'The auto-login health daemon runs as root, so removing it needs your'
        info_ 'login password. Nothing else here does.'
        printf "\n  ${C_WHITE}Login password (input hidden)${C_RESET}\n  > "
        read -r -s SUDO_PASSWORD
        printf "\n"
      fi
      if [ -n "$SUDO_PASSWORD" ]; then
        root_() { printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"; }
        root_ true 2>/dev/null && _can_root=1
      fi
    fi

    if [ "$_can_root" = 1 ]; then
      root_ launchctl bootout "system/${_health_label}" >/dev/null 2>&1 || true
      root_ rm -f "/Library/LaunchDaemons/${_health_label}.plist" \
                  /usr/local/bin/nomercy-autologin-healthcheck.sh
      SUDO_PASSWORD=''
      ok_ 'Removed the auto-login health daemon'
    else
      SUDO_PASSWORD=''
      warn_ 'The auto-login health daemon needs root to remove. Left in place.'
      info_ "  sudo launchctl bootout system/${_health_label}"
      info_ "  sudo rm /Library/LaunchDaemons/${_health_label}.plist /usr/local/bin/nomercy-autologin-healthcheck.sh"
    fi
  fi

  # Auto-login is deliberately NOT undone. It is a machine-level setting the
  # operator opted into, they may want it for other reasons, and silently
  # re-enabling the login window on a headless Mac is its own outage.
  if [ -n "$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)" ]; then
    printf "\n"
    info_ 'Auto-login is still enabled. It was left alone deliberately - turning'
    info_ 'it off is a machine-level change you may not want. To undo it:'
    info_ '  sudo defaults delete /Library/Preferences/com.apple.loginwindow autoLoginUser'
    info_ '  sudo rm /etc/kcpassword'
  fi
fi

# --------------------------------------------------------------------------
# 4. data directory - only with explicit consent
# --------------------------------------------------------------------------

head_ 'Data directory'

if [ -d "$DATA_PATH" ]; then
  _size="$(du -sh "$DATA_PATH" 2>/dev/null | cut -f1)"
  printf "\n    ${C_YELLOW}%s   (%s)${C_RESET}\n" "$DATA_PATH" "${_size:-unknown}"

  # Default No. The operator chose this path, it may not be exclusively ours,
  # and deleting it cannot be undone.
  if [ "$DELETE_DATA" = 1 ]; then _do=1
  elif [ "$DELETE_DATA" = 0 ] || [ "$NON_INTERACTIVE" = 1 ]; then _do=0
  else _do="$(ask_yn_ 'Delete this directory and everything in it' n)"
  fi

  if [ "$_do" = 1 ]; then
    rm -rf "$DATA_PATH" && ok_ 'Data directory deleted' || warn_ 'Could not delete it.'
  else
    ok_ 'Kept. Delete it yourself whenever you are ready.'
    info_ "$DATA_PATH"
  fi
else
  info_ 'No data directory found.'
fi

# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

head_ 'Done'
printf "\n"
if [ -n "$(printf '%s' "$FAILED" | tr -d ' ')" ]; then
  warn_ 'These runners may still be listed in your organisation:'
  for _f in $FAILED; do printf "    ${C_RED}%s${C_RESET}\n" "$_f"; done
  printf "\n"
  info_ 'Remove them at:'
  if [ -n "$ORG" ]; then
    info_ "  https://github.com/organizations/${ORG}/settings/actions/runners"
  else
    info_ '  https://github.com/organizations/<your-org>/settings/actions/runners'
  fi
else
  ok_ 'All runners were deregistered from GitHub.'
fi
printf "\n"
info_ 'Your own Docker was never touched by these runners, and is unaffected.'
printf "\n"
