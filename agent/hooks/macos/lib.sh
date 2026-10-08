# What the macOS runner's job hooks share (GitHub #7). Sourced by
# job-started.sh and job-completed.sh, which the agent puts beside it in the
# runner's reg directory. Plain bash 3.2, as macOS ships it.
#
# The thresholds and the wording are the Linux unit's
# (images/linux/unit/runner/job_started.py). Nothing here follows a link:
# rm -rf and find without -L remove a link as a link.
#
# The environment is the launchd job's: RUNNER_WORK_DIR, GRADLE_USER_HOME and
# HOME (the runner's own, under its work directory), RUNNER_HOOK_USER_HOME
# (the account's real home, where Xcode keeps DerivedData), and from the
# runner GITHUB_WORKSPACE.

REFUSE=75

# Bytes free on the volume holding $1 - on APFS the Data volume, which / is
# not - or nothing when df cannot say.
free_bytes() {
  df -Pk "$1" 2>/dev/null | awk 'NR == 2 && $4 ~ /^[0-9]+$/ { printf "%.0f", $4 * 1024 }'
}

# A threshold in bytes: $1 in GB (1000^3, as the Linux hook), or $2 if unset.
threshold() {
  awk -v v="$1" -v d="$2" 'BEGIN { if (v == "") v = d; printf "%.0f", v * 1000000000 }'
}

less_than() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 < b + 0) }'
}

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

gb() {
  awk -v b="$1" -v f="${2:-%.0f}" 'BEGIN { printf f, b / 1000000000 }'
}

# Every earlier job's workspace, never the current one; nothing while it
# cannot tell which one is current. _tool, _actions, _temp and the runner's
# own dot entries (its HOME is .home) stay.
remove_earlier_workspaces() {
  local work current keep keep_lower path name
  work=${RUNNER_WORK_DIR%/}
  current=${GITHUB_WORKSPACE:-}
  if [ -z "$work" ] || [ -L "$work" ] || [ ! -d "$work" ]; then
    return 0
  fi
  case "$current" in
    "$work"/?*) keep=${current#"$work"/}; keep=${keep%%/*} ;;
    *) echo "The job's workspace is not under the runner's work directory; earlier workspaces were left alone"
       return 0 ;;
  esac
  # APFS is case-insensitive: GITHUB_WORKSPACE may spell the current
  # directory with other capitals, so it is matched without case, and as the
  # same file.
  keep_lower=$(lower "$keep")
  for path in "$work"/*; do
    name=${path##*/}
    case "$name" in _*|.*) continue ;; esac
    if [ "$(lower "$name")" = "$keep_lower" ] || [ "$path" -ef "$work/$keep" ] \
        || [ -L "$path" ] || [ ! -d "$path" ]; then
      continue
    fi
    rm -rf -- "$path" || echo "$name could not be removed"
  done
  return 0
}

# What the runner left in _temp, except the hook step's own file commands,
# which the runner reads once the step has finished.
clear_temp() {
  local temp
  temp=${RUNNER_WORK_DIR%/}/_temp
  if [ -z "${RUNNER_WORK_DIR:-}" ] || [ -L "$temp" ] || [ ! -d "$temp" ]; then
    return 0
  fi
  find "$temp" -mindepth 1 -maxdepth 1 ! -name _runner_file_commands -exec rm -rf {} + 2>/dev/null
  return 0
}

# Whether nothing at all under $1, itself included, changed in the last two
# days. find must answer cleanly: a tree it could not read all of is in use
# as far as this is concerned.
untouched_for_two_days() {
  local recent
  recent=$(find "$1" -mmin -2880 -print -quit 2>/dev/null) || return 1
  [ -z "$recent" ]
}

# Xcode's per-project build products in which nothing has changed for two
# days. Every instance in a guest shares the account's DerivedData - the
# appliance runs a GitHub and a Forgejo runner as the same user - and an
# incremental build writes deep inside an entry without touching the entry's
# own directory, so every file in it is looked at, not the entry.
clear_stale_derived_data() {
  local home dd entry seen=""
  for home in "${RUNNER_HOOK_USER_HOME:-}" "${HOME:-}"; do
    [ -n "$home" ] || continue
    dd=${home%/}/Library/Developer/Xcode/DerivedData
    case " $seen " in *" $dd "*) continue ;; esac
    seen="$seen $dd"
    if [ ! -d "$dd" ] || [ -L "$dd" ]; then
      continue
    fi
    for entry in "$dd"/*; do
      if [ -L "$entry" ] || [ ! -d "$entry" ]; then
        continue
      fi
      untouched_for_two_days "$entry" && rm -rf -- "$entry"
    done
  done
  return 0
}

# Gradle's build cache, in this runner's own Gradle home (GRADLE_USER_HOME is
# under the runner's cache directory, shared with no other runner, and its
# job has not started yet): rebuildable, and Gradle keeps it for a week on
# its own.
clear_gradle_build_cache() {
  local caches
  caches=${GRADLE_USER_HOME%/}/caches
  if [ -z "${GRADLE_USER_HOME:-}" ] || [ -L "$caches" ] || [ ! -d "$caches" ]; then
    return 0
  fi
  find "$caches" -mindepth 1 -maxdepth 1 -name 'build-cache-*' -exec rm -rf {} + 2>/dev/null
  return 0
}

# 0 to let the job run, REFUSE to fail it on purpose.
job_started() {
  local work free after clean_below warn_below fail_below
  work=${RUNNER_WORK_DIR:-}
  [ -n "$work" ] || return 0
  clean_below=$(threshold "${RUNNER_DISK_CLEAN_BELOW_GB:-}" 15)
  warn_below=$(threshold "${RUNNER_DISK_WARN_BELOW_GB:-}" 10)
  fail_below=$(threshold "${RUNNER_DISK_FAIL_BELOW_GB:-}" 3)
  free=$(free_bytes "$work")
  [ -n "$free" ] || return 0
  echo "Disk free before the job: $(gb "$free") GB (lowest of $work)"
  if less_than "$free" "$clean_below"; then
    echo "Under $(gb "$clean_below") GB: clearing earlier workspaces, Xcode DerivedData unused for two days and the Gradle build cache"
    remove_earlier_workspaces
    clear_stale_derived_data
    clear_gradle_build_cache
    after=$(free_bytes "$work")
    [ -n "$after" ] && free=$after
    echo "Disk free after cleanup: $(gb "$free") GB"
  fi
  if less_than "$free" "$fail_below"; then
    echo "::error title=Runner disk full::only $(gb "$free" %.1f) GB free on this runner (needs $(gb "$fail_below") GB). The job was stopped before it could crash the runner; re-run it, it will land elsewhere or after a cleanup."
    return "$REFUSE"
  fi
  if less_than "$free" "$warn_below"; then
    echo "::warning title=Runner disk low::only $(gb "$free") GB free on this runner."
  fi
  return 0
}

job_completed() {
  clear_temp
  clear_stale_derived_data
  return 0
}
