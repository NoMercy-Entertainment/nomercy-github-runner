<#
.SYNOPSIS
    Configures an installed Windows guest for the runner platform, over
    PowerShell Direct - no network needed (W10b). Run elevated, after Windows
    is installed in the VM New-WindowsGuest.ps1 made and rnr-admin exists.

.DESCRIPTION
    Reaches the guest through Invoke-Command -VMName, which Hyper-V carries
    over the VM bus rather than the network - the guest need not have an
    address yet, which is exactly the setting being configured. Inside it:

      - the management adapter, identified by its static MAC (settings.psd1),
        is given its static address and the platform's DNS servers;
      - OpenSSH.Server is installed, started, and its firewall rule is in
        place;
      - the timezone is set to match this host's;
      - sleep and hibernation are turned off, so a runner is not paused
        mid-job;
      - Remote Desktop is enabled, for when the console is not convenient.

    Nothing about Windows activation is read, checked, or reported: only
    Get-ComputerInfo's edition and version fields come back, for the
    operator to see the install succeeded.

    Every step checks the guest's current state first, so running this again
    changes nothing.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Name = 'rnr-windows-1',
    [Parameter(Mandatory)] [System.Management.Automation.PSCredential] $Credential
)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

if (-not (Test-Elevated)) {
    throw "Run this elevated: PowerShell Direct needs an administrator on the host."
}
if (-not $s.VMs.ContainsKey($Name)) {
    throw "settings.psd1 has no VM named $Name."
}
$spec = $s.VMs[$Name]

$vm = Get-VM -Name $Name -ErrorAction SilentlyContinue
if (-not $vm) {
    throw "$Name does not exist; run New-WindowsGuest.ps1 first."
}
if ($vm.State -ne 'Running') {
    throw "$Name is $($vm.State), not Running; start it and finish the Windows install first."
}

if (-not $PSCmdlet.ShouldProcess($Name, 'configure over PowerShell Direct')) {
    return
}

$hostTimeZone = (Get-TimeZone).Id

$guestConfig = {
    param($MgmtMac, $Address, $PrefixLength, $Dns, $TimeZoneId)
    $ErrorActionPreference = 'Stop'
    $changed = [System.Collections.Generic.List[string]]::new()

    # --- the management adapter, told apart from the uplink one by its MAC ---
    $nic = Get-NetAdapter | Where-Object { ($_.MacAddress -replace '[:-]', '') -eq $MgmtMac }
    if (-not $nic) { throw "No network adapter with MAC $MgmtMac in this guest." }
    $haveAddress = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address }
    if (-not $haveAddress) {
        Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Remove-NetIPAddress -Confirm:$false
        Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled
        New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $Address -PrefixLength $PrefixLength | Out-Null
        $changed.Add("address -> $Address/$PrefixLength")
    }
    $haveDns = @((Get-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4).ServerAddresses | Sort-Object)
    $wantDns = @($Dns | Sort-Object)
    if (($haveDns -join ',') -ne ($wantDns -join ',')) {
        Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses $Dns
        $changed.Add("dns -> $($Dns -join ', ')")
    }

    # --- OpenSSH.Server, so the control plane can reach this guest by key ---
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ($cap.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
        $changed.Add('OpenSSH.Server installed')
    }
    if ((Get-Service sshd).StartType -ne 'Automatic') { Set-Service -Name sshd -StartupType Automatic }
    if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd; $changed.Add('sshd started') }
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
        $changed.Add('OpenSSH firewall rule created')
    }

    # --- timezone, matching the host's, so schedules and logs read the same ---
    if ((Get-TimeZone).Id -ne $TimeZoneId) {
        Set-TimeZone -Id $TimeZoneId
        $changed.Add("timezone -> $TimeZoneId")
    }

    # --- never pause mid-job -------------------------------------------------
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /hibernate off

    # --- Remote Desktop, for when the console is not convenient --------------
    $rdpKey = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
    if ((Get-ItemProperty -Path $rdpKey -Name fDenyTSConnections).fDenyTSConnections -ne 0) {
        Set-ItemProperty -Path $rdpKey -Name fDenyTSConnections -Value 0
        $changed.Add('RDP enabled')
    }
    Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue

    [pscustomobject]@{
        Changed = if ($changed.Count) { $changed -join '; ' } else { '(nothing; already configured)' }
        Info    = Get-ComputerInfo | Select-Object CsName, WindowsProductName, WindowsEditionId,
                                                     WindowsVersion, OsArchitecture
    }
}

$result = Invoke-Command -VMName $Name -Credential $Credential -ScriptBlock $guestConfig `
    -ArgumentList $spec.MgmtMac, $spec.Address, $s.PrefixLength, $s.Dns, $hostTimeZone

Write-Host ""
Write-Host "Changed: $($result.Changed)"
Write-Host ""
$result.Info | Format-List | Out-String | Write-Host
Write-Host "Reachable at $($spec.Address); the operator verifies SSH from the control plane next."
