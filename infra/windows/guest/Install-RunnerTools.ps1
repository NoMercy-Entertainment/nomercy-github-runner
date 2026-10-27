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
    'node'   = @{ Package = 'OpenJS.NodeJS';        Dir = 'C:\Program Files\nodejs' }
    'python' = @{ Package = 'Python.Python.3.12';   Dir = 'C:\Program Files\Python312' }
    'dotnet' = @{ Package = 'Microsoft.DotNet.SDK.10'; Dir = 'C:\Program Files\dotnet' }
    'cmake'  = @{ Package = 'Kitware.CMake';        Dir = 'C:\Program Files\CMake\bin' }
    'go'     = @{ Package = 'GoLang.Go';            Dir = 'C:\Program Files\Go\bin' }
    'java'   = @{ Package = 'EclipseAdoptium.Temurin.21.JDK'
                  DirGlob = 'C:\Program Files\Eclipse Adoptium\jdk-21*\bin' }
    'php'    = @{ Package = 'PHP.PHP.8.5'
                  DirGlob = 'C:\Program Files\WinGet\Packages\PHP.PHP.8.5*' }
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

# Composer has no machine-scope WinGet package. The pinned phar beside a
# composer.bat on the machine PATH gives every runner the `composer` that
# Linux and macOS jobs already have (2026-10-27).
$composerVersion = '2.10.3'
$composerSha256 = '7a2d379d5b8ffdaa028580ef26494c36d2feef4b178d3dd1473a4dbc5e17c8d6'
$composerDir = 'C:\Program Files\Composer'
if (-not (Resolve-ForService 'composer') -and
        $PSCmdlet.ShouldProcess('composer', "install $composerVersion for the machine")) {
    New-Item -ItemType Directory -Force -Path $composerDir | Out-Null
    $phar = Join-Path $composerDir 'composer.phar'
    Invoke-WebRequest -UseBasicParsing -OutFile $phar `
        -Uri "https://getcomposer.org/download/$composerVersion/composer.phar"
    if ((Get-FileHash -LiteralPath $phar -Algorithm SHA256).Hash.ToLower() -ne $composerSha256) {
        Remove-Item -LiteralPath $phar -Force
        throw "composer.phar $composerVersion does not match its published SHA-256"
    }
    Set-Content -LiteralPath (Join-Path $composerDir 'composer.bat') -Encoding ASCII `
        -Value '@php "%~dp0composer.phar" %*'
    [void](Add-ToMachinePath $composerDir)
}

# Node 25 and later no longer bundle corepack. Installed beside node, in
# the machine's Node directory, not in the installing admin's profile.
$nodeDir = 'C:\Program Files\nodejs'
if ((Test-Path -LiteralPath (Join-Path $nodeDir 'npm.cmd')) -and
        -not (Test-Path -LiteralPath (Join-Path $nodeDir 'corepack.cmd')) -and
        $PSCmdlet.ShouldProcess('corepack', 'install beside node')) {
    & (Join-Path $nodeDir 'npm.cmd') install --global --prefix $nodeDir corepack
    if ($LASTEXITCODE -ne 0) { throw "Installing corepack exited with $LASTEXITCODE" }
}

