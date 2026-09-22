# Fixed worker-owned key location; no caller-supplied paths or commands.
param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')][string]$RunnerId,
      [ValidateSet('ensure', 'remove')][string]$Action = 'ensure')
$ErrorActionPreference = 'Stop'
$root = 'C:\ProgramData\nomercy\runner-keys'
$path = Join-Path $root ($RunnerId + '.key')
$temp = $path + '.tmp'
try {
    $current = $root
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Registration key path has a reparse ancestor'
            }
        }
        $current = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
    }
    if ($Action -eq 'remove') {
        foreach ($ownedPath in @($path, $temp)) {
            if (Test-Path -LiteralPath $ownedPath) {
                if ((Get-Item -LiteralPath $ownedPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw 'Registration key must not be a reparse point'
                }
                Remove-Item -LiteralPath $ownedPath
            }
        }
        @{runner_id=$RunnerId; removed=$true} | ConvertTo-Json -Compress
        exit 0
    }
    [IO.Directory]::CreateDirectory($root) | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetSecurityDescriptorSddlForm('D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    Set-Acl -LiteralPath $root -AclObject $acl
    $account = New-Object Security.Principal.NTAccount('NT SERVICE\rnr-' + $RunnerId)
    $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetSecurityDescriptorSddlForm('D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;' + $sid + ')')
    $fileAcl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    if (Test-Path -LiteralPath $path) {
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Registration key must not be a reparse point'
        }
        $ownerSid = (Get-Acl -LiteralPath $path).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($ownerSid -notin @('S-1-5-18', 'S-1-5-32-544')) { throw 'Registration key ownership is untrusted' }
        if ([IO.File]::ReadAllBytes($path).Length -ne 32) { throw 'Registration key has invalid length' }
    } else {
        if (Test-Path -LiteralPath $temp) {
            if ((Get-Item -LiteralPath $temp -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Registration key staging file must not be a reparse point'
            }
            $tempOwner = (Get-Acl -LiteralPath $temp).GetOwner([Security.Principal.SecurityIdentifier]).Value
            if ($tempOwner -notin @('S-1-5-18', 'S-1-5-32-544')) { throw 'Registration key staging ownership is untrusted' }
        }
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $stream = [IO.File]::Open($temp, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        Set-Acl -LiteralPath $temp -AclObject $fileAcl
        [IO.File]::Move($temp, $path)
    }
    Set-Acl -LiteralPath $path -AclObject $fileAcl
    @{runner_id=$RunnerId; key_file=$path} | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
