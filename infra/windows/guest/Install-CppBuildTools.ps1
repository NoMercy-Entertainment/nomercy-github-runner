# Install the C++ compiler and Windows SDK for a Windows runner guest.
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64')]
    [string] $Architecture = 'x64',
    [string] $ResultPath = 'C:\ProgramData\nomercy\cpp-build-tools-result.json'
)

$ErrorActionPreference = 'Stop'
$component = if ($Architecture -eq 'arm64') {
    'Microsoft.VisualStudio.Component.VC.Tools.ARM64'
} else {
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64'
}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$bootstrapper = Join-Path $env:ProgramData 'nomercy\vs_buildtools_2022.exe'
$resultDirectory = Split-Path -Parent $ResultPath
New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null

function Write-Result([string] $State, [string] $Detail) {
    [pscustomobject]@{
        State = $State
        Detail = $Detail
        Architecture = $Architecture
        Updated = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
}

function Find-Compiler {
    if (-not (Test-Path -LiteralPath $vswhere)) { return $null }
    $root = (& $vswhere -latest -products '*' -requires $component -property installationPath |
        Select-Object -First 1)
    if (-not $root) { return $null }
    $toolsRoot = Join-Path $root 'VC\Tools\MSVC'
    if (-not (Test-Path -LiteralPath $toolsRoot)) { return $null }
    $pattern = if ($Architecture -eq 'arm64') { '*\Hostarm64\arm64\cl.exe' } else { '*\Hostx64\x64\cl.exe' }
    $compiler = Get-ChildItem -LiteralPath $toolsRoot -Filter cl.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object FullName -Like $pattern | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $compiler) { return $null }
    return [pscustomobject]@{ Root = $root; Path = $compiler.FullName }
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run elevated to install Visual Studio Build Tools for the machine.'
    }
    $installed = Find-Compiler
    if (-not $installed) {
        if ((Get-PSDrive C).Free -lt 30GB) { throw 'At least 30 GB free on C: is required.' }
        Write-Result 'downloading' 'Downloading Microsoft Visual Studio 2022 Build Tools.'
        Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vs_buildtools.exe' -OutFile $bootstrapper
        $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
            throw 'The Build Tools bootstrapper does not have a valid Microsoft signature.'
        }
        $arguments = @(
            '--quiet', '--wait', '--norestart',
            '--installPath', '"C:\Program Files\Microsoft Visual Studio\2022\BuildTools"',
            '--add', 'Microsoft.VisualStudio.Workload.VCTools',
            '--includeRecommended'
        )
        if ($Architecture -eq 'arm64') { $arguments += @('--add', $component) }
        Write-Result 'installing' 'Installing the C++ desktop workload and recommended compiler and SDK components.'
        $process = Start-Process -FilePath $bootstrapper -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Visual Studio Build Tools installer exited with $($process.ExitCode)."
        }
        $installed = Find-Compiler
        if (-not $installed) { throw "The $Architecture C++ compiler is still absent after installation." }
        if ($process.ExitCode -eq 3010) {
            Write-Result 'reboot-required' $installed.Path
            return
        }
    }

    $vsdev = Join-Path $installed.Root 'Common7\Tools\VsDevCmd.bat'
    if (-not (Test-Path -LiteralPath $vsdev)) { throw "Missing $vsdev" }
    $smokeRoot = Join-Path $env:TEMP 'nomercy-cpp-smoke'
    New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
    $source = Join-Path $smokeRoot 'hello.c'
    $binary = Join-Path $smokeRoot 'hello.exe'
    [IO.File]::WriteAllText($source, 'int main(void) { return 0; }')
    $target = if ($Architecture -eq 'arm64') { 'arm64' } else { 'amd64' }
    $command = 'call "' + $vsdev + '" -arch=' + $target +
        ' -host_arch=' + $target + ' >nul && cl /nologo /W4 /WX /Fe:"' +
        $binary + '" "' + $source + '"'
    & $env:ComSpec /d /s /c $command | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $binary)) {
        throw 'MSVC could not compile and link the C smoke test.'
    }
    & $binary
    if ($LASTEXITCODE -ne 0) { throw 'The compiled C smoke test did not run successfully.' }
    Write-Result 'ready' $installed.Path
} catch {
    Write-Result 'error' $_.Exception.Message
    throw
}
