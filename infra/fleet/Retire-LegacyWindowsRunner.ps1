<#
.SYNOPSIS
    Take the Windows Forgejo runner that runs outside the platform out of
    service, so the controller's managed one can take its place. Elevated.

.DESCRIPTION
    The runner on this host today is the service `forgejo-runner`, started by
    NSSM from C:\forgejo-runner, running a binary that reports `dev` - no tag,
    no commit (images/windows/manifest.json). Its replacement is a unit the
    controller makes: its own virtual account, its own tree, a Job Object with
    a memory ceiling, and the traceably built v13.1.0 binary.

    This script only stops that service and sets it to Manual, after checking
    at Forgejo that it has no job. Nothing is deleted: C:\forgejo-runner, its
    registration and its data stay exactly as they are, so putting it back is
    `sc.exe config forgejo-runner start= auto` and `sc.exe start
    forgejo-runner`.

    Its registration at Forgejo is left alone here as well. The controller's
    runner registers as a new runner; the old record is deleted separately,
    once its replacement is serving.

.EXAMPLE
    .\Retire-LegacyWindowsRunner.ps1
    .\Retire-LegacyWindowsRunner.ps1 -Restore
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Service = 'forgejo-runner',
    [string] $RunnerName = 'beaststack-windows-runner',
    [switch] $Restore
)
$ErrorActionPreference = 'Stop'
$repo = 'D:\docker-compose\GithubRunners'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: it changes a service.'
}
$existing = Get-Service -Name $Service -ErrorAction SilentlyContinue
if (-not $existing) { throw "There is no service named $Service on this host." }

if ($Restore) {
    if ($PSCmdlet.ShouldProcess($Service, 'set to Automatic and start')) {
        & sc.exe config $Service start= auto | Out-Null
        Start-Service -Name $Service
        Get-Service -Name $Service | Format-Table Name, Status, StartType -AutoSize
    }
    return
}

# --- does it have a job? ------------------------------------------------------
$env:NO_COLOR = '1'
$check = Join-Path $PSScriptRoot 'is-forgejo-runner-idle.py'
$state = & python $check $RunnerName
if ($LASTEXITCODE -ne 0) {
    throw "Could not ask Forgejo about $RunnerName ($state). Not touching the service."
}
Write-Host "Forgejo says $RunnerName is $state"
if ($state -ne 'idle' -and $state -ne 'offline') {
    throw "$RunnerName is $state; a running job must finish first."
}

if ($PSCmdlet.ShouldProcess($Service, 'stop and set to Manual')) {
    # Stopped, not removed: the service gets its own stop grace, and
    # forgejo-runner takes nothing new once it is told to stop.
    Stop-Service -Name $Service -ErrorAction Stop
    & sc.exe config $Service start= demand | Out-Null
    Get-Service -Name $Service | Format-Table Name, Status, StartType -AutoSize
    Write-Host "Put it back with: .\Retire-LegacyWindowsRunner.ps1 -Restore"
}
