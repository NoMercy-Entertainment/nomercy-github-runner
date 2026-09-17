<#
.SYNOPSIS
  Installs the runner exporter as a Windows service on BEAST-UNIT.

.DESCRIPTION
  Registers exporters/windows/serve.py through the same NSSM binary that
  already runs the forgejo-runner service, and opens the one port the
  dashboard needs on the WSL interface only.

  The dashboard lives in a container in WSL. It can reach this host on the
  WSL gateway, but it cannot reach the macOS VM at all - the route exists and
  no traffic crosses it - which is why this one exporter reads both machines
  rather than one sitting on each. See
  docs/superpowers/specs/2026-09-17-external-runner-telemetry-design.md.

  Run elevated. Re-running is safe: the service is removed and re-added, and
  the firewall rule is replaced rather than duplicated.
#>
[CmdletBinding()]
param(
  [string]$ServiceName = 'forgejo-runner-exporter',
  [string]$Nssm        = 'C:\forgejo-runner\nssm.exe',
  [string]$Bind        = '0.0.0.0',
  [int]   $Port        = 9101,
  [string]$MacosHost   = '172.19.136.46',
  [string]$MacosUser   = 'runner',
  [string]$MacosKey    = "$env:USERPROFILE\.ssh\macos_runner",
  [string]$WindowsLog  = 'C:\forgejo-runner\runner.err.log',
  # The WSL subnet, so the port is not offered to the LAN. WSL's address is
  # assigned per boot, so the whole /20 is allowed rather than one address.
  [string]$AllowFrom   = '172.28.192.0/20'
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal] `
      [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run this elevated: it registers a service and adds a firewall rule.'
}
if (-not (Test-Path $Nssm))     { throw "NSSM not found at $Nssm" }
if (-not (Test-Path $MacosKey)) { throw "SSH key not found at $MacosKey" }

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python.exe not on PATH' }

$serve = Join-Path $PSScriptRoot 'serve.py'
if (-not (Test-Path $serve)) { throw "serve.py not found next to this script" }

# Remove first, so re-running updates the configuration instead of failing on
# an existing service with stale arguments.
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Write-Host "Removing existing $ServiceName"
  & $Nssm stop   $ServiceName | Out-Null
  & $Nssm remove $ServiceName confirm | Out-Null
  Start-Sleep -Seconds 2
}

Write-Host "Registering $ServiceName"
& $Nssm install $ServiceName $python $serve | Out-Null
& $Nssm set $ServiceName AppDirectory (Split-Path $serve -Parent) | Out-Null
& $Nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $Nssm set $ServiceName AppStdout 'C:\forgejo-runner\exporter.out.log' | Out-Null
& $Nssm set $ServiceName AppStderr 'C:\forgejo-runner\exporter.err.log' | Out-Null

# Configuration through the environment, not the command line: a command line
# is readable by every process on the box, and the key path is in here.
$envBlock = @(
  "EXPORTER_BIND=$Bind"
  "EXPORTER_PORT=$Port"
  "EXPORTER_WINDOWS_LOG=$WindowsLog"
  "EXPORTER_MACOS_HOST=$MacosHost"
  "EXPORTER_MACOS_USER=$MacosUser"
  "EXPORTER_MACOS_KEY=$MacosKey"
) -join ' '
& $Nssm set $ServiceName AppEnvironmentExtra $envBlock | Out-Null

$rule = "$ServiceName ($Port)"
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -RemoteAddress $AllowFrom -Profile Any | Out-Null
Write-Host "Firewall: $Port open to $AllowFrom only"

& $Nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Write-Host "$ServiceName is $($svc.Status)"

try {
  $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/metrics" -TimeoutSec 10
  Write-Host "Reporting on: $($r.runners.PSObject.Properties.Name -join ', ')"
} catch {
  Write-Warning "Service is up but /metrics did not answer: $($_.Exception.Message)"
  Write-Warning 'Check C:\forgejo-runner\exporter.err.log'
}
