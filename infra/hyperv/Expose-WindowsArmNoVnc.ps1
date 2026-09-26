[CmdletBinding()]
param(
    [string]$ListenAddress = '192.168.178.19',
    [string]$ConnectAddress = '10.77.0.40',
    [int]$Port = 8898,
    [string]$ResultPath = 'D:\HyperV\runner-platform\stage\windows-arm-novnc-result.json'
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ State = 'failed'; Url = "http://${ListenAddress}:$Port/"; Detail = $null }
try {
    if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction SilentlyContinue)) {
        throw "$ListenAddress is not assigned to this host."
    }
    $entry = & netsh.exe interface portproxy show v4tov4 | Select-String -Pattern "^\s*$([regex]::Escape($ListenAddress))\s+$Port\s+"
    $verb = if ($entry) { 'set' } else { 'add' }
    & netsh.exe interface portproxy $verb v4tov4 "listenport=$Port" "connectaddress=$ConnectAddress" "connectport=$Port" "listenaddress=$ListenAddress" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not $verb the Windows ARM noVNC port proxy." }

    $rule = Get-NetFirewallRule -Name 'rnr-windows-arm-novnc' -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule -Name 'rnr-windows-arm-novnc' -DisplayName 'Windows ARM noVNC (LAN)' `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
            -LocalAddress $ListenAddress -RemoteAddress '192.168.178.0/24' | Out-Null
    } else {
        Enable-NetFirewallRule -Name 'rnr-windows-arm-novnc' | Out-Null
    }

    $response = Invoke-WebRequest -Uri $result.Url -Method Head -TimeoutSec 10
    if ($response.StatusCode -ne 200) { throw "noVNC returned HTTP $($response.StatusCode)." }
    $result.State = 'ready'
} catch {
    $result.Detail = $_.Exception.Message
} finally {
    $result | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding utf8
    Write-Host "Windows ARM noVNC: $($result.State) $($result.Url) $($result.Detail)"
}
