<#
.SYNOPSIS
    Puts this repository's HEAD agent code on a Windows guest worker over
    SSH and restarts its agent. No elevation, no re-enrolment.
.DESCRIPTION
    Install-WindowsGuestWorker.ps1 and Install-WindowsArmGuestWorker.ps1 set a
    worker up: Python, NSSM, templates, a fresh certificate. Redeploying the
    agent's code needs none of that, and both need things a plain redeploy
    should not: an elevated session here, or ten minutes of toolchain checks
    under emulation. This does the code swap those installers end with, and
    nothing else:
      1. `git archive HEAD agent`, copied into the guest;
      2. unpacked beside the running code as app.new;
      3. the agent stopped (its runners are services of their own and keep
         running), app kept as app.previous, app.new moved into place, and
         the version in agent.json set to this HEAD;
      4. the agent started, and this waits until the controller reports the
         worker healthy at the new version.
    Going back is the same swap the other way: move app.previous to app and
    start rnr-agent.

    Reaches rnr-windows-1 directly (Enable-WindowsGuestSsh.ps1 opened it) and
    the ARM64 guest through its appliance host, as Install-WindowsArmGuestWorker.ps1 does.
#>
[CmdletBinding()]
param(
    [ValidateSet('rnr-windows-1', 'windows-arm64-1')] [string] $Name = 'rnr-windows-1'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
$repo = $script:RepoRoot
$ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
$scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$key = (Join-Path $s.Root 'ssh\id_ed25519').Replace('\', '/')
$known = (Join-Path $s.Root 'ssh\known_hosts').Replace('\', '/')

$version = (& git -C $repo rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read repository version.' }
if (& git -C $repo status --porcelain -- agent) { throw 'agent/ has uncommitted changes; commit them first.' }

$stage = Join-Path $env:TEMP ("rnr-agent-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$sshConfig = Join-Path $stage 'ssh.conf'
$tar = Join-Path $stage 'agent.tar'
if ($Name -eq 'windows-arm64-1') {
    $hostKey = (Join-Path $env:USERPROFILE '.ssh\macos_runner').Replace('\', '/')
    $lines = @('Host guest', '    HostName 127.0.0.1', '    Port 52222', '    User admin',
               '    ProxyJump rnr-arm-host', "    IdentityFile $key", '    IdentitiesOnly yes',
               '    StrictHostKeyChecking accept-new',
               'Host rnr-arm-host', '    HostName 10.77.0.40', '    HostKeyAlias 172.19.136.46',
               '    User runner', "    IdentityFile $hostKey", '    IdentitiesOnly yes',
               '    StrictHostKeyChecking accept-new')
} else {
    $lines = @('Host guest', "    HostName $($s.VMs[$Name].Address)", '    User admin',
               "    IdentityFile $key", '    IdentitiesOnly yes', "    UserKnownHostsFile $known",
               '    StrictHostKeyChecking accept-new')
}
[IO.File]::WriteAllLines($sshConfig, $lines)

function Invoke-GuestPs([string] $Command) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    $output = & $ssh -F $sshConfig -o BatchMode=yes guest `
        powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$Name command failed: $($output | Out-String)" }
    return $output
}

try {
    & git -C $repo archive --format=tar -o $tar HEAD agent
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
    # -O: the classic protocol. rnr-windows-1's sftp subsystem closes the
    # connection, and nothing here needs sftp.
    & $scp -O -F $sshConfig -o BatchMode=yes $tar 'guest:rnr-agent.tar'
    if ($LASTEXITCODE -ne 0) { throw "Copying the agent into $Name failed." }

    $swap = @"
`$ErrorActionPreference = 'Stop'
`$root = '$($s.Windows.Root)'
`$agentDir = Join-Path `$root 'agent'
`$app = Join-Path `$agentDir 'app'
`$fresh = Join-Path `$agentDir 'app.new'
`$previous = Join-Path `$agentDir 'app.previous'
`$nssm = Join-Path `$root 'bin\nssm.exe'
`$tar = Join-Path `$env:USERPROFILE 'rnr-agent.tar'
if (Test-Path `$fresh) { Remove-Item -Recurse -Force `$fresh }
New-Item -ItemType Directory -Path `$fresh | Out-Null
& tar.exe -xf `$tar -C `$fresh
if (`$LASTEXITCODE -ne 0) { throw 'extracting the agent failed' }
Remove-Item -LiteralPath `$tar -Force
if (-not (Test-Path (Join-Path `$fresh 'agent\__main__.py'))) { throw 'the archive holds no agent package' }
& `$nssm stop rnr-agent | Out-Null
if (Test-Path `$previous) { Remove-Item -Recurse -Force `$previous }
Move-Item -LiteralPath `$app -Destination `$previous
Move-Item -LiteralPath `$fresh -Destination `$app
`$configPath = Join-Path `$agentDir 'agent.json'
`$text = [IO.File]::ReadAllText(`$configPath)
`$text = [regex]::Replace(`$text, '"version"\s*:\s*"[^"]*"', '"version": "$version"')
[void](`$text | ConvertFrom-Json)
[IO.File]::WriteAllText(`$configPath, `$text, [Text.UTF8Encoding]::new(`$false))
& `$nssm start rnr-agent | Out-Null
Start-Sleep -Seconds 5
"rnr-agent: " + (Get-Service rnr-agent).Status
Get-Content -LiteralPath (Join-Path `$agentDir 'agent.log') -Tail 3 -ErrorAction SilentlyContinue
"@
    Invoke-GuestPs $swap | Write-Host

    $cp = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })[0]
    $deadline = (Get-Date).AddSeconds(180)
    do {
        Start-Sleep -Seconds 10
        $status = Invoke-Guest $s $s.VMs[$cp].Address 'sudo docker exec rnr-controller python -m control status' | Out-String
        $line = ($status -split "`n" | Where-Object { $_ -match "^\s+$Name\s" }) -join ''
    } until ($line -match 'healthy' -or (Get-Date) -gt $deadline)
    if ($line -notmatch 'healthy') { throw "$Name did not report healthy within 180 seconds: $line" }
    Write-Host "$Name is healthy at $version."
} finally {
    Remove-Item -Recurse -Force -LiteralPath $stage -ErrorAction SilentlyContinue
}
