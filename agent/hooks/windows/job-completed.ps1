# The GitHub runner's job-completed hook on Windows. The name ends in .ps1
# because the runner runs only .ps1, .sh and .js hooks; any other path fails
# the job's last step, and the job with it. The work is in runner_disk.py,
# run with the agent's own Python (RUNNER_HOOK_PYTHON). It never fails a job.
$ErrorActionPreference = 'Continue'
$status = 0
try {
    $python = $env:RUNNER_HOOK_PYTHON
    if (-not $python -or -not (Test-Path -LiteralPath $python)) {
        throw "the agent's Python is not at '$python'"
    }
    & $python -I -B (Join-Path $PSScriptRoot 'runner_disk.py') completed 2>&1 |
        ForEach-Object { "$_" }
    $status = $LASTEXITCODE
} catch {
    Write-Output "::warning title=Runner hook::job-completed cleanup failed: $($_.Exception.Message)"
    exit 0
}
if ($status -ne 0) { Write-Output "::warning title=Runner hook::job-completed cleanup ended with status $status" }
exit 0
