# Owned fixed VHDX volumes. This file is deployed with the agent, never inside
# a runner's writable tree. All requests arrive as JSON stdin, never commands.
# Images are created with diskpart, read and detached with the Storage
# module's disk-image cmdlets, and attached through virtdisk.dll's own
# AttachVirtualDisk (P/Invoke, below) rather than that module's mount
# cmdlet - the only one of these calls that takes a security descriptor, so
# it is the only one that can grant a runner's own account access instead of
# binding the disk to whichever account attached it. Never the Hyper-V
# module's virtual-disk cmdlets either: this script also runs inside a
# Hyper-V guest, where that module does not exist (2026-09-23). No fallback
# to either alternative - one path, working on both hosts.
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

function Get-RunnerServiceSid([string]$Rid) {
    # The SID Windows would derive for this runner's own service account,
    # `NT SERVICE\rnr-<rid>` - the same derivation as
    # agent/runtimes/windows_process.py's service_sid: SHA-1 of the
    # upper-cased name in UTF-16LE, taken as five little-endian 32-bit words
    # after S-1-5-80. Independently computable before the service exists,
    # which is the whole point of that Python helper (icacls answers "No
    # mapping between account names and security IDs was done" for a
    # service that is not there yet) - and it is plain hashing, nothing
    # privileged, so it runs for real whether or not Add-VirtualDisk below
    # is mocked.
    $name = ("rnr-$Rid").ToUpperInvariant()
    $sha1 = [Security.Cryptography.SHA1]::Create()
    try {
        $digest = $sha1.ComputeHash([Text.Encoding]::Unicode.GetBytes($name))
    } finally {
        $sha1.Dispose()
    }
    $words = 0..4 | ForEach-Object { [BitConverter]::ToUInt32($digest, $_ * 4) }
    return 'S-1-5-80-' + ($words -join '-')
}

function Get-DiskSecurityDescriptor([string]$Rid, [bool]$GrantRunner) {
    # SYSTEM and Administrators always get full access. This runner's own
    # service SID - never the well-known "every service" SID (SU), which
    # would let any service on the machine open any runner's disk - gets
    # full access too, and only for the writable attach `ensure`/`mount`
    # use. The read-only attach `remove` uses purely to prove identity
    # grants neither: nothing needs to read a disk that is about to be
    # deleted, least of all every service on the machine.
    #
    # Full access, not read/write/execute: this descriptor gates the device,
    # and GENERIC_WRITE there does not carry DELETE. With only GRGWGX the
    # runner could create and write files on its own disk but never rename
    # or delete one - which is how the job host's telemetry first went
    # missing in the guest: it wrote telemetry.json.tmp every beat and could
    # never move it into place (2026-09-24). What the runner may reach is
    # decided by the file system's own ACLs, which the runtime sets; the
    # device descriptor only says who may open the volume at all.
    $sddl = 'O:BAG:SYD:(A;;GA;;;SY)(A;;GA;;;BA)'
    if ($GrantRunner) { $sddl += "(A;;GA;;;$(Get-RunnerServiceSid $Rid))" }
    return $sddl
}

