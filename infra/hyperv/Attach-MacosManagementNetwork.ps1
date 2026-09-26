<#
.SYNOPSIS
    Attach the existing macos-runner appliance to the runner management switch.

.DESCRIPTION
    Run elevated on BEAST-UNIT. Adds one NIC with a fixed MAC, leaving the
    appliance's existing Default Switch NIC and running QEMU guests intact.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$name = 'rnr-mgmt'
$mac = '00155D77280A'
$switch = 'rnr-internal'
$log = 'D:\HyperV\runner-platform\logs\macos-management-network.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Start-Transcript -Path $log -Append | Out-Null
try {
    if (-not (Get-VMSwitch -Name $switch -ErrorAction SilentlyContinue)) {
        throw "The $switch switch is missing."
    }
    $adapter = Get-VMNetworkAdapter -VMName 'macos-runner' -Name $name -ErrorAction SilentlyContinue
    if (-not $adapter) {
        Add-VMNetworkAdapter -VMName 'macos-runner' -Name $name `
            -SwitchName $switch -StaticMacAddress $mac -ErrorAction Stop
        Write-Host "Added $name to macos-runner."
    } else {
        if ($adapter.MacAddress -ne $mac) { throw "$name has unexpected MAC $($adapter.MacAddress)." }
        if ($adapter.SwitchName -ne $switch) {
            Connect-VMNetworkAdapter -VMNetworkAdapter $adapter -SwitchName $switch -ErrorAction Stop
            Write-Host "Reconnected $name to $switch."
        }
    }
    Get-VMNetworkAdapter -VMName 'macos-runner' |
        Select-Object VMName,Name,SwitchName,MacAddress,Status,IPAddresses |
        Format-Table -AutoSize | Out-String | Write-Host
} catch {
    Write-Host "ATTACH FAILED: $($_.Exception.Message)"
    $_ | Format-List * -Force | Out-String | Write-Host
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
