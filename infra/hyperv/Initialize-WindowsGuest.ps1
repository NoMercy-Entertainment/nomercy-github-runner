<#
.SYNOPSIS
    Configures an installed Windows guest for the runner platform, over
    PowerShell Direct - no network needed (W10b). Run elevated, after Windows
    is installed in the VM New-WindowsGuest.ps1 made and rnr-admin exists.

.DESCRIPTION
    First, on the host side, before the guest is ever touched: the install
    media is ejected (Set-VMDvdDrive -Path $null) and the firmware's first
    boot device is set back to the system disk. The install media put the
    DVD first so Setup could run from a blank disk; leaving it there is how
    an unattended multi-reboot install has occasionally been reported to
    loop back into WinPE. Both are idempotent - safe whether or not the
    media was already out.

    Then it reaches the guest through Invoke-Command -VMName, which Hyper-V
    carries over the VM bus rather than the network - the guest need not have
    an address yet, which is exactly the setting being configured. Inside it:

      - the management adapter, identified by its static MAC (settings.psd1):
        the install media's autounattend disables every adapter in the
        specialize pass and only re-enables them at first logon, so this
        checks the adapter's status first and enables it if it is still
        disabled, before giving it its static address and the platform's DNS
        servers;
      - OpenSSH.Server is installed, started, and its firewall rule is in
        place - refusing clearly, naming the media's debloat pass as the
        likely cause, if the capability or its install source is gone;
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

# PowerShell Direct reads a bare user name as a domain account and answers
# "The credential is invalid" - the guest's local account has to be named
# ".\<user>". Operators type `Get-Credential rnr-admin`, so make the local
# form here rather than making that their problem.
if ($Credential.UserName -notmatch '[\\@]') {
    $Credential = [System.Management.Automation.PSCredential]::new(
        ".\$($Credential.UserName)", $Credential.Password)
}

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

if (-not $PSCmdlet.ShouldProcess($Name, 'eject the install media, restore the boot order, and configure over PowerShell Direct')) {
    return
}

# --- host side, before the guest is touched at all: settle the boot order ---------
# New-WindowsGuest.ps1 boots the DVD first so Setup has something to run from
# a blank disk. Leaving it first afterwards is how an unattended install has
# occasionally been reported to loop back into WinPE on a mid-install reboot.
# Both steps are safe to repeat.
$hostChanges = [System.Collections.Generic.List[string]]::new()
$dvd = Get-VMDvdDrive -VMName $Name -ErrorAction SilentlyContinue
if ($dvd -and $dvd.Path) {
    $dvd | Set-VMDvdDrive -Path $null
    $hostChanges.Add('install media ejected')
}
# This guest was created before New-WindowsGuest.ps1 started turning
# automatic checkpoints off, so make sure of it here too, and clear any
# checkpoint already taken: a runner host runs on its own disks, not on a
# differencing chain, and only removing the checkpoint - then waiting for its
# merge - gets the VM off the .avhdx files it left behind. Safe to repeat:
# with checkpoints already off and none present, this changes nothing.
if ((Get-VM -Name $Name).AutomaticCheckpointsEnabled) {
    Set-VM -Name $Name -AutomaticCheckpointsEnabled $false
    $hostChanges.Add('automatic checkpoints turned off')
}
# Get-VMSnapshot/Remove-VMSnapshot, not their -VMCheckpoint aliases: the
# aliases live in the Hyper-V module, and PowerShell 7 loads that module
# through the Windows PowerShell compatibility layer, which exports its
# cmdlets but not its aliases. The alias form fails in pwsh with "not
# recognized", which is exactly how this was found.
$existingCheckpoints = Get-VMSnapshot -VMName $Name -ErrorAction SilentlyContinue
if ($existingCheckpoints) {
    $existingCheckpoints | Remove-VMSnapshot
    $mergeTimeout = (Get-Date).AddMinutes(10)
    while (Get-VMHardDiskDrive -VMName $Name | Where-Object { $_.Path -match '\.avhdx$' }) {
        if ((Get-Date) -gt $mergeTimeout) {
            throw ("$Name's checkpoint removal did not finish merging within 10 minutes; check " +
                   "Get-VM $Name | Select-Object -ExpandProperty Status and the disk files under " +
                   "its directory by hand before running this again.")
        }
        Start-Sleep -Seconds 5
    }
    $hostChanges.Add('checkpoint(s) removed and merged back into their base disks')
}

# Only now is the system disk findable by its own name: while a checkpoint
# exists the VM runs on "<name>_<guid>.avhdx", so this has to come after the
# merge above. It is still matched by name rather than by position, because
# "the first disk" is exactly the assumption that would point the firmware at
# the data disk if the two were ever attached the other way round. The data
# disk's own files all begin "<name>-data", which this cannot match.
$sysHdd = Get-VMHardDiskDrive -VMName $Name |
    Where-Object { (Split-Path $_.Path -Leaf) -eq "$Name.vhdx" } |
    Select-Object -First 1
