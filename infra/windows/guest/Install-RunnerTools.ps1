<#
.SYNOPSIS
    Puts the toolchain a Windows runner needs into a guest, with winget.

.DESCRIPTION
    A runner host is not only the agent: a job that cannot find `git` fails at
    checkout, and one that cannot find `node` fails in the first action that
    is written in JavaScript. The physical host grew its toolchain by hand
    over years; a guest starts empty, and this is what makes the two
    comparable - written down, so the next guest gets the same and nobody has
    to remember what was installed at three in the morning.

    Idempotent: a package already present is left alone, and winget is asked
    for the `winget` source only - the `msstore` source needs a Store this
    image does not have.

    Run it elevated, in the guest.

    Not installed here, deliberately:
      - Docker. Windows containers inside a Hyper-V guest need nested
        virtualisation turned on for that guest and a licence story of their
        own; no Windows job of this platform has asked for it yet.
      - Visual Studio build tools. Multi-gigabyte, and nothing has needed
        them; add them here when something does, rather than carrying them
        for everyone.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    # Everything the physical host has that a job might reach for, in the
    # order a build would need them. Each is a winget package id.
    [string[]] $Packages = @(
        'Git.Git',
        'Microsoft.PowerShell',
        'OpenJS.NodeJS.LTS',
        'Python.Python.3.12',
        'Microsoft.DotNet.SDK.8',
        'Kitware.CMake',
        'GitHub.cli',
        'Rustlang.Rustup',
        '7zip.7zip'
    )
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: installing for every user needs an administrator.'
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw ('winget is not on this machine. The install media''s debloat pass ' +
           'removes the Store, and App Installer with it on some images; ' +
           'reinstall App Installer before running this.')
}

$installed = @()
$skipped = @()
$failed = @()

foreach ($id in $Packages) {
    $present = (& winget list --exact --id $id --source winget --accept-source-agreements 2>&1 | Out-String)
    if ($present -match [regex]::Escape($id)) {
        $skipped += $id
        Write-Host ("already there : {0}" -f $id)
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($id, 'winget install')) { continue }
    Write-Host ("installing    : {0}" -f $id)
    $out = (& winget install --exact --id $id --source winget --silent `
                --accept-source-agreements --accept-package-agreements 2>&1 | Out-String)
    if ($LASTEXITCODE -eq 0) {
        $installed += $id
    } else {
        $failed += $id
        Write-Host ("  failed ({0}): {1}" -f $LASTEXITCODE,
                    (($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '))
    }
}

# A fresh install is on PATH for new processes, not for this one; report from
# the machine's own PATH rather than this session's stale copy.
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
               [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = $machinePath

Write-Host ''
Write-Host ('installed: {0}' -f (($installed -join ', '), '(none)')[[int]($installed.Count -eq 0)])
Write-Host ('already there: {0}' -f (($skipped -join ', '), '(none)')[[int]($skipped.Count -eq 0)])
if ($failed.Count) { Write-Host ('failed: {0}' -f ($failed -join ', ')) }

Write-Host ''
Write-Host 'what a job would find now:'
foreach ($tool in 'git', 'pwsh', 'node', 'npm', 'python', 'dotnet', 'cmake', 'gh', 'cargo', '7z') {
    $command = Get-Command $tool -ErrorAction SilentlyContinue
    Write-Host ('  {0,-8} {1}' -f $tool, $(if ($command) { $command.Source } else { 'absent' }))
}

if ($failed.Count) {
    throw ('These packages did not install: {0}. Read the lines above for each one.' -f ($failed -join ', '))
}
Write-Host ''
Write-Host 'Runners pick up a new PATH when their service restarts; recreate them, or restart the guest.'
