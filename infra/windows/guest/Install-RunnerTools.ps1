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

    The C++ desktop workload is installed by Install-CppBuildTools.ps1 and
    verified with an actual compile and link, because native build jobs now
    run on these Windows workers.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('x64', 'arm64')]
    [string] $Architecture = 'x64'
)

$ErrorActionPreference = 'Stop'

# command -> how to get it, and where it lands. `Dir` is added to the machine
# PATH when the installer does not do it itself (7-Zip never does).
$Tools = [ordered]@{
    'git'    = @{ Package = 'Git.Git';              Dir = 'C:\Program Files\Git\cmd' }
    'bash'   = @{ Package = 'Git.Git';              Dir = 'C:\Program Files\Git\bin' }
    'pwsh'   = @{ Package = 'Microsoft.PowerShell'; Dir = 'C:\Program Files\PowerShell\7' }
    'node'   = @{ Package = 'OpenJS.NodeJS.LTS';    Dir = 'C:\Program Files\nodejs' }
    'python' = @{ Package = 'Python.Python.3.12';   Dir = 'C:\Program Files\Python312' }
    'dotnet' = @{ Package = 'Microsoft.DotNet.SDK.10'; Dir = 'C:\Program Files\dotnet' }
    'cmake'  = @{ Package = 'Kitware.CMake';        Dir = 'C:\Program Files\CMake\bin' }
    'go'     = @{ Package = 'GoLang.Go';            Dir = 'C:\Program Files\Go\bin' }
    'java'   = @{ Package = 'EclipseAdoptium.Temurin.21.JDK'
                  DirGlob = 'C:\Program Files\Eclipse Adoptium\jdk-21*\bin' }
    'php'    = @{ Package = 'PHP.PHP.8.4'
                  DirGlob = 'C:\Program Files\WinGet\Packages\PHP.PHP.8.4*' }
    'ruby'   = @{ Package = 'RubyInstallerTeam.RubyWithDevKit.3.3'
                  DirGlob = 'C:\Ruby33*\bin' }
    'clang'  = @{ Package = 'LLVM.LLVM';            Dir = 'C:\Program Files\LLVM\bin' }
    'gh'     = @{ Package = 'GitHub.cli';           Dir = 'C:\Program Files\GitHub CLI' }
    'cargo'  = @{ Package = 'Rustlang.Rustup';      Dir = 'C:\Rust\cargo\bin'
                  # rustup has no machine-wide installer: it installs into
                  # whoever runs it. Give it a home outside any profile and
                  # point every account at it, so the runner's account finds
                  # the same toolchain this install made.
                  MachineEnv = @{ CARGO_HOME = 'C:\Rust\cargo'; RUSTUP_HOME = 'C:\Rust\rustup' } }
    '7z'     = @{ Package = '7zip.7zip';            Dir = 'C:\Program Files\7-Zip' }
}

