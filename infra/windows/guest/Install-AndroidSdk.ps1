# Install the Android command line SDK for both Windows forge listeners.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$sdk = 'C:\Android\sdk'
$tools = Join-Path $sdk 'cmdline-tools\latest\bin\sdkmanager.bat'
$archive = Join-Path $env:TEMP 'nomercy-android-commandlinetools-win-15859902.zip'
$unpacked = Join-Path $env:TEMP 'nomercy-android-commandlinetools-win-15859902'
$url = 'https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip'
$sha256 = '90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a'

function Add-MachinePath([string] $Directory) {
    $path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (($path -split ';') -notcontains $Directory) {
        [Environment]::SetEnvironmentVariable('Path', ($path.TrimEnd(';') + ';' + $Directory), 'Machine')
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run elevated to install the Android SDK for every runner service.'
}

if (-not (Test-Path -LiteralPath $tools)) {
    Invoke-WebRequest -Uri $url -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $sha256) {
        throw 'The Android command line tools archive failed its SHA-256 check.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $unpacked -Force
    $source = Join-Path $unpacked 'cmdline-tools'
    if (-not (Test-Path -LiteralPath (Join-Path $source 'bin\sdkmanager.bat'))) {
        throw 'Android command line tools archive has an unexpected layout.'
    }
    $latest = Join-Path $sdk 'cmdline-tools\latest'
    New-Item -ItemType Directory -Path $latest -Force | Out-Null
    Copy-Item -Path (Join-Path $source '*') -Destination $latest -Recurse -Force
}

[Environment]::SetEnvironmentVariable('ANDROID_HOME', $sdk, 'Machine')
[Environment]::SetEnvironmentVariable('ANDROID_SDK_ROOT', $sdk, 'Machine')
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$java = Get-Item -Path 'C:\Program Files\Eclipse Adoptium\jdk-21*' -ErrorAction SilentlyContinue |
    Where-Object PSIsContainer | Sort-Object Name -Descending | Select-Object -First 1
if ($java) {
    [Environment]::SetEnvironmentVariable('JAVA_HOME', $java.FullName, 'Machine')
    $env:JAVA_HOME = $java.FullName
}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User')
Add-MachinePath (Join-Path $sdk 'cmdline-tools\latest\bin')
Add-MachinePath (Join-Path $sdk 'platform-tools')

# Windows PowerShell 5.1 promotes native stderr to an ErrorRecord when the
# preference is Stop. sdkmanager writes a deprecation notice to stderr even
# when it succeeds, so rely on its exit code for these two native invocations.
$previousPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    1..100 | ForEach-Object { 'y' } | & $tools --sdk_root=$sdk --licenses 2>$null | Out-Null
    $licenseExit = $LASTEXITCODE
    if ($licenseExit -eq 0) {
        & $tools --sdk_root=$sdk 'platform-tools' 'platforms;android-35' 'build-tools;35.0.0' 2>$null | Out-Null
        $installExit = $LASTEXITCODE
    }
} finally {
    $ErrorActionPreference = $previousPreference
}
if ($licenseExit -ne 0) { throw "Android SDK licences were not accepted (exit $licenseExit)." }
if ($installExit -ne 0) { throw "Installing Android SDK packages failed (exit $installExit)." }
foreach ($path in @('platform-tools\adb.exe', 'platforms\android-35\android.jar',
                    'build-tools\35.0.0\aapt2.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sdk $path))) { throw "Missing Android SDK component: $path" }
}
Write-Host "Android SDK ready at $sdk"
