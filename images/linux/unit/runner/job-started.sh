#!/usr/bin/env bash
# The GitHub runner's job-started hook. The name ends in .sh because the
# runner runs only .sh, .ps1 and .js hooks. The work is in job_started.py.
# Only its deliberate refusal (a disk too full for the job) fails the job;
# anything else, a timeout included, lets the job run.
set -uo pipefail
timeout --kill-after=5s 600s python3 /runner/job_started.py
status=$?
[ "$status" -eq 75 ] && exit 1
[ "$status" -ne 0 ] && echo "::warning title=Runner hook::job-started check ended with status ${status}"
exit 0
