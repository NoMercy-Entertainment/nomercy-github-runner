# Owned fixed VHDX volumes. This file is deployed with the agent, never inside
# a runner's writable tree. All requests arrive as JSON stdin, never commands.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest

function Assert-PlainAncestors([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse ancestor is forbidden: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Set-PrivateAcl([string]$Path) {
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Protect-Directory([string]$Path) {
    Assert-PlainAncestors $Path
    [IO.Directory]::CreateDirectory($Path) | Out-Null
    Set-PrivateAcl $Path
}

function Protect-RunnerParent([string]$Path) {
    # The directory every runner's volume is mounted under. The GitHub runner
    # refuses to start unless it can list each directory above its own, so
    # service accounts (S-1-5-80-0) may list and traverse this one - with no
    # inheritance: each mounted volume keeps an ACL of its own, and a runner
    # sees another's directory name, never its contents.
    Assert-PlainAncestors $Path
    [IO.Directory]::CreateDirectory($Path) | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $services = New-Object Security.Principal.SecurityIdentifier('S-1-5-80-0')
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $services, 'ListDirectory,ReadAttributes,Traverse,Synchronize', 'None', 'None', 'Allow')))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Save-State {
    $json = $script:state | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($script:manifest + '.tmp', $json,
                          (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $script:manifest) {
        [IO.File]::Replace($script:manifest + '.tmp', $script:manifest, [NullString]::Value)
    } else {
        [IO.File]::Move($script:manifest + '.tmp', $script:manifest)
    }
}

function Get-OwnedDisk {
    Assert-PlainAncestors $script:image
    $vhd = Get-VHD -Path $script:image
    if ($vhd.VhdType -ne 'Fixed' -or [int64]$vhd.Size -ne [int64]$script:state.limit) {
        throw 'Backing image size/type differs from its manifest'
    }
    if ($script:state.vhd_id -and [string]$vhd.DiskIdentifier -ne $script:state.vhd_id) {
        throw 'Backing image identity differs from its manifest'
    }
    if (-not $vhd.Attached) { throw 'Backing image is not attached' }
    # Number is only a lookup obtained from this exact image; identity below
    # must match before any mutation is allowed.
    $disk = Get-Disk -Number $vhd.DiskNumber
    if ($script:state.disk_id -and [string]$disk.UniqueId -ne $script:state.disk_id) {
        throw 'Attached disk identity differs from its manifest'
    }
    return $disk
}

function Get-OwnedPartition {
    $disk = Get-OwnedDisk
    $parts = @(Get-Partition -DiskNumber $disk.Number | Where-Object {
        [string]$_.Guid -eq $script:state.partition_id
    })
    if ($parts.Count -ne 1) { throw 'Owned partition identity is missing or ambiguous' }
    return $parts[0]
}

function Assert-Mounted {
    $part = Get-OwnedPartition
    if (@($part.AccessPaths) -notcontains $script:mount) {
        throw 'Owned volume is not mounted at the runner directory'
    }
    $volume = $part | Get-Volume
    if ([string]$volume.UniqueId -ne $script:state.volume_guid -or $volume.FileSystemType -ne 'NTFS') {
        throw 'Mounted volume identity/filesystem differs from its manifest'
    }
    $otherPaths = @($part.AccessPaths | Where-Object { $_ -ne $script:mount -and $_ -ne $script:state.volume_guid })
    if ($otherPaths.Count) { throw 'Owned volume has unexpected access paths' }
    foreach ($area in @('work', 'cache', 'reg', 'logs', 'tmp')) {
        $areaPath = Join-Path $script:mount $area
        if (Test-Path -LiteralPath $areaPath) {
            $item = Get-Item -LiteralPath $areaPath -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Runner area must not redirect filesystem operations: $area"
            }
        }
    }
    return $volume
}

$mutex = $null
$acquired = $false
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.action -notin @('ensure', 'mount', 'verify', 'remove')) { throw 'Unknown operation' }
    if ([string]$request.runner_id -cnotmatch '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') { throw 'Invalid UUID' }
    $rid = [string]$request.runner_id
    $root = [IO.Path]::GetFullPath([string]$request.root).TrimEnd('\')
    $runnerRoot = [IO.Path]::GetFullPath([string]$request.runner_root).TrimEnd('\')
    if ($root -notmatch '^[A-Za-z]:\\.+' -or $runnerRoot -notmatch '^[A-Za-z]:\\.+' -or
        $root -eq $runnerRoot -or $root.StartsWith($runnerRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $runnerRoot.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid storage roots' }
    $script:image = Join-Path $root ($rid + '.vhdx')
    $script:manifest = Join-Path $root ($rid + '.json')
    $script:mount = (Join-Path $runnerRoot $rid) + '\'
    # One lock for every storage mutation on this host. A caller that loads
    # this file into its own scope may name another - the test harness does,
    # so a suite run on a live worker never contends with its agent.
    $mutexName = 'Global\NoMercyRunnerStorage'
    if (Test-Path variable:script:StorageMutexName) { $mutexName = $script:StorageMutexName }
    $mutex = New-Object Threading.Mutex($false, $mutexName)
    $waitMilliseconds = if ($request.action -eq 'verify') { 1000 } else { 3600000 }
    try { $acquired = $mutex.WaitOne($waitMilliseconds) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw 'Storage is locked by another operation' }
    Assert-PlainAncestors $root
    Assert-PlainAncestors $runnerRoot
    Assert-PlainAncestors $script:manifest
    if (Test-Path -LiteralPath $script:manifest) {
        $script:state = Get-Content -LiteralPath $script:manifest -Raw | ConvertFrom-Json
        if ($state.runner_id -ne $rid -or $state.image -ne $image -or $state.mount -ne $mount -or $state.version -ne 1) {
            throw 'Storage manifest identity mismatch'
        }
        if ($request.action -eq 'ensure' -and [int64]$request.limit -ne [int64]$state.limit) {
            throw 'Changing an existing disk limit requires an explicit offline resize'
        }
    } else {
        if ($request.action -eq 'remove' -and -not (Test-Path -LiteralPath $image) -and
            -not (Test-Path -LiteralPath $mount)) {
            @{runner_id=$rid; removed=$true} | ConvertTo-Json -Compress
            exit 0
        }
        if ($request.action -ne 'ensure') { throw 'No owned storage manifest; refusing to adopt a directory/image' }
        if (Test-Path -LiteralPath $image) { throw 'Unowned backing image already exists' }
        if (Test-Path -LiteralPath $mount) { throw 'Existing runner directory requires explicit offline migration' }
        if ([int64]$request.limit -lt 1GB -or [int64]$request.limit % 1MB -ne 0 -or [int64]$request.reserve_bytes -lt 0) {
            throw 'Invalid disk size/reserve'
        }
        Protect-Directory $root
        Protect-RunnerParent $runnerRoot
        $script:state = [pscustomobject]@{version=1; runner_id=$rid; image=$image; mount=$mount;
            limit=[int64]$request.limit; stage='planned'; vhd_id=''; disk_id=''; partition_id=''; volume_guid=''; volume_acl=$false}
        Save-State
    }
    if ($request.action -in @('ensure', 'mount')) {
        Protect-Directory $root
        Protect-RunnerParent $runnerRoot
        if (-not (Test-Path -LiteralPath $image)) {
            if ($state.stage -ne 'planned' -or $request.action -ne 'ensure') { throw 'Owned disk is missing; refusing replacement' }
            $hostVolume = Get-Volume -FilePath ($root + '\')
            if ($hostVolume.FileSystemType -ne 'NTFS' -or
                [int64]$hostVolume.SizeRemaining -lt ([int64]$state.limit + [int64]$request.reserve_bytes + 64MB)) {
                throw 'Insufficient physical NTFS space for fixed disk and host reserve'
            }
            New-VHD -Path $image -SizeBytes $state.limit -Fixed | Out-Null
        }
        Assert-PlainAncestors $image
        $vhd = Get-VHD -Path $image
        if ($vhd.VhdType -ne 'Fixed' -or [int64]$vhd.Size -ne [int64]$state.limit) { throw 'Unexpected backing image size/type' }
        if (-not $state.vhd_id) { $state.vhd_id = [string]$vhd.DiskIdentifier; Save-State }
        if ([string]$vhd.DiskIdentifier -ne $state.vhd_id) { throw 'Backing image was replaced' }
        if (-not $vhd.Attached) { Mount-VHD -Path $image -NoDriveLetter | Out-Null }
        $disk = Get-OwnedDisk
        if (-not $state.disk_id) { $state.disk_id = [string]$disk.UniqueId; Save-State }
        if ($state.stage -eq 'planned') {
            if ($disk.PartitionStyle -eq 'RAW') {
                $disk | Initialize-Disk -PartitionStyle GPT | Out-Null
                $disk = Get-OwnedDisk
            }
            if ($disk.PartitionStyle -ne 'GPT') { throw 'New owned disk has unexpected partition style' }
            $parts = @(Get-Partition -DiskNumber $disk.Number | Where-Object { $_.Type -ne 'Reserved' })
            if ($parts.Count -eq 0) { $part = $disk | New-Partition -UseMaximumSize }
            elseif ($parts.Count -eq 1) { $part = $parts[0] }
            else { throw 'New owned disk has unexpected partitions' }
            $state.partition_id = [string]$part.Guid
            $state.stage = 'partitioned'
            Save-State
        }
        if ($state.stage -eq 'partitioned') {
            $part = Get-OwnedPartition
            $volume = $part | Get-Volume
            $label = 'rnr-' + $rid.Substring(0, 8)
            if ($volume.FileSystemType -in @('Unknown', 'RAW', '')) {
                $volume = $part | Format-Volume -FileSystem NTFS -NewFileSystemLabel $label -Confirm:$false -Force
            }
            if ($volume.FileSystemType -ne 'NTFS' -or $volume.FileSystemLabel -ne $label) { throw 'Unexpected filesystem on new disk' }
            $state.volume_guid = [string]$volume.UniqueId
            $state.stage = 'ready'
            Save-State
        }
        if ($state.stage -ne 'ready') { throw 'Unknown storage stage' }
        $part = Get-OwnedPartition
        $otherPaths = @($part.AccessPaths | Where-Object { $_ -ne $mount -and $_ -ne $state.volume_guid })
        if ($otherPaths.Count) { throw 'Owned volume has unexpected access paths' }
        if (@($part.AccessPaths) -notcontains $mount) {
            Assert-PlainAncestors $mount
            if (Test-Path -LiteralPath $mount) {
                if (@(Get-ChildItem -LiteralPath $mount -Force).Count) { throw 'Mount directory is not empty; refusing adoption' }
            } else { [IO.Directory]::CreateDirectory($mount) | Out-Null }
            $volume = $part | Get-Volume
            if ([string]$volume.UniqueId -ne $state.volume_guid) { throw 'Volume identity changed before mount' }
            $part | Add-PartitionAccessPath -AccessPath $mount
        }
        if (-not $state.volume_acl) {
            # A newly formatted NTFS root can have explicit broad defaults.
            # Replace its entire ACL, not just inherited entries. The runtime
            # grants the own service SID only after that service exists.
            $null = Assert-Mounted
            Set-PrivateAcl $mount
            $state.volume_acl = $true
            Save-State
        }
    }
    if ($request.action -eq 'remove') {
        if ($state.stage -eq 'planned' -and -not (Test-Path -LiteralPath $image) -and
            -not (Test-Path -LiteralPath $mount)) {
            # A preflight allocation failure reserved only metadata, no data.
            Remove-Item -LiteralPath $manifest
            @{runner_id=$rid; removed=$true} | ConvertTo-Json -Compress
            exit 0
        }
        if ($state.stage -notin @('ready', 'deleting')) { throw 'Incomplete disk creation retained for recovery' }
        if ($state.stage -eq 'ready') {
            Assert-PlainAncestors $image
            $vhd = Get-VHD -Path $image
            if ([string]$vhd.DiskIdentifier -ne $state.vhd_id -or $vhd.VhdType -ne 'Fixed' -or
                [int64]$vhd.Size -ne [int64]$state.limit) { throw 'Refusing to remove a replaced image' }
            if ($vhd.Attached) {
                $part = Get-OwnedPartition
                $volume = $part | Get-Volume
                if ([string]$volume.UniqueId -ne $state.volume_guid) { throw 'Refusing to remove a replaced volume' }
                $otherPaths = @($part.AccessPaths | Where-Object { $_ -ne $mount -and $_ -ne $state.volume_guid })
                if ($otherPaths.Count) { throw 'Owned volume has unexpected access paths' }
            }
            $state.stage = 'deleting'
            Save-State
        }
        if (Test-Path -LiteralPath $image) {
            $vhd = Get-VHD -Path $image
            if ([string]$vhd.DiskIdentifier -ne $state.vhd_id) { throw 'Refusing to remove a replaced image' }
            if ($vhd.Attached) {
                $part = Get-OwnedPartition
                $volume = $part | Get-Volume
                if ([string]$volume.UniqueId -ne $state.volume_guid) { throw 'Refusing to remove a replaced volume' }
                $otherPaths = @($part.AccessPaths | Where-Object { $_ -ne $mount -and $_ -ne $state.volume_guid })
                if ($otherPaths.Count) { throw 'Owned volume has unexpected access paths' }
                if (@($part.AccessPaths) -contains $mount) { $part | Remove-PartitionAccessPath -AccessPath $mount }
                Dismount-VHD -Path $image
            }
            Remove-Item -LiteralPath $image
        }
        Assert-PlainAncestors $mount
        if (Test-Path -LiteralPath $mount) { [IO.Directory]::Delete($mount, $false) }
        Remove-Item -LiteralPath $manifest
        @{runner_id=$rid; removed=$true} | ConvertTo-Json -Compress
    } else {
        if ($state.stage -ne 'ready' -or -not $state.volume_acl) { throw 'Storage is not ready' }
        $volume = Assert-Mounted
        @{runner_id=$rid; volume_guid=$state.volume_guid; virtual_bytes=[int64]$state.limit;
          capacity_bytes=[int64]$volume.Size; free_bytes=[int64]$volume.SizeRemaining} | ConvertTo-Json -Compress
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
