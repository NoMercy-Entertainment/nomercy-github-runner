<#
.SYNOPSIS
  Make the runner dashboard reachable at http://192.168.178.19:9200.

.DESCRIPTION
  The dashboard's port is published inside the github-runners WSL distro, on
  the distro's own NAT address. Windows 10 has no WSL mirrored networking
  (that needs Windows 11 22H2+), so the host only ever gets a loopback relay:
  127.0.0.1:9200 works and the LAN address does not. This bridges that last
  hop with a portproxy.

  The distro's address changes every time WSL restarts, which silently breaks
  a hand-written rule. That is why this is a script and not a one-off command:
  run it again and it re-derives the address and rewrites the rule.

  Idempotent. Safe to re-run, safe to schedule at logon.

.NOTES
  Requires elevation: both netsh portproxy and the firewall rule do.
#>
[CmdletBinding()]
param(
    [string] $Distro     = 'github-runners',
    [int]    $Port       = 9200,
    # Bound to this address specifically, never 0.0.0.0: the host also has
    # Docker, WSL and virtual adapters, and a wildcard bind would publish the
    # dashboard on all of them instead of the one that was asked for.
    [string] $ListenOn   = '192.168.178.19',
    [string] $AllowFrom  = '192.168.178.0/24',
    [string] $RuleName   = 'NoMercy Runners dashboard'
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

# --- the distro's current address -----------------------------------------
$raw = (wsl -d $Distro -u root -- hostname -I) 2>$null
if (-not $raw) { Fail "distro '$Distro' did not answer - is it running?" }

# hostname -I lists every address; the docker bridges (172.17/172.18) are not
# the one to talk to. Take the first that is neither a bridge nor loopback.
$wslIp = ($raw -split '\s+' |
    Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -notmatch '^172\.1[78]\.' -and $_ -ne '127.0.0.1' } |
    Select-Object -First 1)
if (-not $wslIp) { Fail "no usable address in '$raw'" }
Ok "distro $Distro is at $wslIp"

# --- the host address must actually exist ---------------------------------
if (-not (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object IPAddress -eq $ListenOn)) {
    Fail "$ListenOn is not an address on this host - check the LAN adapter or a changed DHCP lease"
}
Ok "host address $ListenOn present"

# --- IP Helper carries portproxy; without it the rule exists but does nothing
$svc = Get-Service iphlpsvc -ErrorAction SilentlyContinue
if (-not $svc) { Fail 'IP Helper (iphlpsvc) not found - portproxy cannot work without it' }
if ($svc.Status -ne 'Running') {
    Start-Service iphlpsvc
    Info 'started IP Helper (it was stopped - portproxy silently does nothing then)'
}
Ok "IP Helper running"

# --- rewrite the portproxy rule -------------------------------------------
# Delete first: netsh add on an existing listener errors instead of updating,
# and after a WSL restart the existing one points at a dead address.
netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=$ListenOn 2>&1 | Out-Null
netsh interface portproxy add v4tov4 `
      listenport=$Port listenaddress=$ListenOn `
      connectport=$Port connectaddress=$wslIp | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "netsh portproxy add returned $LASTEXITCODE" }
Ok "portproxy ${ListenOn}:${Port} -> ${wslIp}:${Port}"

# --- firewall -------------------------------------------------------------
$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Set-NetFirewallRule -RemoteAddress $AllowFrom -Enabled True
    Ok "firewall rule updated (from $AllowFrom)"
} else {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -RemoteAddress $AllowFrom `
        -Profile Any -Description 'Runner dashboard, LAN only' | Out-Null
    Ok "firewall rule created (from $AllowFrom)"
}

# --- prove it, rather than assume it --------------------------------------
$probe = Test-NetConnection -ComputerName $ListenOn -Port $Port -WarningAction SilentlyContinue
if ($probe.TcpTestSucceeded) {
    Ok "http://${ListenOn}:${Port} answers"
} else {
    Fail "http://${ListenOn}:${Port} still does not answer - portproxy is set but nothing is behind it"
}
