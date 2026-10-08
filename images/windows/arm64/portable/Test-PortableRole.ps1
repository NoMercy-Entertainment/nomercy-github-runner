param([ValidateSet('github','forgejo')][string]$Role)
$ErrorActionPreference='Stop'
$root='C:\ProgramData\nomercy\portable'
$base="D:\portable-checks\$Role"
$work=Join-Path $base 'work'
$cache=Join-Path $base 'cache'
try {
    $env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')
    $env:RUSTUP_HOME=[Environment]::GetEnvironmentVariable('RUSTUP_HOME','Machine')
    $env:JAVA_HOME=[Environment]::GetEnvironmentVariable('JAVA_HOME','Machine')
    $env:ANDROID_HOME='C:\Android\sdk'
    $env:ANDROID_SDK_ROOT=$env:ANDROID_HOME
    $env:RUNNER_CACHE_DIR=$cache
    $env:HOME=$work; $env:USERPROFILE=$work
    $env:APPDATA=Join-Path $cache 'roaming'; $env:LOCALAPPDATA=Join-Path $cache 'local'
    foreach ($entry in @{
        CARGO_HOME='cargo'; DOTNET_CLI_HOME='dotnet'; NUGET_PACKAGES='nuget';
        GRADLE_USER_HOME='gradle'; NPM_CONFIG_CACHE='npm'; PIP_CACHE_DIR='pip';
        GOCACHE='go-build'; GOMODCACHE='go-mod'
    }.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key,(Join-Path $cache $entry.Value),'Process')
    }
    New-Item -ItemType Directory -Path $work,$cache -Force | Out-Null
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ($identity -ine "NT SERVICE\rnr-portable-$Role-test") { throw "Unexpected test identity: $identity" }
    & (Join-Path $root 'Test-RunnerBuildEnvironment.ps1') -Architecture arm64 -Workspace $work
    $listener = if ($Role -eq 'github') {
        'C:\ProgramData\nomercy\templates\actions-runner-v2.338.0-windows-arm64\agent\bin\Runner.Listener.exe'
    } else {
        'C:\ProgramData\nomercy\templates\forgejo-runner-v13.1.0-windows-arm64\forgejo-runner.exe'
    }
    & $listener --version
    if ($LASTEXITCODE -ne 0) { throw "$Role runner executable failed its version check." }
    @{passed=$true; identity=$identity; registered=$false} | ConvertTo-Json | Set-Content (Join-Path $base 'result.json')
} catch {
    @{passed=$false; error=$_.Exception.Message} | ConvertTo-Json | Set-Content (Join-Path $base 'result.json')
    exit 1
}
