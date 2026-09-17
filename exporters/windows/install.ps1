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
  [string]$Python      = '',
  [string]$Nssm        = 'C:\forgejo-runner\nssm.exe',
  [string]$Bind        = '0.0.0.0',
  [int]   $Port        = 9101,
  [string]$MacosHost   = '172.19.136.46',
  [string]$MacosUser   = 'runner',
  [string]$MacosKey    = "$env:USERPROFILE\.ssh\macos_runner",
  [string]$WindowsLog  = 'C:\forgejo-runner\runner.err.log',
  # Windows ships OpenSSH in System32 but does not put it on PATH, so the
  # exporter cannot find it by name and would silently skip the macOS probe
  # while still answering 200 and still listing the runner, as null.
  [string]$Ssh         = "$env:SystemRoot\System32\OpenSSH\ssh.exe",
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
if (-not (Test-Path $Ssh))      { throw "ssh.exe not found at $Ssh" }

# The python.exe on PATH here is usually the Microsoft Store execution alias
# in WindowsApps. That alias is a per-user reparse point: a service running as
# LocalSystem cannot follow it and dies instantly with "The system cannot find
# the path specified" and an empty log. Resolve a real interpreter instead.
function Resolve-RealPython {
  foreach ($c in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
    if ($c.Source -and $c.Source -notlike '*\WindowsApps\*') { return $c.Source }
  }
  foreach ($root in 'HKLM:\SOFTWARE\Python\PythonCore', 'HKCU:\SOFTWARE\Python\PythonCore') {
    foreach ($v in (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
      $ip = (Get-ItemProperty "$($v.PSPath)\InstallPath" -ErrorAction SilentlyContinue).'(default)'
      if ($ip) {
        $exe = Join-Path $ip 'python.exe'
        if (Test-Path $exe) { return $exe }
      }
    }
  }
  return $null
}

$python = if ($Python) { $Python } else { Resolve-RealPython }
if (-not $python) {
  throw ('No python.exe usable by a service was found. The Store alias in ' +
         'WindowsApps does not work for LocalSystem. Install Python from ' +
         'python.org, or pass -Python <path> to a real interpreter.')
}
Write-Host "Interpreter: $python"

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
# OpenSSH refuses a private key it considers world-readable, and running as
# LocalSystem it judged the user's own key "too open" - the macOS probe
# failed on every sweep with UNPROTECTED PRIVATE KEY FILE while the exporter
# still answered 200. Give the service its own copy, owned by SYSTEM and
# readable by nobody else, rather than loosening the user's own key.
$svcKey = Join-Path (Split-Path $Nssm -Parent) 'exporter_macos_key'
Copy-Item -Path $MacosKey -Destination $svcKey -Force
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)   # drop inherited folder rights
$system = New-Object System.Security.Principal.NTAccount('NT AUTHORITY\SYSTEM')
$acl.SetOwner($system)
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
  $system, 'FullControl', 'Allow')))
Set-Acl -Path $svcKey -AclObject $acl
Write-Host "Service key: $svcKey (SYSTEM only)"

# Each variable as its OWN argument. Joined with spaces, NSSM stores the whole
# string as the value of the FIRST variable, so the exporter was handed a bind
# address of "0.0.0.0 EXPORTER_PORT=9101 ..." and died on getaddrinfo.
$envVars = @(
  "EXPORTER_BIND=$Bind"
  "EXPORTER_PORT=$Port"
  "EXPORTER_WINDOWS_LOG=$WindowsLog"
  "EXPORTER_MACOS_HOST=$MacosHost"
  "EXPORTER_MACOS_USER=$MacosUser"
  "EXPORTER_MACOS_KEY=$svcKey"
  "EXPORTER_POWERSHELL=$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  "EXPORTER_SSH=$Ssh"
)
& $Nssm set $ServiceName AppEnvironmentExtra @envVars | Out-Null

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
