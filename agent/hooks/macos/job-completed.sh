#!/bin/bash
# The GitHub runner's job-completed hook on macOS. The name ends in .sh
# because the runner runs only .sh, .ps1 and .js hooks; any other path fails
# the job's last step, and the job with it. The work is in lib.sh, beside it.
# It never fails a job.
set +e +u
set +o pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)
if ! . "$here/lib.sh" 2>/dev/null; then
  echo "::warning title=Runner hook::job-completed cleanup could not load $here/lib.sh"
  exit 0
fi
( job_completed )
status=$?
[ "$status" -ne 0 ] && echo "::warning title=Runner hook::job-completed cleanup ended with status ${status}"
exit 0