# The latest Temurin 21 WinGet entry currently selects x64 on Windows ARM.
# Microsoft's JDK 21 distribution also provides a native Windows ARM64 JDK.
if ($Architecture -eq 'arm64') {
    $Tools['java'] = @{ Package = 'Microsoft.OpenJDK.21'
                       DirGlob = 'C:\Program Files\Microsoft\jdk-21*\bin' }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: installing for every user needs an administrator.'
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    # App Installer registration happens after the first interactive logon.
    # A freshly installed guest may run this script before that finishes.
    try {
        Add-AppxPackage -RegisterByFamilyName `
            -MainPackage 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe' `
            -ErrorAction Stop
    } catch {
        Write-Warning "Could not register App Installer: $($_.Exception.Message)"
    }
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

function Get-ToolDirs($Spec) {
    $dirs = @()
    if ($Spec.Dir -and (Test-Path -LiteralPath $Spec.Dir)) { $dirs += $Spec.Dir }
    if ($Spec.DirGlob) {
        $dirs += @(Get-Item -Path $Spec.DirGlob -ErrorAction SilentlyContinue |
                   Where-Object PSIsContainer | Select-Object -ExpandProperty FullName)
    }
    return $dirs
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
    foreach ($dir in (Get-ToolDirs $spec)) {
        if (Add-ToMachinePath $dir) {
            Write-Host ('on the PATH   : {0} (was installed, could not be reached)' -f $command)
        }
        $found = Resolve-ForService $command
        if ($found) { break }
    }
    if ($found) { $report[$command] = $found; continue }

    Write-Host ('installing    : {0} ({1}), for the machine' -f $command, $spec.Package)
    $installerOptions = @()
    if ($command -eq 'cargo') {
        $rustHost = if ($Architecture -eq 'arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
        $rustArguments = '-y --default-host ' + $rustHost +
            ' --default-toolchain stable --profile minimal --component clippy --component rustfmt --no-modify-path'
        $installerOptions = @('--override', $rustArguments)
    }
    $out = (& winget install --exact --id $spec.Package --source winget --silent --scope machine --force `
                --accept-source-agreements --accept-package-agreements --disable-interactivity @installerOptions 2>&1 | Out-String)
    $installExit = $LASTEXITCODE
    # Only a scope/installer selection failure justifies dropping --scope.
    # An aborted WinGet can leave its installer running, or can return after
    # a successful install when App Installer itself updates. Retrying every
    # failure with --force started a second Git install on ARM (2026-10-02).
    # APPINSTALLER_CLI_ERROR_NO_APPLICABLE_INSTALLER = 0x8A150010.
    if ($installExit -eq -1978335216) {
        Write-Host ('  no installer for machine scope ({0}); trying the package default' -f $installExit)
        $out = (& winget install --exact --id $spec.Package --source winget --silent --force `
                    --accept-source-agreements --accept-package-agreements --disable-interactivity @installerOptions 2>&1 | Out-String)
        $installExit = $LASTEXITCODE
    }
    if ($installExit -ne 0) {
        $detail = ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 8) -join ' | '
        throw "Installing $($spec.Package) exited with $installExit. Check its installer before resuming; no automatic reinstall was started. $detail"
    }
    foreach ($dir in (Get-ToolDirs $spec)) { [void](Add-ToMachinePath $dir) }
    $found = Resolve-ForService $command
    $report[$command] = $found
    if (-not $found) {
        Write-Host ('  still not reachable: {0}' -f
                    (($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '))
    }
}

# The WinGet PHP package can resolve on PATH while its package ACL denies
# virtual service accounts (NT SERVICE\rnr-*) read/execute access. A job then
# fails with "Access is denied" even though Get-Command finds php.exe.
$phpExecutable = Resolve-ForService 'php'
if ($phpExecutable -and $phpExecutable.StartsWith('C:\Program Files\WinGet\Packages\',
        [System.StringComparison]::OrdinalIgnoreCase) -and $PSCmdlet.ShouldProcess(
        $phpExecutable, 'grant authenticated runner services read/execute access')) {
    $phpDirectory = Split-Path -Parent $phpExecutable
    & icacls.exe $phpDirectory /grant '*S-1-5-11:(OI)(CI)RX' /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not make PHP executable accessible to runner services: $phpDirectory" }
}

$cppInstaller = Join-Path $PSScriptRoot 'Install-CppBuildTools.ps1'
if (-not (Test-Path -LiteralPath $cppInstaller)) {
    throw "Missing $cppInstaller"
}
if ($PSCmdlet.ShouldProcess('MSVC C++ Build Tools', 'install and compile smoke test')) {
    $cppResultPath = 'C:\ProgramData\nomercy\cpp-build-tools-result.json'
    & $cppInstaller -Architecture $Architecture -ResultPath $cppResultPath
    $cppResult = Get-Content -LiteralPath $cppResultPath -Raw | ConvertFrom-Json
    if ($cppResult.State -ne 'ready') {
        throw "MSVC C++ Build Tools are $($cppResult.State): $($cppResult.Detail)"
    }
}
$androidInstaller = Join-Path $PSScriptRoot 'Install-AndroidSdk.ps1'
if (-not (Test-Path -LiteralPath $androidInstaller)) {
    throw "Missing $androidInstaller"
}
if ($PSCmdlet.ShouldProcess('Android SDK', 'install platform and build tools')) {
    if (-not (Resolve-ForService java)) { throw 'Android SDK requires a machine-wide Java installation.' }
    & $androidInstaller
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
if ($missing.Count -and -not $WhatIfPreference) {
    throw ('Not reachable by a runner: {0}.' -f ($missing -join ', '))
} else {
    Write-Host 'Every tool resolves from the machine PATH, which is the one a runner gets.'
}
Write-Host 'A runner takes a new PATH only when its service restarts: restart or recreate the runners.'
