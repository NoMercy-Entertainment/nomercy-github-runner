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

# Where this file is: reg/hooks, beside the runner's own software.
HOOK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)

# ---- whose code: before anything else, the job's origin ---------------------
#
# The rule and the wording are runner_guard.py's (images/linux/unit/runner),
# and agent/tests/origin_cases.py holds the two to the same answers: a pull
# request whose head is a fork, or a fork deleted since, is refused unless its
# author is OWNER or MEMBER, or a login in RUNNER_TRUSTED_AUTHORS
# (comma-separated, without case). GitHub reports a member whose membership is
# private as CONTRIBUTOR, which is what the list is for. Everything else runs,
# with one line that says where it came from. An event that cannot be read, or
# no node to read it with, is a warning, and the job runs.
#
# bash cannot read JSON, and a pattern over the payload can be fooled by a
# pull request's own title or body. The runner's own node reads it - every
# GitHub runner ships one in externals/ to run JavaScript actions - and says
# only what the rule needs, each value already in the characters GitHub allows
# in a login or a repository name.

# What the agent reports for this runner; the same number as runner_guard.py's
# GUARD_VERSION.
ORIGIN_GUARD_VERSION=1
ORIGIN_UNREAD="::warning title=Runner guard::could not read the event; origin not checked"

# kind|base|head|login|association|number|repository|sender, from the event
# file in argv[1]. kind: none, same, fork, deleted or unknown. Exit 3 when the
# file is not a JSON object.
ORIGIN_FACTS_JS='
const fs = require("fs");
let p;
try { p = JSON.parse(fs.readFileSync(process.argv[1], "utf8")); } catch (e) { process.exit(3); }
const obj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
if (!obj(p)) process.exit(3);
const get = (v, ...keys) => { for (const k of keys) { if (!obj(v)) return undefined; v = v[k]; } return v; };
const text = (v) => (typeof v === "string" && v !== "") ? v : "";
const shown = (v) => text(v).replace(/[^A-Za-z0-9._\/-]/gu, "?").slice(0, 100);
let kind = "none", base = "", head = "", login = "", association = "", number = "";
const pr = p.pull_request;
if (obj(pr)) {
  base = text(get(pr, "base", "repo", "full_name")) || text(get(p, "repository", "full_name"));
  login = text(get(pr, "user", "login"));
  association = typeof pr.author_association === "string" ? pr.author_association.toUpperCase() : "";
  number = Number.isInteger(pr.number) && pr.number > 0 ? String(pr.number) : "";
  const h = pr.head;
  if (!obj(h) || !Object.prototype.hasOwnProperty.call(h, "repo")) kind = "unknown";
  else if (h.repo === null) kind = "deleted";
  else {
    const name = text(get(h, "repo", "full_name"));
    if (!name || !base) kind = "unknown";
    else { head = name; kind = name.toLowerCase() === base.toLowerCase() ? "same" : "fork"; }
  }
}
process.stdout.write([kind, shown(base), shown(head), shown(login), shown(association),
  number, shown(get(p, "repository", "full_name")), shown(get(p, "sender", "login"))].join("|") + "\n");
'

# The runner's own node, newest first: RUNNER_HOOK_NODE when it is set, then
# externals/node*/bin/node beside these hooks or in RUNNER_REG_DIR, then any
# node on PATH. Nothing when there is none.
hook_node() {
  local found="" candidate
  if [ -n "${RUNNER_HOOK_NODE:-}" ]; then
    printf '%s' "$RUNNER_HOOK_NODE"
    return 0
  fi
  for candidate in "${RUNNER_REG_DIR:-/nonexistent}"/externals/node*/bin/node \
      "$HOOK_DIR"/../externals/node*/bin/node; do
    [ -x "$candidate" ] && found=$candidate
  done
  [ -n "$found" ] || found=$(command -v node 2>/dev/null)
  printf '%s' "$found"
}

# $1 as it may appear in the log - only what GitHub allows in a login or a
# repository name - or $2 when it is empty.
shown() {
  local value
  value=$(printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9._/-' '?' | cut -c1-100)
  printf '%s' "${value:-$2}"
}

# Whether login $1 is in RUNNER_TRUSTED_AUTHORS, without case.
trusted_author() {
  local login entry found=1
  login=$(lower "$1")
  [ -n "$login" ] || return 1
  set -f
  local IFS=','
  for entry in ${RUNNER_TRUSTED_AUTHORS:-}; do
    entry=$(lower "$(printf '%s' "$entry" | tr -d '[:space:]')")
    [ -n "$entry" ] && [ "$entry" = "$login" ] && found=0
  done
  set +f
  return $found
}

# 0 to let the job run, REFUSE to stop it.
origin_check() {
  local node facts kind base head login association number repository sender
  local name who where org pr
  name=$(shown "${GITHUB_EVENT_NAME:-}" "an unnamed event")
  if [ -z "${GITHUB_EVENT_PATH:-}" ]; then
    echo "$ORIGIN_UNREAD"
    return 0
  fi
  node=$(hook_node)
  if [ -z "$node" ]; then
    echo "::warning title=Runner guard::no node to read the event with; origin not checked"
    return 0
  fi
  if ! facts=$("$node" -e "$ORIGIN_FACTS_JS" "$GITHUB_EVENT_PATH" 2>/dev/null) || [ -z "$facts" ]; then
    echo "$ORIGIN_UNREAD"
    return 0
  fi
  IFS='|' read -r kind base head login association number repository sender <<EOF
$facts
EOF
  case "$kind" in
    none)
      echo "Origin: $name from $(shown "${repository:-${GITHUB_REPOSITORY:-}}" "an unknown repository") by $(shown "${sender:-${GITHUB_ACTOR:-}}" "an unknown account") — allowed"
      return 0 ;;
    same) where=$(shown "$base" "an unknown repository") ;;
    fork) where="fork $(shown "$head" "an unknown repository")" ;;
    deleted) where="a deleted fork" ;;
    *) echo "$ORIGIN_UNREAD"
       return 0 ;;
  esac
  who=$(shown "$login" "an unknown account")
  if [ "$kind" = same ]; then
    echo "Origin: $name from $where by $who — allowed"
    return 0
  fi
  case " OWNER MEMBER " in
    *" ${association:-none} "*)
      echo "Origin: $name from $where by $who — allowed"
      return 0 ;;
  esac
  if trusted_author "$login"; then
    echo "Origin: $name from $where by $who — allowed"
    return 0
  fi
  org=$(shown "${base%%/*}" "the organisation")
  if [ -n "$number" ]; then pr="Pull request #$number"; else pr="The pull request"; fi
  echo "::error title=Outside code refused::Self-hosted runners only run code from $org members and known maintainers. $pr by $who ($(shown "$association" UNKNOWN)) comes from $where, so this job was stopped before any of its code ran. If $who is a maintainer whose org membership is private, add them to RUNNER_TRUSTED_AUTHORS."
  return "$REFUSE"
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
  [ $? -eq "$REFUSE" ] && return "$REFUSE"
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
