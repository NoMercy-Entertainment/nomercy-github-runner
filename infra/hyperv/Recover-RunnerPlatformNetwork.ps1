<#
.SYNOPSIS
    Restore the runner management switch after a Windows network reset.

.DESCRIPTION
    Run elevated on BEAST-UNIT. Only creates the missing internal switch,
    restores its host IP, and reconnects the existing named management NICs.
    VM disks, guest power state, and the Default Switch are not changed.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$logDir = 'D:\HyperV\runner-platform\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'hyperv-network-recovery.log'
Start-Transcript -Path $log -Append | Out-Null
try {
    . (Join-Path $PSScriptRoot 'lib.ps1')
    $settings = Get-RunnerPlatformSettings
    $switch = Get-VMSwitch -Name $settings.Switch -ErrorAction SilentlyContinue
    if (-not $switch) {
        $switch = New-VMSwitch -Name $settings.Switch -SwitchType Internal
        Write-Host "Created internal switch $($switch.Name)"
    } elseif ($switch.SwitchType -ne 'Internal') {
        throw "$($settings.Switch) exists but is not an Internal switch."
    }

    $alias = "vEthernet ($($settings.Switch))"
    if (-not (Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object IPAddress -eq $settings.HostAddress)) {
        New-NetIPAddress -InterfaceAlias $alias -IPAddress $settings.HostAddress `
            -PrefixLength $settings.PrefixLength | Out-Null
        Write-Host "Assigned $($settings.HostAddress)/$($settings.PrefixLength) to $alias"
    }

    foreach ($name in $settings.VMs.Keys) {
        $nic = Get-VMNetworkAdapter -VMName $name -Name 'mgmt' -ErrorAction SilentlyContinue
        if (-not $nic) { throw "$name has no existing mgmt NIC; refusing to create a replacement." }
        if ($nic.MacAddress -ne $settings.VMs[$name].MgmtMac) {
            throw "$name mgmt MAC is $($nic.MacAddress), expected $($settings.VMs[$name].MgmtMac)."
        }
        if ($nic.SwitchName -ne $settings.Switch) {
            Connect-VMNetworkAdapter -VMNetworkAdapter $nic -SwitchName $settings.Switch -ErrorAction Stop
            Write-Host "Reconnected $name mgmt to $($settings.Switch)"
        }
    }

    foreach ($name in $settings.VMs.Keys) {
        $state = (Get-VM -Name $name).State
        if ($state -in @('Off', 'Saved')) {
            Start-VM -Name $name -ErrorAction Stop | Out-Null
            Write-Host "Started $name from $state"
        } else {
            Write-Host "$name state: $state"
        }
    }

    Get-VMSwitch | Select-Object Name,SwitchType | Format-Table | Out-String | Write-Host
    Get-VMNetworkAdapter -VMName @($settings.VMs.Keys) |
        Select-Object VMName,Name,SwitchName,MacAddress,Status,IPAddresses |
        Format-Table -AutoSize | Out-String | Write-Host
    Write-Host 'Network switch recovery finished.'
} catch {
    Write-Host "RECOVERY FAILED: $($_.Exception.Message)"
    $_ | Format-List * -Force | Out-String | Write-Host
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
