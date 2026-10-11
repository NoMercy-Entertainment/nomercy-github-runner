<# Prepare the empty ARM data disk before any production guest outage.
   Run elevated on the Hyper-V host. No production disk is attached or changed. #>
[CmdletBinding()]
param([string]$StageRoot = 'D:\HyperV\runner-platform\stage\resource-uniformity-20261009')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$stage = [IO.Path]::GetFullPath($StageRoot).TrimEnd('\')
if ($stage -ne 'D:\HyperV\runner-platform\stage\resource-uniformity-20261009') {
    throw 'Unexpected staging directory'
}
$image = Join-Path $stage 'arm64-uniform-data.vhdx'
$mount = Join-Path $stage 'arm64-data-mount'
$ids = @('246e44e4-c7a7-4a67-ad61-6e5a09ab5a6b', '34746f24-145b-4d1c-9b97-24ea77db1e81')
$helper = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'agent\runtimes\windows_storage.ps1'
if (-not (Test-Path -LiteralPath $helper)) { throw 'Pinned storage helper missing' }
if (-not (Test-Path -LiteralPath $image)) {
    New-VHD -Path $image -SizeBytes 320GB -Dynamic | Out-Null
}
$vhd = Get-VHD -Path $image
if ($vhd.Path -ne $image -or $vhd.Size -ne 320GB -or $vhd.VhdType -ne 'Dynamic') {
    throw 'Unexpected staged disk identity or size'
}
if (-not $vhd.Attached) { Mount-VHD -Path $image -NoDriveLetter | Out-Null }
$disk = Get-Disk -Number (Get-VHD -Path $image).DiskNumber
if ($disk.PartitionStyle -eq 'RAW') {
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT | Out-Null
    $partition = New-Partition -DiskNumber $disk.Number -UseMaximumSize
    Format-Volume -Partition $partition -FileSystem NTFS -NewFileSystemLabel 'RNR-ARM-UNIFORM' -Confirm:$false | Out-Null
} else {
    $partition = Get-Partition -DiskNumber $disk.Number | Where-Object Type -eq 'Basic'
    if (@($partition).Count -ne 1 -or ($partition | Get-Volume).FileSystemLabel -ne 'RNR-ARM-UNIFORM') {
        throw 'Existing staged disk is not the prepared empty ARM data disk'
    }
}
New-Item -ItemType Directory -Path $mount -Force | Out-Null
if (@($partition.AccessPaths) -notcontains ($mount + '\')) {
    Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AccessPath ($mount + '\')
}
# The production helper rejects reparse ancestors, including directory volume
# mounts. Use a free drive letter on this prepared disk instead.
$letter = $partition.DriveLetter
if (-not $letter) {
    $letter = @('R','Q','P','O','N') | Where-Object {
        -not (Get-Volume -DriveLetter $_ -ErrorAction SilentlyContinue)
    } | Select-Object -First 1
    if (-not $letter) { throw 'No unused staging drive letter' }
    Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AccessPath ($letter + ':\')
}
Remove-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AccessPath ($mount + '\')
$root = $letter + ':\runner-disks'
$runners = $letter + ':\runners'
foreach ($rid in $ids) {
    $request = @{action='ensure'; runner_id=$rid; root=$root; runner_root=$runners;
                 reserve_bytes=20GB; limit=100GB} | ConvertTo-Json -Compress
    $answer = $request | & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper
    if ($LASTEXITCODE -ne 0) { throw "Preparing owned disk failed for $rid" }
    $answer | Write-Output
}
foreach ($rid in $ids) {
    Dismount-DiskImage -ImagePath (Join-Path $root ($rid + '.vhdx'))
}
Dismount-VHD -Path $image
Get-VHD -Path $image | Select-Object Path,Size,FileSize,Attached | ConvertTo-Json -Compress
