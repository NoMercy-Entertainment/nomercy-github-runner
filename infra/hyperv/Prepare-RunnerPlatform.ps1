<#
.SYNOPSIS
    Everything the runner-platform VMs are made from, prepared without
    elevation and without changing the host: an SSH key, the verified Ubuntu
    cloud image as a VHDX, and one cloud-init seed image per VM.

.DESCRIPTION
    Idempotent: what is there and correct is kept. The conversions (qemu-img,
    xorriso) run in throwaway containers on the WSL engine, so no tool is
    installed on the host. Nothing here creates a VM, a switch or a NAT; that
    is New-RunnerPlatformVMs.ps1, which needs elevation.

    The seed images carry no secret and no code - an admin account reached by
    this platform's key, a static address, and a few packages. Code and the
    controller's settings go over SSH afterwards (Initialize-RunnerPlatform.ps1).
#>
[CmdletBinding()]
param()
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

foreach ($dir in 'base', 'ssh', 'seed', 'vms') {
    New-Item -ItemType Directory -Force -Path (Join-Path $s.Root $dir) | Out-Null
}

# --- the key -----------------------------------------------------------------
$key = Get-KeyPath $s
if (-not (Test-Path $key)) {
    & $script:SshKeygen -q -t ed25519 -N '' -C "$($s.AdminUser)@runner-platform" -f $key
    if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen failed' }
}
# OpenSSH refuses a private key others can read, and D:\ is readable by Users.
& icacls.exe $key /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
$publicKey = (Get-Content -Raw "$key.pub").Trim()
Write-Host "key: $key"

# --- the cloud image, verified -----------------------------------------------
$img = Join-Path $s.Root 'base\noble-server-cloudimg-amd64.img'
$sums = (Invoke-WebRequest -UseBasicParsing -Uri $s.ImageSums).Content
$line = ($sums -split "`n") | Where-Object { $_ -match '\*?noble-server-cloudimg-amd64\.img$' } | Select-Object -First 1
if (-not $line) { throw "SHA256SUMS lists no noble-server-cloudimg-amd64.img" }
$want = ($line -split '\s+')[0].ToLower()
$have = if (Test-Path $img) { (Get-FileHash -Algorithm SHA256 $img).Hash.ToLower() } else { '' }
if ($have -ne $want) {
    Write-Host "downloading the Ubuntu 24.04 cloud image..."
    Invoke-WebRequest -UseBasicParsing -Uri $s.ImageUrl -OutFile "$img.part"
    $have = (Get-FileHash -Algorithm SHA256 "$img.part").Hash.ToLower()
    if ($have -ne $want) {
        Remove-Item "$img.part"
        throw "the download does not match Canonical's SHA256SUMS ($have, expected $want)"
    }
    Move-Item -Force "$img.part" $img
}
Write-Host "image: verified $want"

# --- as a VHDX -----------------------------------------------------------------
# Written here as a base; New-RunnerPlatformVMs.ps1 makes each VM's disk from
# it with Convert-VHD, which writes a file Hyper-V itself made.
$vhdx = Join-Path $s.Root 'base\noble-server-cloudimg-amd64.vhdx'
if (-not (Test-Path $vhdx) -or (Get-Item $vhdx).LastWriteTime -lt (Get-Item $img).LastWriteTime) {
    Write-Host "converting to VHDX..."
    $base = ConvertTo-WslPath (Join-Path $s.Root 'base')
    Invoke-InDistro $s @('docker', 'run', '--rm', '-v', "${base}:/work", 'alpine:3.20', 'sh', '-c',
        'apk add --no-cache qemu-img >/dev/null && qemu-img convert -O vhdx -o subformat=dynamic /work/noble-server-cloudimg-amd64.img /work/noble-server-cloudimg-amd64.vhdx.part && mv /work/noble-server-cloudimg-amd64.vhdx.part /work/noble-server-cloudimg-amd64.vhdx')
}
Write-Host "vhdx: $vhdx"

# --- one seed image per VM ------------------------------------------------------
$guest = Join-Path $PSScriptRoot 'guest'
foreach ($name in $s.VMs.Keys) {
    $vm = $s.VMs[$name]
    $dir = Join-Path $s.Root "seed\$name"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-LfFile (Join-Path $dir 'user-data') (Expand-Template (Join-Path $guest 'user-data.tmpl') @{
        HOSTNAME = $name; ADMIN = $s.AdminUser; SSH_KEY = $publicKey })
    Write-LfFile (Join-Path $dir 'network-config') (Expand-Template (Join-Path $guest 'network-config.tmpl') @{
        ADDRESS = $vm.Address; PREFIX_LENGTH = $s.PrefixLength; GATEWAY = $s.HostAddress
        DNS = ($s.Dns -join ', ') })
    Write-LfFile (Join-Path $dir 'meta-data') "instance-id: $name-1`nlocal-hostname: $name`n"
    $seed = ConvertTo-WslPath (Join-Path $s.Root 'seed')
    Invoke-InDistro $s @('docker', 'run', '--rm', '-v', "${seed}:/seed", 'alpine:3.20', 'sh', '-c',
        "apk add --no-cache xorriso >/dev/null 2>&1 && xorriso -as mkisofs -quiet -output /seed/$name.iso -volid cidata -joliet -rock /seed/$name/user-data /seed/$name/meta-data /seed/$name/network-config")
    Write-Host "seed: $(Join-Path $s.Root "seed\$name.iso")"
}

Write-Host ""
Write-Host "Prepared. Next, elevated: $PSScriptRoot\New-RunnerPlatformVMs.ps1"