if (-not $sysHdd) {
    $found = (Get-VMHardDiskDrive -VMName $Name | ForEach-Object { Split-Path $_.Path -Leaf }) -join ', '
    throw ("Could not find ${Name}'s system disk (${Name}.vhdx) among its hard disk drives; " +
           "it has: $found. A name ending in .avhdx means a checkpoint is still merging - " +
           "wait for it to finish and run this again.")
}
Set-VMFirmware -VMName $Name -FirstBootDevice $sysHdd

$hostTimeZone = (Get-TimeZone).Id

$guestConfig = {
    param($MgmtMac, $Address, $PrefixLength, $Dns, $TimeZoneId)
    $ErrorActionPreference = 'Stop'
    $changed = [System.Collections.Generic.List[string]]::new()

    # --- the management adapter, told apart from the uplink one by its MAC ---
    $nic = Get-NetAdapter | Where-Object { ($_.MacAddress -replace '[:-]', '') -eq $MgmtMac }
    if (-not $nic) { throw "No network adapter with MAC $MgmtMac in this guest." }
    # The install media's autounattend disables every adapter in the
    # specialize pass and only re-enables them at first logon (a
    # FirstLogonCommands entry). The operator's own console session normally
    # reaches first logon before this script ever runs, but do not assume it.
    if ($nic.Status -eq 'Disabled') {
        Enable-NetAdapter -InterfaceIndex $nic.ifIndex -Confirm:$false | Out-Null
        Start-Sleep -Seconds 2
        $nic = Get-NetAdapter -InterfaceIndex $nic.ifIndex
        $changed.Add('mgmt adapter enabled (the install media leaves every adapter disabled until first logon)')
    }
    if ($nic.Status -ne 'Up') {
        throw "The mgmt adapter ($($nic.Name)) is $($nic.Status), not Up, even after enabling it - check the switch and cable inside Hyper-V."
    }
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
    # A debloat pass on the install media is known to strip Feature-on-Demand
    # payloads or disable Windows Update, either of which takes away
    # Add-WindowsCapability's install source; refuse clearly instead of
    # letting an opaque DISM error stand for it.
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction SilentlyContinue
    if (-not $cap) {
        throw ("OpenSSH.Server is not a capability this image knows about - the install media's " +
               "debloat pass likely removed its source. Install it by hand (Settings > System > " +
               "Optional features > Add a feature, or DISM against a Feature-on-Demand ISO), then " +
               "run this again.")
    }
    if ($cap.State -ne 'Installed') {
        try {
            Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null
        } catch {
            throw ("Installing OpenSSH.Server failed: $($_.Exception.Message) - likely the install " +
                   "media's debloat pass removed its payload or disabled Windows Update as its " +
                   "source. Install it by hand, then run this again.")
        }
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

$changeParts = @($hostChanges)
if ($result.Changed -ne '(nothing; already configured)') { $changeParts += $result.Changed }
$summary = if ($changeParts.Count) { $changeParts -join '; ' } else { '(nothing; already configured)' }
Write-Host ""
Write-Host "Changed: $summary"
Write-Host ""
$result.Info | Format-List | Out-String | Write-Host
Write-Host "Reachable at $($spec.Address); the operator verifies SSH from the control plane next."

# --- Secure Boot: back on now that the guest itself is configured ----------------
# New-WindowsGuest.ps1 turns Secure Boot on by default (Windows 11 requires it),
# but this guest's install media is a repacked ISO - rebuilt to carry an
# autounattend.xml - and Hyper-V refused to boot it with Secure Boot on ("SCSI
# DVD (0,2) The boot loader failed"); the operator turned Secure Boot off to get
# Setup running. It is the repacked media's loader that failed validation, not
# Windows itself: the installed Windows bootloader is Microsoft-signed and boots
# fine under Secure Boot, so put it back on here, now that the guest
# configuration above has already succeeded - never before, so a reboot for this
# is not risked against an unconfigured guest. Idempotent: already On, do nothing.
$firmware = Get-VMFirmware -VMName $Name
if ($firmware.SecureBoot -eq 'On') {
    Write-Host ""
    Write-Host "Secure Boot is already on for $Name; nothing to do."
} else {
    Stop-VM -Name $Name
    $stopDeadline = (Get-Date).AddMinutes(5)
    while ((Get-VM -Name $Name).State -ne 'Off') {
        if ((Get-Date) -gt $stopDeadline) {
            throw "$Name did not reach Off within 5 minutes of Stop-VM; check its state by hand before running this again."
        }
        Start-Sleep -Seconds 5
    }
    Set-VMFirmware -VMName $Name -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
    Start-VM -Name $Name
    $directDeadline = (Get-Date).AddMinutes(5)
    $direct = $false
    while (-not $direct -and (Get-Date) -le $directDeadline) {
        try {
            $direct = Invoke-Command -VMName $Name -Credential $Credential -ScriptBlock { $true } -ErrorAction Stop
        } catch {
            Start-Sleep -Seconds 5
        }
    }
    if (-not $direct) {
        throw ("$Name did not answer PowerShell Direct within 5 minutes of turning Secure Boot back on. " +
               "To undo: Set-VMFirmware -VMName $Name -EnableSecureBoot Off, then start the VM again.")
    }
    $hostChanges.Add('Secure Boot turned back on (was off only to let the repacked install media boot)')
    Write-Host ""
    Write-Host "Secure Boot turned back on for $Name; the guest answered PowerShell Direct again."
}