if (-not (Test-Path function:Add-VirtualDisk)) {
    # Attach through AttachVirtualDisk, which takes a security descriptor -
    # the Storage module's own mount cmdlet does not expose one, and binds
    # whatever it attaches to the account that called it (this agent,
    # LocalSystem), so Windows 11 refuses the runner's own account at the
    # device itself, before any file ACL is even consulted (proven live in a
    # Hyper-V guest, 2026-09-23). Windows 10 (beast-unit) never enforced
    # that, so the old attach was harmless there, but this is the only
    # attach path now for both hosts - no fallback to the old one anywhere.
    # Guarded so a test double can replace Add-VirtualDisk before this file
    # is dot-sourced, the same seam every other privileged cmdlet here uses.
    Add-Type -Namespace Rnr -Name VirtDisk -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct VIRTUAL_STORAGE_TYPE { public uint DeviceId; public Guid VendorId; }

[DllImport("virtdisk.dll", CharSet = CharSet.Unicode, SetLastError = false)]
public static extern int OpenVirtualDisk(
    ref VIRTUAL_STORAGE_TYPE VirtualStorageType, string Path, uint VirtualDiskAccessMask,
    uint Flags, IntPtr Parameters, out IntPtr Handle);

[DllImport("virtdisk.dll", SetLastError = false)]
public static extern int AttachVirtualDisk(
    IntPtr VirtualDiskHandle, IntPtr SecurityDescriptor, uint Flags,
    uint ProviderSpecificFlags, IntPtr Parameters, IntPtr Overlapped);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool CloseHandle(IntPtr h);
'@

    function Add-VirtualDisk([string]$Path, [string]$Sddl, [switch]$ReadOnly) {
        $type = New-Object Rnr.VirtDisk+VIRTUAL_STORAGE_TYPE
        $type.DeviceId = 3   # VHDX
        $type.VendorId = [Guid]'EC984AEC-A0F9-47e9-901F-71415A66345B'   # Microsoft
        $handle = [IntPtr]::Zero
        # VIRTUAL_DISK_ACCESS_ALL
        $rc = [Rnr.VirtDisk]::OpenVirtualDisk([ref]$type, $Path, 0x003f0000, 0,
            [IntPtr]::Zero, [ref]$handle)
        if ($rc -ne 0) { throw "OpenVirtualDisk failed (code $rc) opening $Path" }
        try {
            $sd = New-Object Security.AccessControl.RawSecurityDescriptor($Sddl)
            $bytes = New-Object byte[] $sd.BinaryLength
            $sd.GetBinaryForm($bytes, 0)
            $mem = [Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
            try {
                [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $mem, $bytes.Length)
                # ATTACH_VIRTUAL_DISK_FLAG: NO_DRIVE_LETTER (0x2) and
                # PERMANENT_LIFETIME (0x4) always - the same combination the
                # guest prototype proved (2026-09-23); READ_ONLY (0x1) added
                # only for the identity-check attach.
                $flags = 0x2 -bor 0x4
                if ($ReadOnly) { $flags = $flags -bor 0x1 }
                $rc = [Rnr.VirtDisk]::AttachVirtualDisk($handle, $mem, $flags, 0,
                    [IntPtr]::Zero, [IntPtr]::Zero)
                if ($rc -ne 0) { throw "AttachVirtualDisk failed (code $rc) attaching $Path" }
            } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($mem) }
        } finally { [void][Rnr.VirtDisk]::CloseHandle($handle) }
    }
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

function New-FixedImage([string]$Path, [int64]$SizeBytes) {
    # diskpart takes its commands from a script file, never inline text on its
    # own command line. Its exit code and its output text are never the
    # decision, only supporting evidence in the message if this fails: both
    # are documented to lie (diskpart can return 0 after printing a failure,
    # and its text is localized) - the only authority is the post-condition,
    # the image now existing at the size that was asked for. Anything else,
    # including a partial file diskpart itself left behind, is removed here
    # rather than handed to the next run to trip over.
    $sizeMb = [int64]($SizeBytes / 1MB)
    $scriptPath = Join-Path ([IO.Path]::GetTempPath()) `
        ('nomercy-diskpart-' + [Guid]::NewGuid().ToString('N') + '.txt')
    try {
        [IO.File]::WriteAllText($scriptPath,
            "create vdisk file=`"$Path`" maximum=$sizeMb type=fixed`r`n",
            (New-Object Text.UTF8Encoding($false)))
        $output = (& diskpart /s $scriptPath 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $madeIt = $false
        try {
            $madeIt = [int64](Get-DiskImage -ImagePath $Path).Size -eq $SizeBytes
        } catch {
            $madeIt = $false
        }
        if (-not $madeIt) {
            Remove-Item -LiteralPath $Path -ErrorAction SilentlyContinue
            throw "diskpart create vdisk failed (exit $code): $($output.Trim())"
        }
    } finally {
        Remove-Item -LiteralPath $scriptPath -ErrorAction SilentlyContinue
    }
}

function Assert-ImageMatchesManifest([string]$FailureMessage) {
    # The size/type half of what the old Hyper-V-based image check used to do
    # in one step. Type is never re-inferred from the live file - diskpart
    # only ever creates fixed disks, and that fact was recorded in the
    # manifest the moment it was asked for; a heuristic like on-disk file
    # size versus virtual size cannot tell a fixed disk from a dynamic one
    # that has simply grown full, so it is not attempted. This is a sanity
    # gate, never an identity check - nothing here may substitute for one.
    $diskImage = Get-DiskImage -ImagePath $script:image
    if ([int64]$diskImage.Size -ne [int64]$script:state.limit -or
        ($script:state.PSObject.Properties['type'] -and $script:state.type -ne 'Fixed')) {
        throw $FailureMessage
    }
    return $diskImage
}

function Get-OwnedDisk {
    Assert-PlainAncestors $script:image
    $diskImage = Assert-ImageMatchesManifest 'Backing image size/type differs from its manifest'
    if (-not $diskImage.Attached) { throw 'Backing image is not attached' }
    # Number is only a lookup obtained from this exact image; identity below
    # must match before any mutation is allowed.
    $disk = Get-Disk -Number $diskImage.Number
    if ($script:state.disk_id -and [string]$disk.UniqueId -ne $script:state.disk_id) {
        throw 'Attached disk identity differs from its manifest'
    }
    return $disk
}

