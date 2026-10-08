#!/usr/bin/env bash
# The GitHub runner's job-started hook. The name ends in .sh because the
# runner runs only .sh, .ps1 and .js hooks. The work is in job_started.py,
# beside it (/runner in the unit). Only its deliberate refusals - a pull
# request from an outside fork, a disk too full for the job - fail the job;
# anything else, a timeout included, lets the job run.
# The runner starts a .sh hook as `bash -e -o pipefail`: switched off here,
# and the call guarded, so a crash or a timeout reaches the lines below
# instead of ending the script with the job's failure.
set +e
set -u
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd) || here=/runner
status=0
timeout --kill-after=5s 600s python3 "$here/job_started.py" || status=$?
[ "$status" -eq 75 ] && exit 1
[ "$status" -ne 0 ] && echo "::warning title=Runner hook::job-started check ended with status ${status}"
exit 0
