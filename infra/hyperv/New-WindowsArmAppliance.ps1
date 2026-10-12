<# Creates only the dedicated Windows ARM64 QEMU host. Seed must be prepared first.
   Does not stop or change existing VMs, switches, runners or the controller. #>
[CmdletBinding(SupportsShouldProcess)]
param()
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
$spec = $s.WindowsArm.Appliance
$name = $spec.Name
if (-not (Test-Elevated)) { throw 'Hyper-V requires an administrator session.' }
if (Get-VM -Name $name -ErrorAction SilentlyContinue) { throw "$name already exists; refusing to modify it." }
foreach ($switchName in @($s.Switch, $s.UplinkSwitch)) {
    if (-not (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue)) { throw "Missing switch $switchName" }
}
if ((Get-CommitHeadroomGB) - $spec.MemoryGB -lt $s.CommitReserveGB) { throw 'Insufficient host commit reserve.' }
$plannedGB = ($s.VMs.Values | Measure-Object -Property MemoryGB -Sum).Sum + $spec.MemoryGB
if ($plannedGB -gt $s.VmBudgetGB) { throw 'Dedicated appliance exceeds the platform memory budget.' }
$base = Join-Path $s.Root 'base\noble-server-cloudimg-amd64.vhdx'
$seed = Join-Path $s.Root "seed\$name.iso"
foreach ($file in @($base, $seed)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing $file" }
}
$dir = Join-Path $s.Root "vms\$name"
if (Test-Path -LiteralPath $dir) { throw "Target directory exists: $dir" }
if (-not $PSCmdlet.ShouldProcess($name, "create dedicated $($spec.MemoryGB) GiB / $($spec.Cpus) vCPU ARM64 appliance host")) { return }
New-Item -ItemType Directory -Path $dir | Out-Null
$osDisk = Join-Path $dir "$name.vhdx"
$dataDisk = Join-Path $dir "$name-data.vhdx"
Convert-VHD -Path $base -DestinationPath $osDisk -VHDType Dynamic
Resize-VHD -Path $osDisk -SizeBytes ([uint64]$spec.DiskGB * 1GB)
New-VHD -Path $dataDisk -Dynamic -SizeBytes ([uint64]$spec.DataDiskGB * 1GB) | Out-Null
New-VM -Name $name -Generation 2 -Path (Join-Path $s.Root 'vms') -VHDPath $osDisk -MemoryStartupBytes ([int64]$spec.MemoryGB * 1GB) -SwitchName $s.Switch | Out-Null
Get-VMNetworkAdapter -VMName $name | Rename-VMNetworkAdapter -NewName 'mgmt'
Set-VMNetworkAdapter -VMName $name -Name 'mgmt' -StaticMacAddress $spec.MgmtMac
Add-VMNetworkAdapter -VMName $name -Name 'uplink' -SwitchName $s.UplinkSwitch -StaticMacAddress $spec.UplinkMac
Set-VMMemory -VMName $name -DynamicMemoryEnabled $false
Set-VMProcessor -VMName $name -Count $spec.Cpus
Set-VMFirmware -VMName $name -EnableSecureBoot On -SecureBootTemplate 'MicrosoftUEFICertificateAuthority'
Add-VMHardDiskDrive -VMName $name -Path $dataDisk -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 1
Add-VMDvdDrive -VMName $name -Path $seed
Set-VMFirmware -VMName $name -FirstBootDevice (Get-VMHardDiskDrive -VMName $name | Where-Object Path -eq $osDisk)
Set-VM -Name $name -AutomaticStartAction Start -AutomaticStartDelay 30 -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false -Notes "Dedicated Windows ARM64 QEMU host, $($spec.Address)"
Start-VM -Name $name
Get-VM -Name $name | Select-Object Name, State, ProcessorCount, MemoryAssigned
