<#
.SYNOPSIS
    Phase 5 in one elevated run: the VMs, their software, and this host as
    the Windows worker. Everything it does is logged to
    <Root>\deploy-<time>.log.

.DESCRIPTION
    Runs, in order, stopping at the first failure:
      1. New-RunnerPlatformVMs.ps1    the switch and the two VMs
      2. Initialize-RunnerPlatform.ps1 the controller and the Linux worker
      3. Install-WindowsWorker.ps1    the agent on this host
    Each is idempotent, so after a failure this can simply be run again.
    Prepare-RunnerPlatform.ps1 must have run first; it needs no elevation.

    Nothing is scaled: at the end every fleet is at capacity 0 and no runner
    exists. The first one is an operator's `capacity` command (README.md).
#>
[CmdletBinding()]
param([switch] $SkipWindowsWorker)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings
if (-not (Test-Elevated)) { throw 'Run this elevated.' }

$log = Join-Path $s.Root ("deploy-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $log | Out-Null
try {
    Write-Host "== 1/3 VMs"; & "$PSScriptRoot\New-RunnerPlatformVMs.ps1"
    Write-Host "== 2/3 controller and Linux worker"; & "$PSScriptRoot\Initialize-RunnerPlatform.ps1"
    if (-not $SkipWindowsWorker) {
        Write-Host "== 3/3 Windows worker"; & "$PSScriptRoot\Install-WindowsWorker.ps1"
    }
    Write-Host "== done"
}
catch {
    Write-Host "== FAILED: $($_.Exception.Message)"
    throw
}
finally {
    Stop-Transcript | Out-Null
    Write-Host "log: $log"
}
