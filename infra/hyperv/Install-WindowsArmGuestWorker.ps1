<#
.SYNOPSIS
    Enrol the installed Windows ARM64 QEMU guest as a Windows worker.

.DESCRIPTION
    Run after Windows Setup and images/windows/arm64/Prepare-WindowsArm.ps1
    finish. The QEMU guest must have SSH forwarded on the appliance host's
    localhost:52222 and the agent port forwarded on 10.77.0.40:8445.
    The guest payload ISO provides the checked Forgejo binary and NSSM.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
$arm = $s.WindowsArm
$repo = $script:RepoRoot
$ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
$scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$hostKey = Join-Path $env:USERPROFILE '.ssh\macos_runner'
$guestKey = Join-Path $s.Root 'ssh\id_ed25519'
foreach ($path in @($ssh, $scp, $hostKey, $guestKey)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$control = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })
if ($control.Count -ne 1) { throw 'Expected one control-plane VM.' }
$cp = $s.VMs[$control[0]].Address
$stage = Join-Path $env:TEMP ("rnr-arm-worker-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$sshConfig = Join-Path $stage 'ssh.conf'
$codeTar = Join-Path $stage 'code.tar'
$bundleTar = Join-Path $stage 'bundle.tar'
$version = (& git -C $repo rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read repository version.' }
if (& git -C $repo status --porcelain -- agent infra/hyperv infra/windows/templates) {
    $version += '-workingtree'
}

$configText = @"
Host rnr-arm-host
    HostName 10.77.0.40
    HostKeyAlias 172.19.136.46
    User runner
    IdentityFile $($hostKey.Replace('\', '/'))
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
Host rnr-arm-guest
    HostName 127.0.0.1
    Port 52222
    User admin
    ProxyJump rnr-arm-host
    IdentityFile $($guestKey.Replace('\', '/'))
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
"@
[IO.File]::WriteAllText($sshConfig, $configText, [Text.UTF8Encoding]::new($false))

function Invoke-ArmGuest([string] $Command) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    $output = & $ssh -F $sshConfig -o BatchMode=yes rnr-arm-guest `
        powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ARM guest command failed: $($output | Out-String)" }
    return $output
}

function Send-ArmGuest([string] $Source, [string] $Destination) {
    & $scp -F $sshConfig -o BatchMode=yes $Source "rnr-arm-guest:$Destination"
    if ($LASTEXITCODE -ne 0) { throw "Copying $Source into the ARM guest failed." }
}

try {
    $architecture = (Invoke-ArmGuest '$env:PROCESSOR_ARCHITECTURE' | Out-String).Trim()
    if ($architecture -ne 'ARM64') { throw "Guest reports $architecture, expected ARM64." }

    & tar.exe -C $repo -cf $codeTar agent `
        infra/hyperv/lib.ps1 infra/hyperv/settings.psd1 `
        infra/hyperv/Install-WindowsWorker.ps1 `
        infra/windows/guest/Install-RunnerTools.ps1 `
        infra/windows/guest/Install-CppBuildTools.ps1 `
        infra/windows/guest/Install-AndroidSdk.ps1 `
        infra/windows/templates/actions-runner-v2.338.0-windows-arm64 `
        infra/windows/templates/forgejo-runner-v13.1.0-windows-arm64
    if ($LASTEXITCODE -ne 0) { throw 'Could not pack the ARM worker source.' }
    Send-ArmGuest $codeTar 'code.tar'

    $sourceRoot = 'C:\ProgramData\nomercy\src'
    $stageCode = @"
`$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path '$sourceRoot' | Out-Null
& tar.exe -xf (Join-Path `$env:USERPROFILE 'code.tar') -C '$sourceRoot'
if (`$LASTEXITCODE -ne 0) { throw 'Extracting ARM worker source failed.' }
Remove-Item -LiteralPath (Join-Path `$env:USERPROFILE 'code.tar') -Force
[IO.File]::WriteAllText((Join-Path '$sourceRoot' 'VERSION'), '$version')
`$payload = Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    Join-Path `$_.Root 'forgejo-runner-v13.1.0-windows-arm64.exe'
} | Where-Object { Test-Path -LiteralPath `$_ } | Select-Object -First 1
if (-not `$payload) { throw 'The ARM runner payload ISO is not attached.' }
Copy-Item -LiteralPath `$payload -Destination (Join-Path '$sourceRoot' 'forgejo-runner.exe')
Copy-Item -LiteralPath (Join-Path (Split-Path `$payload) 'nssm.exe') -Destination (Join-Path '$sourceRoot' 'nssm.exe')
"@
    Invoke-ArmGuest $stageCode | Out-Null

    # The GitHub and Forgejo listeners will share this guest. Finish its
    # machine-wide build environment before either can accept a job.
    Invoke-ArmGuest "& '$sourceRoot\infra\windows\guest\Install-RunnerTools.ps1' -Architecture arm64" | Write-Host

    $endpoint = $arm.Endpoint
    Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $($arm.HostId) hyperv-windows $endpoint" +
        " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$($arm.HostId) /tmp/bundle" +
        " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar" +
        " && sudo rm -rf /tmp/bundle") -Quiet
    Receive-FromGuest $s $cp '/tmp/bundle.tar' $bundleTar
    Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet
    Send-ArmGuest $bundleTar 'bundle.tar'

    $install = @"
`$ErrorActionPreference = 'Stop'
`$sourceRoot = '$sourceRoot'
`$bundle = Join-Path `$env:USERPROFILE 'bundle.tar'
& (Join-Path `$sourceRoot 'infra\hyperv\Install-WindowsWorker.ps1') `
    -HostId '$($arm.HostId)' -ListenAddress '$($arm.GuestAddress)' `
    -TlsBundle `$bundle -NssmSource (Join-Path `$sourceRoot 'nssm.exe') `
    -RunnerBinary (Join-Path `$sourceRoot 'forgejo-runner.exe') `
    -RunnerTemplate '$($arm.ForgejoTemplate)' -RunnerSha256 '$($arm.ForgejoSha256)' `
    -Architecture arm64 -FirewallRemoteAddress @('10.0.2.2', '$cp') `
    -MaxRunners $($arm.MaxRunners) -RunnerMemGB $($arm.RunnerMemGB)
"@
    Invoke-ArmGuest $install | Write-Host

    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 5
        $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
    } until ($status -match "(?m)^\s+$($arm.HostId)\s+\S+\s+healthy" -or (Get-Date) -gt $deadline)
    if ($status -notmatch "(?m)^\s+$($arm.HostId)\s+\S+\s+healthy") {
        throw "$($arm.HostId) did not report healthy within 120 seconds."
    }
    Write-Host $status
} finally {
    foreach ($path in @($bundleTar, $codeTar, $sshConfig)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
}
