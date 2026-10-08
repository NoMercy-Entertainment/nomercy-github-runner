# The GitHub runner's job-started hook on Windows. The name ends in .ps1
# because the runner runs only .ps1, .sh and .js hooks; it runs this one with
# pwsh, or Windows PowerShell where there is no pwsh. The work is in
# runner_disk.py, run with the agent's own Python (RUNNER_HOOK_PYTHON).
# Only its deliberate refusal (a disk too full for the job) fails the job;
# anything else lets the job run.
$ErrorActionPreference = 'Continue'
$status = 0
try {
    $python = $env:RUNNER_HOOK_PYTHON
    if (-not $python -or -not (Test-Path -LiteralPath $python)) {
        throw "the agent's Python is not at '$python'"
    }
    & $python -I -B (Join-Path $PSScriptRoot 'runner_disk.py') started 2>&1 |
        ForEach-Object { "$_" }
    $status = $LASTEXITCODE
} catch {
    Write-Output "::warning title=Runner hook::job-started check failed: $($_.Exception.Message)"
    exit 0
}
if ($status -eq 75) { exit 1 }
if ($status -ne 0) { Write-Output "::warning title=Runner hook::job-started check ended with status $status" }
exit 0
