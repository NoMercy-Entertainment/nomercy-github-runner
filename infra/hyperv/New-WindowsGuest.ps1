<#
.SYNOPSIS
    The Windows guest that takes the Windows runners over (W10b, design
    section "The Windows runners move into their own Hyper-V guest"). Run
    elevated.

.DESCRIPTION
    Creates, once, the VM named by -Name (settings.psd1's 'rnr-windows-1'
    entry unless a different one is given): Generation 2, static memory
    (OPEN-5), two dynamic VHDX under $s.Root\vms\<name> (a system disk of
    DiskGB and a data disk of DataDiskGB for the runners' own VHDX store),
    one adapter on the Internal switch with a static address and one on the
    Default Switch for outbound traffic - both with their static MACs -
    vTPM and Secure Boot on (Windows 11 requires both), and the install ISO
    in a DVD drive as the first boot device. Starts the VM and prints what
    the operator does next.

    Refuses to run unelevated, refuses if the VM already exists (this script
    only creates; it never reconfigures a VM that is there), and refuses to
    create it when, with its static memory reserved, less than
    CommitReserveGB of commit would be left - the same check
    New-RunnerPlatformVMs.ps1 makes, for the same reason: the host has no
    pagefile and a static reservation is committed at once.

    Nothing about a Windows licence is read, stored, or passed to anything
    here. Windows Setup runs from the ISO at the console; the operator enters
    and activates the licence there, and creates the local administrator
    account as rnr-admin. Nothing here reads that back.

    Idempotent in the only way a one-time creation can be: run it again
    against a VM that already exists and it refuses without changing
    anything; a VHDX already on disk from an earlier, interrupted attempt is
    reused rather than recreated.

    -WhatIf shows what would be done without doing it.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $Iso,
    [string] $Name = 'rnr-windows-1'
)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

if (-not (Test-Elevated)) {
    throw "Run this elevated: Hyper-V needs an administrator."
}
if (-not (Test-Path -LiteralPath $Iso -PathType Leaf)) {
    throw "No install ISO at $Iso."
}
if (-not $s.VMs.ContainsKey($Name)) {
    throw "settings.psd1 has no VM named $Name."
}
$spec = $s.VMs[$Name]
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) {
    throw "$Name already exists; this script only creates. Remove it first if you meant to start over."
}
if (-not (Get-VMSwitch -Name $s.Switch -ErrorAction SilentlyContinue)) {
    throw "There is no '$($s.Switch)' switch; run New-RunnerPlatformVMs.ps1 first."
}
if (-not (Get-VMSwitch -Name $s.UplinkSwitch -ErrorAction SilentlyContinue)) {
    throw "There is no '$($s.UplinkSwitch)' for the VM's outbound traffic."
}

$headroom = Get-CommitHeadroomGB
$after = $headroom - $spec.MemoryGB
if ($after -lt $s.CommitReserveGB) {
    throw ("Not creating ${Name}: {0} GB of commit free, {1} GB after its {2} GB, " +
           "under the {3} GB reserve.") -f $headroom, $after, $spec.MemoryGB, $s.CommitReserveGB
}

if (-not $PSCmdlet.ShouldProcess($Name, ("create: {0} GB static, {1} vCPU, {2}+{3} GB disks, {4}" -f
        $spec.MemoryGB, $spec.Cpus, $spec.DiskGB, $spec.DataDiskGB, $spec.Address))) {
    return
}

# --- disks -----------------------------------------------------------------------
$dir = Join-Path $s.Root "vms\$Name"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$sysDisk = Join-Path $dir "$Name.vhdx"
$dataDisk = Join-Path $dir "$Name-data.vhdx"
if (-not (Test-Path $sysDisk)) {
    New-VHD -Path $sysDisk -SizeBytes ([uint64]$spec.DiskGB * 1GB) -Dynamic | Out-Null
}
if (-not (Test-Path $dataDisk)) {
    New-VHD -Path $dataDisk -SizeBytes ([uint64]$spec.DataDiskGB * 1GB) -Dynamic | Out-Null
}

# --- the VM ------------------------------------------------------------------------
New-VM -Name $Name -Generation 2 -NoVHD -Path (Join-Path $s.Root 'vms') `
    -MemoryStartupBytes ([int64]$spec.MemoryGB * 1GB) -SwitchName $s.Switch | Out-Null
Add-VMHardDiskDrive -VMName $Name -Path $sysDisk
Add-VMHardDiskDrive -VMName $Name -Path $dataDisk

# Two adapters, told apart in the guest by these addresses, exactly as the
# other guests' are: management on rnr-internal, outbound on the uplink.
Get-VMNetworkAdapter -VMName $Name | Rename-VMNetworkAdapter -NewName 'mgmt'
Set-VMNetworkAdapter -VMName $Name -Name 'mgmt' -StaticMacAddress $spec.MgmtMac
Add-VMNetworkAdapter -VMName $Name -Name 'uplink' -SwitchName $s.UplinkSwitch -StaticMacAddress $spec.UplinkMac
Set-VMMemory -VMName $Name -DynamicMemoryEnabled $false
Set-VMProcessor -VMName $Name -Count $spec.Cpus

# vTPM and Secure Boot: Windows 11 refuses to install without both. A local
# key protector is enough - this host runs no Host Guardian Service.
Set-VMKeyProtector -VMName $Name -NewLocalKeyProtector
Enable-VMTPM -VMName $Name
Set-VMFirmware -VMName $Name -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows

# The system disk is blank; Setup runs from the ISO, so the DVD boots first.
Add-VMDvdDrive -VMName $Name -Path $Iso
Set-VMFirmware -VMName $Name -FirstBootDevice (Get-VMDvdDrive -VMName $Name)

# Back after a host restart; shut down cleanly with the host, never saved (a
# saved VM keeps a file the size of its memory on disk). Automatic checkpoints
# off: a runner host runs on its own disks, not on a differencing chain.
Set-VM -Name $Name -AutomaticStartAction Start -AutomaticStartDelay 30 `
    -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false `
    -Notes "runner platform: $($spec.Role), $($spec.Address)"
Start-VM -Name $Name

Write-Host ""
Write-Host (("{0} created and started: {1} GB static, {2} vCPU, {3} GB system disk, {4} GB data disk, {5}; " +
    "commit free now {6} GB") -f $Name, $spec.MemoryGB, $spec.Cpus, $spec.DiskGB, $spec.DataDiskGB, $spec.Address,
    (Get-CommitHeadroomGB))
Write-Host ""
Write-Host "Open its console:  vmconnect.exe localhost $Name"
Write-Host ""
Write-Host "At the console: install Windows 11 Pro from the attached ISO, enter and activate your"
Write-Host "licence there - nothing about it is read or stored by anything in this repository -"
Write-Host "and name the local administrator account '$($s.AdminUser)'."
Write-Host ""
Write-Host "What this media does by itself, so none of it looks like a fault:"
Write-Host "  - hardware checks and hardware questions are bypassed; Setup still shows its own"
Write-Host "    licence-key page for you to fill in;"
Write-Host "  - a debloat pass runs during the 'specialize' phase - the console can go black or"
Write-Host "    sit idle for several minutes there; that is expected, not a hang;"
Write-Host "  - every network adapter stays disabled until the first logon, so expect no network"
Write-Host "    (and no successful activation) until you have signed in once."
Write-Host ""
Write-Host "Once Windows is installed and $($s.AdminUser) exists, run elevated:"
Write-Host "  $PSScriptRoot\Initialize-WindowsGuest.ps1 -Name $Name -Credential (Get-Credential $($s.AdminUser))"
