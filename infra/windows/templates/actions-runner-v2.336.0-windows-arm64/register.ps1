# Registers this runner with GitHub. Run by the agent (WindowsRegistrar) as
# `powershell -File register.ps1`, the plan as JSON on standard input:
#   {"url", "token", "name", "labels", "runner_group"}
# The last line of standard output is {"registration_id", "registration_uuid"}.
#
# **The token is never on a command line** where the worker's `ps` would show
# it. GitHub's runner reads any of its arguments from an
# ACTIONS_RUNNER_INPUT_<NAME> variable, and the token goes that way - the
# same path the Linux unit's `register` takes.
#
# Idempotent: a runner registered already answers with its registration and
# configures nothing.
$ErrorActionPreference = 'Stop'
$agent = Join-Path $PSScriptRoot 'agent'
Set-Location -LiteralPath $agent

function Answer {
    $r = Get-Content -Raw -LiteralPath (Join-Path $agent '.runner') |
        ConvertFrom-Json
    # GitHub has no uuid for a runner; its id is the registration.
    [ordered]@{ registration_id = "$($r.agentId)"; registration_uuid = $null } |
        ConvertTo-Json -Compress
}

if (Test-Path -LiteralPath (Join-Path $agent '.runner')) { Answer; exit 0 }

$plan = [Console]::In.ReadToEnd() | ConvertFrom-Json
if (-not $plan.url) { [Console]::Error.WriteLine('the plan names no forge'); exit 65 }

$env:ACTIONS_RUNNER_INPUT_TOKEN = $plan.token
$env:ACTIONS_RUNNER_INPUT_URL = $plan.url
$env:ACTIONS_RUNNER_INPUT_WORK = $env:RUNNER_WORK_DIR
# Only what the plan names: an empty group or label list set as an empty
# input is not the same as none.
if ($plan.name)         { $env:ACTIONS_RUNNER_INPUT_NAME = $plan.name }
if ($plan.labels)       { $env:ACTIONS_RUNNER_INPUT_LABELS = $plan.labels }
if ($plan.runner_group) { $env:ACTIONS_RUNNER_INPUT_RUNNERGROUP = $plan.runner_group }

try {
    # Bounded and judged by its exit code, like the Forgejo template's: the
    # agent gives up on this script after 120 s, and a config.cmd still
    # talking to an unreachable GitHub would be left behind outside any Job
    # Object.
    $run = Start-Process -FilePath (Join-Path $agent 'config.cmd') `
        -ArgumentList '--unattended', '--replace', '--disableupdate' `
        -NoNewWindow -PassThru -RedirectStandardOutput `
        (Join-Path $env:TEMP 'rnr-config.out') -RedirectStandardError `
        (Join-Path $env:TEMP 'rnr-config.err')
    # Without the handle Windows PowerShell loses the exit code, and a failed
    # config.cmd read as $null - which `exit` turns into 0 (2026-09-22).
    $null = $run.Handle
    if (-not $run.WaitForExit(90000)) {
        $run.Kill()
        [Console]::Error.WriteLine('config.cmd did not finish within 90s')
        exit 124
    }
    foreach ($f in 'rnr-config.out', 'rnr-config.err') {
        $path = Join-Path $env:TEMP $f
        if (Test-Path -LiteralPath $path) {
            Get-Content -LiteralPath $path | ForEach-Object {
                [Console]::Error.WriteLine($_)
            }
            Remove-Item -LiteralPath $path -Force
        }
    }
    $code = $run.ExitCode
    if ($null -eq $code) { $code = 1 }
    if ($code -ne 0) {
        [Console]::Error.WriteLine("config.cmd exited $code")
        exit $code
    }
} finally {
    Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath (Join-Path $agent '.runner'))) {
    [Console]::Error.WriteLine(
        'GitHub reported success but left no registration')
    exit 70
}
Answer
