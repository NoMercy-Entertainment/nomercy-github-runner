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

# Where this file is: the runner's hooks directory.
HOOK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)

# ---- whose code: before anything else, the job's origin ---------------------
#
# runner_guard.js, beside this file, decides: the rule and the wording are
# runner_guard.py's (images/linux/unit/runner), and agent/tests/origin_cases.py
# holds the two to the same answers. bash cannot read JSON, and a pattern over
# the payload can be fooled by a pull request's own title or body, so the
# runner's own node runs it - every GitHub runner ships one in externals/ to
# run JavaScript actions. No node, or a node that fails, is a warning, and the
# job runs.

# The node to run runner_guard.js with: RUNNER_HOOK_NODE when it is set, else
# the newest externals/node*/bin/node of the runner in RUNNER_REG_DIR, else
# the newest beside these hooks (../externals), else any node on PATH.
# Nothing when there is none.
hook_node() {
  local found="" candidate
  if [ -n "${RUNNER_HOOK_NODE:-}" ]; then
    printf '%s' "$RUNNER_HOOK_NODE"
    return 0
  fi
  for candidate in "${RUNNER_REG_DIR:-/nonexistent}"/externals/node*/bin/node; do
    [ -x "$candidate" ] && found=$candidate
  done
  if [ -z "$found" ]; then
    for candidate in "$HOOK_DIR"/../externals/node*/bin/node; do
      [ -x "$candidate" ] && found=$candidate
    done
  fi
  [ -n "$found" ] || found=$(command -v node 2>/dev/null)
  printf '%s' "$found"
}

# The file name of process $1, from its executable (comm, a path on macOS) or
# else its first argument.
process_name() {
  local comm
  comm=$(ps -o comm= -p "$1" 2>/dev/null)
  [ -n "$comm" ] || comm=$(ps -o args= -p "$1" 2>/dev/null | awk '{ print $1 }')
  printf '%s' "${comm##*/}"
}

# The Runner.Worker that started this hook, or nothing. The runner runs
# job-started.sh as a shell of its own ($$ in every subshell here), whose
# parent is Runner.Worker - or, when the runner goes through its
# macos-run-invoker.js, a node whose parent is. Anything else is not a hook
# the runner started (a test of the hook inside a job, say), and nothing is
# named. Never Runner.Listener.
worker_of_this_hook() {
  local parent
  parent=$(ps -o ppid= -p "$$" 2>/dev/null | tr -d ' ')
  case "$parent" in ''|*[!0-9]*|0|1) return 0 ;; esac
  case "$(ps -o args= -p "$parent" 2>/dev/null)" in
    *macos-run-invoker.js*)
      parent=$(ps -o ppid= -p "$parent" 2>/dev/null | tr -d ' ')
      case "$parent" in ''|*[!0-9]*|0|1) return 0 ;; esac ;;
  esac
  [ "$(process_name "$parent")" = "Runner.Worker" ] && printf '%s' "$parent"
  return 0
}

# A failed job-started hook is only a failed step: the runner still runs
# every later step marked always(), failure() or !cancelled(), and a fork
# writes its own workflow file. So a refusal kills the Runner.Worker running
# the job, once the ::error has had RUNNER_GUARD_KILL_DELAY seconds (5) to
# reach GitHub - the runner waits for this hook meanwhile.
end_the_job() {
  local worker delay
  worker=$(worker_of_this_hook)
  if [ -z "$worker" ]; then
    echo "::warning title=Runner guard::the job could not be ended (this hook was not started by a Runner.Worker); steps the workflow marks always() may still run"
    return 1
  fi
  delay=${RUNNER_GUARD_KILL_DELAY:-5}
  case "$delay" in ''|*[!0-9]*) delay=5 ;; esac
  [ "$delay" -gt 30 ] && delay=30
  sleep "$delay"
  if ! kill -9 "$worker" 2>/dev/null; then
    echo "::warning title=Runner guard::the job could not be ended (Runner.Worker $worker could not be killed); steps the workflow marks always() may still run"
    return 1
  fi
  return 0
}

# 0 to let the job run, REFUSE to stop it.
origin_check() {
  local node status
  node=$(hook_node)
  if [ -z "$node" ]; then
    echo "::warning title=Runner guard::no node to read the event with; origin not checked"
    return 0
  fi
  "$node" "$HOOK_DIR/runner_guard.js"
  status=$?
  [ "$status" -eq "$REFUSE" ] && return "$REFUSE"
  [ "$status" -ne 0 ] && echo "::warning title=Runner guard::the origin check ended with status ${status}; origin not checked"
  return 0
}

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

# 0 to let the job run, REFUSE to fail it on purpose. Whose code it is comes
# first: a refused job is not measured, let alone cleaned for.
job_started() {
  local work free after clean_below warn_below fail_below
  origin_check
  if [ $? -eq "$REFUSE" ]; then
    end_the_job
    return "$REFUSE"
  fi
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
