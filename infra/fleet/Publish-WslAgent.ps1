<#
.SYNOPSIS
  Let the control plane reach the agent inside the WSL distro. Elevated.

.DESCRIPTION
  The controller lives on a Hyper-V VM and talks to workers over the internal
  switch; the WSL distro's addresses are behind the host and no traffic
  crosses between the two networks. This bridges that last hop with a
  portproxy on the host's internal-switch address, and opens the firewall for
  the control plane's subnet alone.

  The distro's address changes every time WSL restarts, which silently breaks
  a hand-written rule - the same trap `publish-dashboard-lan.ps1` exists for.
  So this is a script: run it again and it re-derives the address and
  rewrites the rule. Worth scheduling beside the dashboard's own.

  Nothing is exposed to the LAN: the listener is bound to the internal switch
  address, and the agent refuses anyone without the controller's certificate
  even there.

.EXAMPLE
  .\Publish-WslAgent.ps1
#>
[CmdletBinding()]
param(
    [string] $Distro = 'github-runners',
    # The host's address on the internal switch the platform's VMs use.
    [string] $ListenOn = '10.77.0.1',
    [int]    $ListenPort = 8453,
    [int]    $AgentPort = 8443,
    [string] $AllowFrom = '10.77.0.0/24',
    [string] $RuleName = 'NoMercy Runners WSL agent'
)
$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "FAIL  $msg" -ForegroundColor Red; exit 1 }
function Ok($msg)   { Write-Host "ok    $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "      $msg" -ForegroundColor DarkGray }

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'must run elevated - netsh portproxy and the firewall rule both need admin'
}

$raw = (wsl -d $Distro -u root -- hostname -I) 2>$null
if (-not $raw) { Fail "distro '$Distro' did not answer - is it running?" }
$wslIp = ($raw -split '\s+' | Where-Object {
    $_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -notmatch '^172\.1[78]\.' -and $_ -ne '127.0.0.1'
} | Select-Object -First 1)
if (-not $wslIp) { Fail "no usable address in '$raw'" }
Ok "distro $Distro is at $wslIp"

if (-not (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object IPAddress -eq $ListenOn)) {
    Fail "$ListenOn is not an address on this host - is the rnr-internal switch there?"
}
Ok "host address $ListenOn present"

$svc = Get-Service iphlpsvc -ErrorAction SilentlyContinue
if (-not $svc) { Fail 'IP Helper (iphlpsvc) not found - portproxy cannot work without it' }
if ($svc.Status -ne 'Running') {
    Start-Service iphlpsvc
    Info 'started IP Helper (it was stopped - portproxy silently does nothing then)'
}
Ok 'IP Helper running'

netsh interface portproxy delete v4tov4 listenport=$ListenPort listenaddress=$ListenOn 2>&1 | Out-Null
netsh interface portproxy add v4tov4 `
      listenport=$ListenPort listenaddress=$ListenOn `
      connectport=$AgentPort connectaddress=$wslIp | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "netsh portproxy add returned $LASTEXITCODE" }
Ok "portproxy ${ListenOn}:${ListenPort} -> ${wslIp}:${AgentPort}"

$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Set-NetFirewallRule -RemoteAddress $AllowFrom -Enabled True
    Ok "firewall rule updated (from $AllowFrom)"
} else {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $ListenPort -RemoteAddress $AllowFrom `
        -Profile Any -Description 'Control plane to the WSL worker''s agent' | Out-Null
    Ok "firewall rule created (from $AllowFrom)"
}

$probe = Test-NetConnection -ComputerName $ListenOn -Port $ListenPort -WarningAction SilentlyContinue
if ($probe.TcpTestSucceeded) {
    Ok "${ListenOn}:${ListenPort} answers"
} else {
    Fail "${ListenOn}:${ListenPort} does not answer - the rule is set but nothing is behind it; is runner-agent running in $Distro?"
}
