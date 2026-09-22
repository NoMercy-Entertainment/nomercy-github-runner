# Runs the actual storage helper with Storage/Hyper-V cmdlets replaced.
# All ordinary filesystem IO stays in the pytest temporary directory.
$script:modelPath = $env:RNR_STORAGE_MODEL
$script:model = Get-Content -LiteralPath $script:modelPath -Raw | ConvertFrom-Json
function Save-Model([string]$Step) {
    $script:model.calls += $Step
    $script:model | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:modelPath
    if ($env:RNR_STORAGE_FAIL -eq $Step) { throw "injected crash after $Step" }
}
function Set-Acl {
    param($LiteralPath, $AclObject)
    # What each path was given, so a test can read the access rules back.
    $key = $LiteralPath.TrimEnd('\')
    if (-not $script:model.PSObject.Properties['acls']) {
        $script:model | Add-Member -NotePropertyName acls -NotePropertyValue ([pscustomobject]@{})
    }
    $script:model.acls | Add-Member -Force -NotePropertyName $key `
        -NotePropertyValue $AclObject.GetSecurityDescriptorSddlForm('Access')
    $script:model | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:modelPath
    if ($LiteralPath.EndsWith($script:rid + '\')) { Save-Model 'protect-volume' }
}
function Get-Item {
    param($LiteralPath, [switch]$Force)
    if ($script:model.reparse_area -and $LiteralPath.EndsWith('\' + $script:model.reparse_area)) {
        [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint}
    } else {
        Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force
    }
}
function Get-VHD {
    param($Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw 'no image' }
    [pscustomobject]@{VhdType=$script:model.type; Size=$script:model.limit;
        DiskIdentifier=$script:model.vhd_id; Attached=$script:model.attached; DiskNumber=7}
}
function New-VHD {
    param($Path, $SizeBytes, [switch]$Fixed)
    if (-not $Fixed) { throw 'only fixed disks are allowed' }
    [IO.File]::WriteAllText($Path, 'fake image')
    $script:model.limit = $SizeBytes
    Save-Model 'create'
    Get-VHD -Path $Path
}
function Mount-VHD {
    param($Path, [switch]$NoDriveLetter)
    if (-not $NoDriveLetter) { throw 'unexpected drive letter' }
    $script:model.attached = $true
    Save-Model 'attach'
}
function Get-Disk {
    param($Number)
    [pscustomobject]@{Number=7; UniqueId=$script:model.disk_id;
        PartitionStyle=$script:model.style}
}
function Initialize-Disk {
    [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject, $PartitionStyle)
    process { $script:model.style = $PartitionStyle; Save-Model 'initialize' }
}
function Get-Partition {
    param($DiskNumber)
    if ($script:model.partition) {
        [pscustomobject]@{Guid=$script:model.partition_id; Type='Basic';
            AccessPaths=@($script:model.access); DiskNumber=7; PartitionNumber=2}
    }
}
function New-Partition {
    [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject, [switch]$UseMaximumSize)
    process {
        $script:model.partition = $true
        Save-Model 'partition'
        Get-Partition -DiskNumber 7
    }
}
function Get-Volume {
    [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject, $FilePath)
    process {
        if ($FilePath) {
            [pscustomobject]@{FileSystemType='NTFS'; SizeRemaining=$script:model.host_free}
        } else {
            [pscustomobject]@{UniqueId=$script:model.volume_guid; FileSystemType=$script:model.fs;
                FileSystemLabel=$script:model.label; Size=($script:model.limit - 16MB);
                SizeRemaining=($script:model.limit - 32MB)}
        }
    }
}
function Format-Volume {
    [CmdletBinding(SupportsShouldProcess=$true)] param(
        [Parameter(ValueFromPipeline=$true)]$InputObject, $FileSystem, $NewFileSystemLabel, [switch]$Force)
    process {
        $script:model.fs = $FileSystem
        $script:model.label = $NewFileSystemLabel
        Save-Model 'format'
        Get-Volume
    }
}
function Add-PartitionAccessPath {
    [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject, $AccessPath)
    process { $script:model.access += $AccessPath; Save-Model 'mount' }
}
function Remove-PartitionAccessPath {
    [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject, $AccessPath)
    process {
        $script:model.access = @($script:model.access | Where-Object { $_ -ne $AccessPath })
        Save-Model 'unmount'
    }
}
function Dismount-VHD {
    param($Path)
    $script:model.attached = $false
    Save-Model 'detach'
}
$global:LASTEXITCODE = 0
# Not the agent's Global lock: on a live worker the agent holds that one, and
# an unprivileged test cannot even open it.
$script:StorageMutexName = 'Local\NoMercyRunnerStorageTest'
. $env:RNR_STORAGE_HELPER
exit $global:LASTEXITCODE
