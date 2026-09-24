<#
.SYNOPSIS
    Puts the toolchain a Windows runner needs into a guest, and proves the
    runner can actually reach it.

.DESCRIPTION
    A runner host is not only the agent: a job that cannot find `git` fails at
    checkout, and one that cannot find `node` fails in the first action that
    is written in JavaScript. The physical host grew its toolchain by hand
    over years; a guest starts empty, and this is what makes the two
    comparable - written down, so the next guest gets the same and nobody has
    to remember what was installed at three in the morning.

    **Installed is not the same as reachable.** winget's default is a per-user
    install, and a runner works under its own virtual service account, which
    has no access to another account's profile and no user PATH of its own.
    The first attempt here put PowerShell 7, Python and rustup into the
    administrator's profile: `where pwsh` answered happily in an
    administrator's shell, every job still reported them absent, and the
    health check failed on a runner that looked perfectly equipped
    (2026-09-24).

    So this script decides what to do by asking the only question that
    matters - can a service resolve this command from the machine's PATH? -
    and not by asking winget what it has installed. Where a package has no
    machine-wide installer (rustup), it is given a machine-wide home instead.

    Idempotent, and safe to run again after adding a package: anything
    already reachable is left alone.

    Two packages resist a machine-wide winget install on this image, and the
    script says so rather than pretending: PowerShell 7 arrives as the
    per-user Store flavour, and Python refuses machine scope while a per-user
    copy of the same version exists. Both were installed by hand from the
    vendor's own signed installer (`Get-AuthenticodeSignature` checked before
    running it) into Program Files; if this script reports them unreachable
    on a fresh guest, that is the fix - remove the per-user copy first
    (`winget uninstall --accept-source-agreements --disable-interactivity`,
    which it needs or it stops on a prompt nobody can answer).

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
param()

$ErrorActionPreference = 'Stop'

# command -> how to get it, and where it lands. `Dir` is added to the machine
# PATH when the installer does not do it itself (7-Zip never does).
$Tools = [ordered]@{
    'git'    = @{ Package = 'Git.Git';              Dir = 'C:\Program Files\Git\cmd' }
    'pwsh'   = @{ Package = 'Microsoft.PowerShell'; Dir = 'C:\Program Files\PowerShell\7' }
    'node'   = @{ Package = 'OpenJS.NodeJS.LTS';    Dir = 'C:\Program Files\nodejs' }
    'python' = @{ Package = 'Python.Python.3.12';   Dir = 'C:\Program Files\Python312' }
    'dotnet' = @{ Package = 'Microsoft.DotNet.SDK.10'; Dir = 'C:\Program Files\dotnet' }
    'cmake'  = @{ Package = 'Kitware.CMake';        Dir = 'C:\Program Files\CMake\bin' }
    'gh'     = @{ Package = 'GitHub.cli';           Dir = 'C:\Program Files\GitHub CLI' }
    'cargo'  = @{ Package = 'Rustlang.Rustup';      Dir = 'C:\Rust\cargo\bin'
                  # rustup has no machine-wide installer: it installs into
                  # whoever runs it. Give it a home outside any profile and
                  # point every account at it, so the runner's account finds
                  # the same toolchain this install made.
                  MachineEnv = @{ CARGO_HOME = 'C:\Rust\cargo'; RUSTUP_HOME = 'C:\Rust\rustup' } }
    '7z'     = @{ Package = '7zip.7zip';            Dir = 'C:\Program Files\7-Zip' }
}

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

function Get-MachinePathDirs {
    @([Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';' | Where-Object { $_ })
}

function Resolve-ForService([string]$Command) {
    # Exactly what a service can do: look through the machine PATH, nothing
    # else. No user profile, no session PATH, no App Execution Aliases.
    foreach ($dir in (Get-MachinePathDirs)) {
        foreach ($ext in '.exe', '.cmd', '.bat') {
            $candidate = Join-Path $dir ($Command + $ext)
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

function Add-ToMachinePath([string]$Dir) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (($current -split ';') -contains $Dir) { return $false }
    [Environment]::SetEnvironmentVariable('Path', ($current.TrimEnd(';') + ';' + $Dir), 'Machine')
    return $true
}

$report = [ordered]@{}
foreach ($command in $Tools.Keys) {
    $spec = $Tools[$command]
    $found = Resolve-ForService $command
    if ($found) {
        Write-Host ('reachable     : {0} ({1})' -f $command, $found)
        $report[$command] = $found
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($command, 'install and make reachable')) { continue }

    if ($spec.MachineEnv) {
        foreach ($name in $spec.MachineEnv.Keys) {
            [Environment]::SetEnvironmentVariable($name, $spec.MachineEnv[$name], 'Machine')
            Set-Item -Path ("env:" + $name) -Value $spec.MachineEnv[$name]
            Write-Host ('  machine environment: {0}={1}' -f $name, $spec.MachineEnv[$name])
        }
    }

    # Already on the disk but out of sight: a PATH entry is the whole fix.
    if ($spec.Dir -and (Test-Path -LiteralPath $spec.Dir)) {
        if (Add-ToMachinePath $spec.Dir) {
            Write-Host ('on the PATH   : {0} (was installed, could not be reached)' -f $command)
        }
        $found = Resolve-ForService $command
        if ($found) { $report[$command] = $found; continue }
    }

    Write-Host ('installing    : {0} ({1}), for the machine' -f $command, $spec.Package)
    $out = (& winget install --exact --id $spec.Package --source winget --silent --scope machine --force `
                --accept-source-agreements --accept-package-agreements 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        Write-Host ('  machine scope refused ({0}); installing without it' -f $LASTEXITCODE)
        $out = (& winget install --exact --id $spec.Package --source winget --silent --force `
                    --accept-source-agreements --accept-package-agreements 2>&1 | Out-String)
    }
    if ($spec.Dir -and (Test-Path -LiteralPath $spec.Dir)) { [void](Add-ToMachinePath $spec.Dir) }
    $found = Resolve-ForService $command
    $report[$command] = $found
    if (-not $found) {
        Write-Host ('  still not reachable: {0}' -f
                    (($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '))
    }
}

Write-Host ''
Write-Host 'what a runner will find (machine PATH only):'
$missing = @()
foreach ($command in $Tools.Keys) {
    $where = if ($report.Contains($command)) { $report[$command] } else { Resolve-ForService $command }
    if (-not $where) { $missing += $command }
    Write-Host ('  {0,-8} {1}' -f $command, $(if ($where) { $where } else { 'NOT reachable by a service' }))
}

Write-Host ''
if ($missing.Count) {
    Write-Host ('Not reachable by a runner: {0}.' -f ($missing -join ', '))
    Write-Host 'A job will report these absent whatever an administrator sees; fix them before relying on them.'
} else {
    Write-Host 'Every tool resolves from the machine PATH, which is the one a runner gets.'
}
Write-Host 'A runner takes a new PATH only when its service restarts: restart or recreate the runners.'