# The Build Tools' MSBuild builds C++ projects; dotnet's own MSBuild does
# not. cl.exe stays off the PATH on purpose: it works only inside the
# developer environment (INCLUDE, LIB), which the build scripts load.
$msbuildDir = Get-Item -Path 'C:\Program Files*\Microsoft Visual Studio\*\*\MSBuild\Current\Bin' `
    -ErrorAction SilentlyContinue | Where-Object PSIsContainer | Select-Object -First 1
if ($msbuildDir -and $PSCmdlet.ShouldProcess('msbuild', 'put the Build Tools MSBuild on the PATH')) {
    [void](Add-ToMachinePath $msbuildDir.FullName)
}

# The clock. Windows Time is demand-started and nothing on this image
# triggers it, so the guest kept only the time it booted with: it ran 17 s
# ahead of the host, and every job event it stamped with it (2026-10-27).
# Running, it takes the host's time through the Hyper-V provider.
if ((Get-Service W32Time).StartType -ne 'Automatic' -and
        $PSCmdlet.ShouldProcess('W32Time', 'start automatically and sync from the host')) {
    Set-Service W32Time -StartupType Automatic
    Start-Service W32Time
    & w32tm.exe /resync /force | Out-Null
}

# Tools with no machine-scope WinGet package that jobs on Linux and macOS
# take for granted (the health checks ask for each, 2026-10-27): pinned
# releases, checked against their publisher's hash, in Program Files and
# on the machine PATH. Gradle and Maven run on the machine's JDK.
$isArm = $Architecture -eq 'arm64'
$Portable = [ordered]@{
    'jq'     = @{ Url = "https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-windows-$(if ($isArm) { 'arm64' } else { 'amd64' }).exe"
                  Sha256 = $(if ($isArm) { '083b5377392bc57cf27052b6d20a2d927770683bca844632901ff38b4b7b0ac7' } else { 'a6fc67fedaf9128a3309a1e2ebb8b986aeccf70122ee46d2cb4849e423f0c627' })
                  Dir = 'C:\Program Files\jq'; Exe = 'jq.exe' }
    # The x64 release zip Chocolatey's wget 1.25.0 verifies (its
    # VERIFICATION.txt). The site answers a client without a browser
    # User-Agent with an HTML challenge page, which the hash check refuses.
    'wget'   = @{ Url = 'https://eternallybored.org/misc/wget/releases/wget-1.25.0-win64.zip'
                  Sha256 = '7567cbc08a1616931162b1c05f1731316853c32c79a21af1e850ccb673d1620c'
                  Dir = 'C:\Program Files\wget'; Zip = $true; X64Only = $true; UserAgent = 'Mozilla/5.0' }
    'ninja'  = @{ Url = "https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-win$(if ($isArm) { 'arm64' }).zip"
                  Sha256 = $(if ($isArm) { 'e52f0bdef9dfb1003229dbd6508a508c4073fd017247002adc66e5e806cb0391' } else { '07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65' })
                  Dir = 'C:\Program Files\Ninja'; Zip = $true }
    'gradle' = @{ Url = 'https://services.gradle.org/distributions/gradle-9.8.2-bin.zip'
                  Sha256 = '21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607'
                  Dir = 'C:\Program Files\Gradle'; Zip = $true; Bin = 'gradle-9.8.2\bin' }
    'mvn'    = @{ Url = 'https://archive.apache.org/dist/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip'
                  Sha512 = 'ed41650d42485cfc243fad22158caf9cbb5dc408ce7a09ddb94dd42a019de929ca43065bfa450612cf12bf78b5cafa3884b96c090de326ff590448c933454af3'
                  Dir = 'C:\Program Files\Maven'; Zip = $true; Bin = 'apache-maven-3.9.16\bin' }
}
foreach ($command in $Portable.Keys) {
    $spec = $Portable[$command]
    $bin = if ($spec.Bin) { Join-Path $spec.Dir $spec.Bin } else { $spec.Dir }
    if ((Resolve-ForService $command) -or -not $PSCmdlet.ShouldProcess($command, 'install a pinned release for the machine')) { continue }
    if ($spec.X64Only -and $isArm) { Write-Host ('no arm64 build : {0}' -f $command); continue }
    Write-Host ('installing    : {0} ({1})' -f $command, $spec.Url)
    $download = Join-Path $env:TEMP ([IO.Path]::GetFileName($spec.Url))
    $agent = if ($spec.UserAgent) { @{ UserAgent = $spec.UserAgent } } else { @{} }
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 -OutFile $download -Uri $spec.Url @agent
    $algorithm = if ($spec.Sha512) { 'SHA512' } else { 'SHA256' }
    $expected = if ($spec.Sha512) { $spec.Sha512 } else { $spec.Sha256 }
    if ((Get-FileHash -LiteralPath $download -Algorithm $algorithm).Hash.ToLower() -ne $expected) {
        Remove-Item -LiteralPath $download -Force
        throw "$($spec.Url) does not match its published $algorithm"
    }
    New-Item -ItemType Directory -Force -Path $spec.Dir | Out-Null
    if ($spec.Zip) {
        & tar.exe -xf $download -C $spec.Dir
        if ($LASTEXITCODE -ne 0) { throw "unpacking $download failed" }
        Remove-Item -LiteralPath $download -Force
    } else {
        # A copy, not a move: a moved file keeps the installing admin's TEMP
        # ACL, and the runners' service accounts were denied jq (2026-10-27).
        Copy-Item -Force -LiteralPath $download -Destination (Join-Path $spec.Dir $spec.Exe)
        Remove-Item -LiteralPath $download -Force
    }
    [void](Add-ToMachinePath $bin)
}

# setup-python and setup-node look in the runner's tool cache before they
# download; the agent fills that from this machine-wide one.
$toolCacheInstaller = Join-Path $PSScriptRoot 'Install-RunnerToolCache.ps1'
if (-not (Test-Path -LiteralPath $toolCacheInstaller)) {
    throw "Missing $toolCacheInstaller"
}
if ($PSCmdlet.ShouldProcess('tool cache', 'Python 3.10-3.14 and Node 20/22/24/26')) {
    & $toolCacheInstaller -Architecture $Architecture
}

Write-Host ''
Write-Host 'what a runner will find (machine PATH only):'
$missing = @()
foreach ($command in @($Tools.Keys) + @('composer', 'corepack', 'msbuild') + @($Portable.Keys) + @('clang-cl')) {
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
