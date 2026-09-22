#!/usr/bin/env bash
# The GitHub runner's job-completed hook. It runs a hook only when the path
# ends in .sh, .ps1 or .js; any other path fails the job's last step, and the
# job with it. So this is the name the hook is given, and the work stays in
# /runner/cleanup, which never fails.
exec /runner/cleanup
