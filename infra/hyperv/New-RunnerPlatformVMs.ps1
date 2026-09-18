<#
.SYNOPSIS
    The runner platform's network and VMs (design phase 5: T-0601, T-0602,
    T-0604). Run elevated, after Prepare-RunnerPlatform.ps1.

.DESCRIPTION
    Creates, only where missing:
      - the Internal switch and the host's address on it (OPEN-6),
      - a NetNat so the guests reach the internet through the host,
      - each VM in settings.psd1: Generation 2, Secure Boot for Ubuntu, static
        memory (OPEN-5), its own disk made from the verified base image, its
        seed image in the DVD drive - and starts it.

    Touches nothing else. No existing VM, switch or NAT is changed, the macOS
    appliance included; an existing one with the same name is left as it is.

    Refuses to create a VM when, with that VM's memory reserved, less than
    CommitReserveGB of commit would be left: the host has no pagefile, a
    static reservation is committed at once, and commit exhaustion is what has
    killed the WSL VM - and every running job with it - twice (R-2).

    -WhatIf shows what would be done without doing it.
#>
[CmdletBinding(SupportsShouldProcess)]
param()
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

if (-not (Test-Elevated)) {
    throw "Run this elevated: Hyper-V and the host's network need an administrator."
}
$base = Join-Path $s.Root 'base\noble-server-cloudimg-amd64.vhdx'
if (-not (Test-Path $base)) { throw "No base image at $base; run Prepare-RunnerPlatform.ps1 first." }

# --- network -------------------------------------------------------------------
$switch = Get-VMSwitch -Name $s.Switch -ErrorAction SilentlyContinue
if (-not $switch) {
    if ($PSCmdlet.ShouldProcess($s.Switch, 'create Internal switch')) {
        $switch = New-VMSwitch -Name $s.Switch -SwitchType Internal
    }
} elseif ($switch.SwitchType -ne 'Internal') {
    throw "A switch named $($s.Switch) exists and is $($switch.SwitchType), not Internal; not touching it."
}

$alias = "vEthernet ($($s.Switch))"
$hostIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $s.HostAddress }
if ($hostIp -and $hostIp.InterfaceAlias -ne $alias) {
    throw "$($s.HostAddress) is already in use on $($hostIp.InterfaceAlias)."
}
if (-not $hostIp -and $PSCmdlet.ShouldProcess($alias, "assign $($s.HostAddress)/$($s.PrefixLength)")) {
    New-NetIPAddress -InterfaceAlias $alias -IPAddress $s.HostAddress -PrefixLength $s.PrefixLength | Out-Null
}

$nat = Get-NetNat -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $s.Nat }
if (-not $nat) {
    $overlap = Get-NetNat -ErrorAction SilentlyContinue |
        Where-Object { $_.InternalIPInterfaceAddressPrefix -eq $s.Prefix }
    if ($overlap) { throw "Another NAT ($($overlap.Name)) already covers $($s.Prefix)." }
    if ($PSCmdlet.ShouldProcess($s.Prefix, "create NetNat $($s.Nat)")) {
        New-NetNat -Name $s.Nat -InternalIPInterfaceAddressPrefix $s.Prefix | Out-Null
    }
}

# --- VMs -------------------------------------------------------------------------
$planned = ($s.VMs.Values | Measure-Object -Property MemoryGB -Sum).Sum
if ($planned -gt $s.VmBudgetGB) {
    throw "settings.psd1 asks for $planned GB of VM memory, over the $($s.VmBudgetGB) GB budget."
}
# The control plane first: the workers report to it.
$order = $s.VMs.Keys | Sort-Object { if ($s.VMs[$_].Role -eq 'control-plane') { 0 } else { 1 } }, { $_ }
foreach ($name in $order) {
    $spec = $s.VMs[$name]
    if (Get-VM -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "$name exists; left as it is."
        continue
    }
    $headroom = Get-CommitHeadroomGB
    $after = $headroom - $spec.MemoryGB
    if ($after -lt $s.CommitReserveGB) {
        throw ("Not creating ${name}: {0} GB of commit free, {1} GB after its {2} GB, " +
               "under the {3} GB reserve.") -f $headroom, $after, $spec.MemoryGB, $s.CommitReserveGB
    }
    $seed = Join-Path $s.Root "seed\$name.iso"
    if (-not (Test-Path $seed)) { throw "No seed image for $name at $seed." }
    $dir = Join-Path $s.Root "vms\$name"
    $disk = Join-Path $dir "$name.vhdx"
    if (-not $PSCmdlet.ShouldProcess($name, ("create: {0} GB static, {1} vCPU, {2} GB disk, {3}" -f
            $spec.MemoryGB, $spec.Cpus, $spec.DiskGB, $spec.Address))) { continue }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    if (-not (Test-Path $disk)) {
        Convert-VHD -Path $base -DestinationPath $disk -VHDType Dynamic
        Resize-VHD -Path $disk -SizeBytes ([uint64]$spec.DiskGB * 1GB)
    }
    New-VM -Name $name -Generation 2 -Path (Join-Path $s.Root 'vms') -VHDPath $disk `
        -MemoryStartupBytes ([int64]$spec.MemoryGB * 1GB) -SwitchName $s.Switch | Out-Null
    Set-VMMemory -VMName $name -DynamicMemoryEnabled $false
    Set-VMProcessor -VMName $name -Count $spec.Cpus
    Set-VMFirmware -VMName $name -EnableSecureBoot On -SecureBootTemplate 'MicrosoftUEFICertificateAuthority'
    Add-VMDvdDrive -VMName $name -Path $seed
    Set-VMFirmware -VMName $name -FirstBootDevice (Get-VMHardDiskDrive -VMName $name)
    # Back after a host restart; shut down cleanly with the host, never saved
    # (a saved VM keeps a file the size of its memory on disk).
    Set-VM -Name $name -AutomaticStartAction Start -AutomaticStartDelay 30 `
        -AutomaticStopAction ShutDown -Notes "runner platform: $($spec.Role), $($spec.Address)"
    Start-VM -Name $name
    Write-Host ("{0} created and started: {1} GB static, {2} vCPU, {3}; commit free now {4} GB" -f
        $name, $spec.MemoryGB, $spec.Cpus, $spec.Address, (Get-CommitHeadroomGB))
}

Write-Host ""
Write-Host "Done. Next, not elevated: $PSScriptRoot\Initialize-RunnerPlatform.ps1"
