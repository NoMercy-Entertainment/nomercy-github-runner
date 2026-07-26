# Register the keepalive as a logon-triggered scheduled task.
#
# Logon trigger deliberately, not startup: a startup task that runs with nobody
# logged on requires Windows to store this account's password. That was declined
# in favour of no stored credentials.
#
# The trade-off, stated plainly so it is not a surprise later: after a reboot
# the runners stay down until someone logs into Windows. If this machine ever
# needs to come back unattended, either switch the task to "run whether user is
# logged on or not" (which will prompt for the password), or enable Windows
# auto-login.
#
#   .\scripts\install-keepalive-task.ps1

$ErrorActionPreference = 'Stop'

$TaskName   = 'GitHub Runners - Keep WSL Distro Alive'
$ScriptPath = Join-Path $PSScriptRoot 'keepalive-distro.ps1'

if (-not (Test-Path $ScriptPath)) { throw "Missing $ScriptPath" }

# Absolute path: scheduled tasks do not inherit PATH, so a bare
# 'powershell.exe' fails with 0x80070002 ("cannot find the file specified").
$PwshPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path $PwshPath)) { throw "Missing $PwshPath" }

$action = New-ScheduledTaskAction `
    -Execute $PwshPath `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Run as the invoking user. WSL distros are registered per-user, so a task
# running as SYSTEM would not be able to see 'github-runners' at all.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# The task runs forever by design. Without this, Windows kills it after the
# default 3-day execution limit and the runners quietly die.
$settings.ExecutionTimeLimit = 'PT0S'

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  trigger:  at logon of $env:USERNAME" -ForegroundColor Gray
Write-Host "  script:   $ScriptPath" -ForegroundColor Gray
Write-Host "  log:      $env:LOCALAPPDATA\github-runners-keepalive.log" -ForegroundColor Gray
Write-Host ""
Write-Host "Starting it now so you do not have to log out and back in." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
(Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo) |
    Select-Object TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns | Format-List
