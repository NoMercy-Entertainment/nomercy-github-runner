<#
.SYNOPSIS
    Point the existing LAN noVNC proxy at the macOS appliance's stable IP.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$log = 'D:\HyperV\runner-platform\logs\novnc-portproxy-repair.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Start-Transcript -Path $log -Append | Out-Null
try {
    $listen = '192.168.178.19'
    $connect = '10.77.0.40'
    if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $listen -ErrorAction SilentlyContinue)) {
        throw "$listen is not an address on this host."
    }
    & netsh.exe interface portproxy set v4tov4 listenport=8899 `
        connectaddress=$connect connectport=8899 listenaddress=$listen
    if ($LASTEXITCODE -ne 0) { throw "netsh failed: $LASTEXITCODE" }
    & netsh.exe interface portproxy show v4tov4
} catch {
    Write-Host "PROXY REPAIR FAILED: $($_.Exception.Message)"
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
