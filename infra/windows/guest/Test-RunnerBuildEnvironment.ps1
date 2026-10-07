# Run from a real CI job, using its service account and inherited PATH.
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64')] [string] $Architecture = 'arm64',
    [string] $Workspace = (Get-Location).Path
)
$ErrorActionPreference = 'Stop'
$failures = [Collections.Generic.List[string]]::new()
$results = [Collections.Generic.List[object]]::new()
$smoke = Join-Path $Workspace ('runner-build-health-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $smoke | Out-Null

function Invoke-Checked([string] $Command, [string[]] $Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command exited with $LASTEXITCODE" }
}
function Test-Build([string] $Name, [scriptblock] $Check) {
    Write-Host "[CHECK] $Name"
    try {
        & $Check
        $results.Add([pscustomobject]@{ Name = $Name; Passed = $true; Error = $null })
        Write-Host "[PASS] $Name"
    } catch {
        $detail = $_.Exception.Message
        $failures.Add("${Name}: $detail")
        $results.Add([pscustomobject]@{ Name = $Name; Passed = $false; Error = $detail })
        Write-Host "::error::${Name}: $detail"
    }
}
function Assert-NativeBinary([string] $Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3c)
    $machine = [BitConverter]::ToUInt16($bytes, $pe + 4)
    $expected = if ($Architecture -eq 'arm64') { 0xaa64 } else { 0x8664 }
    if ($machine -ne $expected) { throw "$Path has PE architecture 0x$('{0:x}' -f $machine)" }
}

