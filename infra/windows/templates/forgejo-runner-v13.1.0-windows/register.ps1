# Registers this runner with Forgejo. Run by the agent (WindowsRegistrar) as
# `powershell -File register.ps1`, the plan as JSON on standard input:
#   {"url", "token", "name", "labels", "runner_group"}
# The last line of standard output is {"registration_id", "registration_uuid"}.
#
# Idempotent: a runner registered already answers with its registration.
# forgejo-runner takes its token only as an argument, for the second the
# command runs - as on Linux; a Forgejo registration token is the
# controller's to rotate.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Answer {
    $r = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '.runner') | ConvertFrom-Json
    [ordered]@{ registration_id = "$($r.id)"; registration_uuid = $r.uuid } |
        ConvertTo-Json -Compress
}

if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.runner')) { Answer; exit 0 }

$plan = [Console]::In.ReadToEnd() | ConvertFrom-Json
if (-not $plan.url) { [Console]::Error.WriteLine('the plan names no forge'); exit 65 }

$runner = Join-Path $PSScriptRoot 'forgejo-runner.exe'
# Bounded, and judged by its exit code alone:
# - forgejo-runner's `register` pings an unreachable instance for ever, and the
#   agent gives up on this script after 120 s - leaving the runner behind,
#   outside any Job Object. So it gets 90 s and is then stopped.
# - forgejo-runner v13 always warns on stderr that `register` is deprecated;
#   stderr is read, never taken as failure.
$logOut = Join-Path $PSScriptRoot 'register.out'
$logErr = Join-Path $PSScriptRoot 'register.err'
$p = Start-Process -FilePath $runner -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr -ArgumentList @(
        'register', '--no-interactive', '--instance', $plan.url, '--token', $plan.token,
        '--name', $plan.name, '--labels', $plan.labels)
$null = $p.Handle       # without it Windows PowerShell may lose the exit code
if (-not $p.WaitForExit(90000)) {
    $p.Kill(); $p.WaitForExit()
    $code = 124
} else {
    $code = $p.ExitCode
}
$out = (Get-Content -Raw -LiteralPath $logOut, $logErr -ErrorAction SilentlyContinue) -join "`n"
Remove-Item -LiteralPath $logOut, $logErr -ErrorAction SilentlyContinue
if ($code -ne 0) {
    $why = if ($code -eq 124) { 'the forge did not answer within 90 s' } else { "exit $code" }
    [Console]::Error.WriteLine("register failed ($why): " + ($out -replace [regex]::Escape([string]$plan.token), '***'))
    exit $code
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.runner'))) {
    [Console]::Error.WriteLine('forgejo-runner reported success but left no registration')
    exit 70
}
Answer