function Confirm-OwnedImage {
    # `remove` must prove the file at $script:image is still the disk this
    # manifest owns by the disk's own identity, never by size alone - a disk
    # is routinely unattached here (every host reboot leaves it that way
    # until something remounts it, which `remove` is explicitly written to
    # tolerate), and a same-sized unrelated image must never pass just
    # because nothing attached is compared. Attaches read-only only if not
    # already attached, purely to read that identity, and always leaves
    # attach state exactly as found - dismounting again on the way out
    # whatever happens, match or refusal alike - so a caller that only needs
    # to prove identity never leaves an image attached behind it. An image
    # that cannot be attached at all is refused and named as such, never
    # treated as good enough to delete on the grounds that it looked about
    # right.
    Assert-PlainAncestors $script:image
    $diskImage = Assert-ImageMatchesManifest 'Refusing to remove a replaced image'
    $selfAttached = -not $diskImage.Attached
    if ($selfAttached) {
        try {
            Add-VirtualDisk $script:image (Get-DiskSecurityDescriptor $script:rid $false) -ReadOnly
        } catch {
            # AttachVirtualDisk can succeed and still have this call throw
            # afterward - a real disk is a raw kernel attach, not the more
            # defensive higher-level cmdlet it replaced. Whatever this call
            # may have surfaced is not this caller's to leave behind.
            try { Dismount-DiskImage -ImagePath $script:image } catch {}
            throw "Refusing to remove an image that cannot be attached for identity verification: $($_.Exception.Message)"
        }
    }
    try {
        $diskImage = Get-DiskImage -ImagePath $script:image
        $disk = Get-Disk -Number $diskImage.Number
        if ($script:state.disk_id -and [string]$disk.UniqueId -ne $script:state.disk_id) {
            throw 'Refusing to remove a replaced image'
        }
        return $disk
    } finally {
        if ($selfAttached) { Dismount-DiskImage -ImagePath $script:image }
    }
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
    # A verify waits a few seconds for another short check - the heartbeat
    # verifies every runner's disk, and with two runners one second lost the
    # race often enough to fail a registration (2026-09-22) - but still gives
    # up well before a disk being created, which holds the lock for minutes.
    $waitMilliseconds = if ($request.action -eq 'verify') { 8000 } else { 3600000 }
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
            limit=[int64]$request.limit; type='Fixed'; stage='planned'; disk_id=''; partition_id=''; volume_guid=''; volume_acl=$false}
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
            New-FixedImage -Path $image -SizeBytes $state.limit
        }
        Assert-PlainAncestors $image
        $diskImage = Assert-ImageMatchesManifest 'Unexpected backing image size/type'
        # A disk this call attaches itself - the routine post-reboot state,
        # where every runner's disk starts out detached until something
        # remounts it - must not be left attached if it then fails identity:
        # dismount on the way out before the error propagates. A disk that
        # was already attached before this call began is left exactly as
        # found on a refusal; it was not this call's to attach or detach.
        $selfAttached = -not $diskImage.Attached
        if ($selfAttached) {
            try {
                Add-VirtualDisk $image (Get-DiskSecurityDescriptor $rid $true)
            } catch {
                # AttachVirtualDisk can succeed and still have this call
                # throw afterward - a real disk is a raw kernel attach, not
                # the more defensive higher-level cmdlet it replaced.
                # Whatever this call may have surfaced is not this caller's
                # to leave behind.
                try { Dismount-DiskImage -ImagePath $image } catch {}
                throw
            }
        }
        try {
            $disk = Get-OwnedDisk
        } catch {
            if ($selfAttached) { try { Dismount-DiskImage -ImagePath $image } catch {} }
            throw
        }
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
            # Identity is proven regardless of attach state - size/type alone
            # is not an identity check and must never stand in for one.
            $wasAttached = (Get-DiskImage -ImagePath $image).Attached
            $null = Confirm-OwnedImage
            if ($wasAttached) {
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
            $wasAttached = (Get-DiskImage -ImagePath $image).Attached
            $null = Confirm-OwnedImage
            if ($wasAttached) {
                $part = Get-OwnedPartition
                $volume = $part | Get-Volume
                if ([string]$volume.UniqueId -ne $state.volume_guid) { throw 'Refusing to remove a replaced volume' }
                $otherPaths = @($part.AccessPaths | Where-Object { $_ -ne $mount -and $_ -ne $state.volume_guid })
                if ($otherPaths.Count) { throw 'Owned volume has unexpected access paths' }
                if (@($part.AccessPaths) -contains $mount) { $part | Remove-PartitionAccessPath -AccessPath $mount }
                Dismount-DiskImage -ImagePath $image
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