Push-Location $smoke
try {
    Test-Build 'Windows and workspace' {
        $osArch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
        if ($osArch -ne $Architecture) { throw "Expected $Architecture, got $osArch" }
        "Account: $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        "OS architecture: $osArch; visible processors: $env:NUMBER_OF_PROCESSORS"
        [IO.File]::WriteAllText((Join-Path $smoke 'probe.txt'), 'runner-write-check')
        if ([IO.File]::ReadAllText((Join-Path $smoke 'probe.txt')) -ne 'runner-write-check') {
            throw 'Workspace readback failed'
        }
    }
    Test-Build 'Runner profile and writable tool caches' {
        if (-not $env:RUNNER_CACHE_DIR) { throw 'RUNNER_CACHE_DIR is missing' }
        $cacheRoot = [IO.Path]::GetFullPath($env:RUNNER_CACHE_DIR)
        $runnerRoot = (Split-Path -Parent $cacheRoot).TrimEnd('\') + '\'
        foreach ($name in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CARGO_HOME',
                            'DOTNET_CLI_HOME', 'NUGET_PACKAGES', 'GRADLE_USER_HOME',
                            'NPM_CONFIG_CACHE', 'PIP_CACHE_DIR', 'GOCACHE', 'GOMODCACHE')) {
            $directory = [Environment]::GetEnvironmentVariable($name)
            if (-not $directory) { throw "$name is missing" }
            $fullPath = [IO.Path]::GetFullPath($directory)
            if (-not $fullPath.StartsWith($runnerRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "$name is outside this runner's cleanup directories: $fullPath"
            }
            New-Item -ItemType Directory -Path $fullPath -Force | Out-Null
            $probe = Join-Path $fullPath ('runner-health-' + $name + '.txt')
            [IO.File]::WriteAllText($probe, 'runner-cache-write-check')
            if ([IO.File]::ReadAllText($probe) -ne 'runner-cache-write-check') { throw "$name readback failed" }
            Write-Host "$name = $fullPath"
        }
    }
    Test-Build 'Git and Bash' {
        Invoke-Checked git @('--version')
        Invoke-Checked bash @('--noprofile', '--norc', '-c', 'test "$(printf build-ok)" = build-ok')
    }
    Test-Build 'Node and npm' {
        Invoke-Checked node @('-e', 'require("node:assert").strictEqual(6 * 7, 42); console.log(process.version, process.arch)')
        Invoke-Checked npm.cmd @('--version')
    }
    Test-Build 'Python' {
        Invoke-Checked python @('-c', 'import platform, ssl, sqlite3; assert sum(range(10)) == 45; print(platform.python_version(), platform.machine())')
    }
    Test-Build 'Java compile and run' {
        [IO.File]::WriteAllText((Join-Path $smoke 'RunnerHello.java'), 'public class RunnerHello { public static void main(String[] args) { if (6 * 7 != 42) throw new AssertionError(); System.out.println("Java build OK"); } }')
        Invoke-Checked javac @('RunnerHello.java')
        Invoke-Checked java @('-cp', $smoke, 'RunnerHello')
    }
    Test-Build 'MSVC C++ compile, architecture and execution' {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        $component = if ($Architecture -eq 'arm64') { 'Microsoft.VisualStudio.Component.VC.Tools.ARM64' } else { 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' }
        $vsRoot = & $vswhere -latest -products '*' -requires $component -property installationPath
        if ($LASTEXITCODE -ne 0 -or -not $vsRoot) { throw 'MSVC workload was not found' }
        $vsdev = Join-Path ($vsRoot | Select-Object -First 1) 'Common7\Tools\VsDevCmd.bat'
        $target = if ($Architecture -eq 'arm64') { 'arm64' } else { 'amd64' }
        [IO.File]::WriteAllText((Join-Path $smoke 'hello.cpp'), '#include <vector>' + "`r`n" + 'int main() { std::vector<int> v{40, 2}; return v[0] + v[1] == 42 ? 0 : 1; }')
        $command = 'call "' + $vsdev + '" -arch=' + $target + ' -host_arch=' + $target + ' >nul && cl /nologo /EHsc /W4 /WX hello.cpp /Fe:hello-cpp.exe'
        Invoke-Checked $env:ComSpec @('/d', '/s', '/c', $command)
        Assert-NativeBinary (Join-Path $smoke 'hello-cpp.exe')
        Invoke-Checked (Join-Path $smoke 'hello-cpp.exe') @()
    }
    Test-Build 'Rust compile, architecture and execution' {
        Invoke-Checked cargo @('new', '--bin', '--vcs', 'none', 'rust-smoke')
        [IO.File]::WriteAllText((Join-Path $smoke 'rust-smoke\src\main.rs'), 'fn main() { assert_eq!(6 * 7, 42); println!("Rust build OK"); }')
        $target = if ($Architecture -eq 'arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
        Invoke-Checked cargo @('build', '--manifest-path', 'rust-smoke\Cargo.toml', '--target', $target)
        $rustBinary = Join-Path $smoke "rust-smoke\target\$target\debug\rust-smoke.exe"
        Assert-NativeBinary $rustBinary
        Invoke-Checked $rustBinary @()
    }
    Test-Build '.NET build and run' {
        Invoke-Checked dotnet @('new', 'console', '--framework', 'net10.0', '--output', 'dotnet-smoke', '--no-restore')
        Invoke-Checked dotnet @('run', '--project', 'dotnet-smoke', '--configuration', 'Release')
    }
    Test-Build 'Go build and run' {
        [IO.File]::WriteAllText((Join-Path $smoke 'hello.go'), 'package main' + "`n" + 'import "fmt"' + "`n" + 'func main() { fmt.Println("Go build OK") }')
        $previous = $env:GOARCH
        try {
            $env:GOARCH = if ($Architecture -eq 'arm64') { 'arm64' } else { 'amd64' }
            Invoke-Checked go @('build', '-o', 'hello-go.exe', 'hello.go')
        } finally { $env:GOARCH = $previous }
        Assert-NativeBinary (Join-Path $smoke 'hello-go.exe')
        Invoke-Checked (Join-Path $smoke 'hello-go.exe') @()
    }
    Test-Build 'PHP' {
        [IO.File]::WriteAllText((Join-Path $smoke 'hello.php'), '<?php if (6 * 7 !== 42) { exit(1); } echo PHP_VERSION, PHP_EOL;')
        Invoke-Checked php @('hello.php')
    }
    Test-Build 'Ruby' {
        Invoke-Checked ruby @('-e', 'raise "failed" unless 6 * 7 == 42; puts RUBY_VERSION')
    }
    Test-Build 'CMake and LLVM builds, architecture and execution' {
        Invoke-Checked cmake @('--version')
        Invoke-Checked clang @('--version')
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        $component = if ($Architecture -eq 'arm64') { 'Microsoft.VisualStudio.Component.VC.Tools.ARM64' } else { 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' }
        $vsRoot = & $vswhere -latest -products '*' -requires $component -property installationPath
        if ($LASTEXITCODE -ne 0 -or -not $vsRoot) { throw 'MSVC environment for CMake and LLVM was not found' }
        $vsdev = Join-Path ($vsRoot | Select-Object -First 1) 'Common7\Tools\VsDevCmd.bat'
        $target = if ($Architecture -eq 'arm64') { 'arm64' } else { 'amd64' }
        $project = Join-Path $smoke 'cmake-project'
        New-Item -ItemType Directory -Path $project | Out-Null
        [IO.File]::WriteAllText((Join-Path $project 'hello.cpp'), '#include <vector>' + "`n" + 'int main() { std::vector<int> v{40, 2}; return v[0] + v[1] == 42 ? 0 : 1; }')
        [IO.File]::WriteAllText((Join-Path $project 'CMakeLists.txt'), 'cmake_minimum_required(VERSION 3.20)' + "`n" + 'project(RunnerBuildHealth LANGUAGES CXX)' + "`n" + 'add_executable(cmake-hello hello.cpp)' + "`n")
        $llvmTarget = if ($Architecture -eq 'arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
        $command = 'call "' + $vsdev + '" -arch=' + $target + ' -host_arch=' + $target +
            ' >nul && cmake -S cmake-project -B cmake-build -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release' +
            ' && cmake --build cmake-build && clang-cl --target=' + $llvmTarget +
            ' /nologo /EHsc /W4 /WX cmake-project\hello.cpp /Fe:hello-clang.exe'
        Invoke-Checked $env:ComSpec @('/d', '/s', '/c', $command)
        foreach ($binary in @('cmake-build\cmake-hello.exe', 'hello-clang.exe')) {
            $binaryPath = Join-Path $smoke $binary
            Assert-NativeBinary $binaryPath
            Invoke-Checked $binaryPath @()
        }
    }
    Test-Build 'Android SDK' {
        if (-not $env:ANDROID_HOME) { throw 'ANDROID_HOME is missing from the runner environment' }
        $jar = Join-Path $env:ANDROID_HOME 'platforms\android-35\android.jar'
        if (-not (Test-Path -LiteralPath $jar)) { throw "Missing $jar" }
        Invoke-Checked adb @('version')
        $aapt = Join-Path $env:ANDROID_HOME 'build-tools\35.0.0\aapt2.exe'
        [IO.File]::WriteAllText((Join-Path $smoke 'AndroidManifest.xml'), '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="me.nomercy.runnerhealth"><uses-sdk android:minSdkVersion="26" android:targetSdkVersion="35"/><application android:hasCode="false"/></manifest>')
        Invoke-Checked $aapt @('link', '-I', $jar, '--manifest', 'AndroidManifest.xml', '-o', 'android-smoke.apk')
        if (-not (Test-Path -LiteralPath (Join-Path $smoke 'android-smoke.apk'))) { throw 'Android APK was not produced' }
    }
    Test-Build 'Archive and GitHub CLI' {
        Invoke-Checked 7z @('a', 'probe.zip', 'probe.txt')
        Invoke-Checked 7z @('t', 'probe.zip')
        Invoke-Checked gh @('--version')
    }
} finally {
    Pop-Location
    $report = Join-Path $Workspace 'runner-build-health.json'
    $results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $report -Encoding utf8
    Write-Host "Report: $report"
}
if ($failures.Count) { throw ($failures -join "`n") }
Write-Host 'All runner build checks passed.'
