#!/bin/bash
# The GitHub runner's job-started hook on macOS. The name ends in .sh because
# the runner runs only .sh, .ps1 and .js hooks; it runs this one with
# `bash -e -o pipefail`, which is turned off again here. The work is in
# lib.sh, beside it. Only its deliberate refusal (a disk too full for the job)
# fails the job; anything else lets the job run.
set +e +u
set +o pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)
if ! . "$here/lib.sh" 2>/dev/null; then
  echo "::warning title=Runner hook::job-started check could not load $here/lib.sh"
  exit 0
fi
( job_started )
status=$?
[ "$status" -eq "$REFUSE" ] && exit 1
[ "$status" -ne 0 ] && echo "::warning title=Runner hook::job-started check ended with status ${status}"
exit 0
