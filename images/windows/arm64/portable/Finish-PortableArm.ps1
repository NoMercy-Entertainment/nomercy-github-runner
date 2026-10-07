# Runs at Windows startup as SYSTEM; no interactive Windows logon is needed.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\ProgramData\nomercy\portable'
$statusPath = Join-Path $root 'status.json'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$mutex = [Threading.Mutex]::new($false, 'Global\NoMercyPortableArmFinish')
if (-not $mutex.WaitOne(0)) { exit 0 }
$transcriptStarted = $false
function Status([string]$Phase, [string]$Detail) {
    @{ phase=$Phase; detail=$Detail; utc=(Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statusPath -Encoding utf8
}
function MachineEnvironment {
    foreach ($entry in [Environment]::GetEnvironmentVariables('Machine').GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}
function Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe returned $LASTEXITCODE" }
}
try {
    & (Join-Path $root 'Set-ArmShutdownPolicy.ps1')
    if (Test-Path -LiteralPath $statusPath) {
        $old = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        if ($old.phase -eq 'ready') { exit 0 }
    }
    Start-Transcript -Path (Join-Path $root 'finish.log') -Append
    $transcriptStarted = $true
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'Arm64') {
        throw 'This appliance requires Windows ARM64.'
    }
    MachineEnvironment
    Status 'cpp' 'Installing native ARM64 MSVC and Windows SDK.'
    & (Join-Path $root 'Install-CppBuildTools.ps1') -Architecture arm64
    $cpp = Get-Content 'C:\ProgramData\nomercy\cpp-build-tools-result.json' -Raw | ConvertFrom-Json
    if ($cpp.State -eq 'reboot-required') {
        $counterPath = Join-Path $root 'reboots.txt'
        $count = if (Test-Path $counterPath) { [int](Get-Content $counterPath -Raw) } else { 0 }
        if ($count -ge 2) { throw 'Installer still requires reboot after two automatic reboots.' }
        ($count + 1) | Set-Content $counterPath
        Status 'rebooting' 'Restarting Windows to finish MSVC; preparation resumes automatically.'
        Restart-Computer -Force
        exit 0
    }
    if ($cpp.State -ne 'ready') { throw "MSVC is not ready: $($cpp.Detail)" }

    Status 'java' 'Installing native ARM64 Microsoft Java 21.'
    $java = Get-ChildItem 'C:\Program Files\Microsoft' -Directory -Filter 'jdk-21*' -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'bin\javac.exe') } | Select-Object -First 1
    if (-not $java) {
        $archive = Join-Path $root 'microsoft-jdk-21-arm64.zip'
        # The exact vendor URL and digest are captured when this package is built.
        $jdk = Get-Content (Join-Path $root 'jdk.json') -Raw | ConvertFrom-Json
        Invoke-WebRequest -Uri $jdk.url -OutFile $archive
        if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne $jdk.sha256) { throw 'Java SHA-256 mismatch.' }
        Expand-Archive -LiteralPath $archive -DestinationPath 'C:\Program Files\Microsoft' -Force
    }
    # This helper verifies ARM64 Java, publishes machine PATH/JAVA_HOME and installs Android.
    Status 'android' 'Installing Android SDK, platform 35 and build tools.'
    & (Join-Path $root 'Install-AndroidSdk.ps1')
    MachineEnvironment

    Status 'testing' 'Building test programs under separate GitHub and Forgejo service accounts.'
    $nssm = 'C:\ProgramData\nomercy\bin\nssm.exe'
    $pwsh = 'C:\Program Files\PowerShell\7\pwsh.exe'
    $data = Get-Volume -FileSystemLabel 'RNR_ARM_DATA' -ErrorAction Stop
    if ($data.DriveLetter -ne 'D') { throw 'Expected the runner data volume at D:; no disk was formatted.' }
    foreach ($role in @('github','forgejo')) {
        $name = "rnr-portable-$role-test"
        $workspace = "D:\portable-checks\$role"
        New-Item -ItemType Directory -Path $workspace -Force | Out-Null
        $priorService = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($priorService -and $priorService.Status -ne 'Stopped') { throw "Previous test service still active: $name" }
        if (-not $priorService) { Checked $nssm @('install',$name,$pwsh) }
        Checked 'sc.exe' @('config',$name,'obj=',"NT SERVICE\$name",'start=','demand')
        Checked 'sc.exe' @('sidtype',$name,'unrestricted')
        Checked $nssm @('set',$name,'Start','SERVICE_DEMAND_START')
        Checked $nssm @('set',$name,'AppParameters',"-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$root\Test-PortableRole.ps1`" -Role $role")
        Checked $nssm @('set',$name,'AppDirectory',$workspace)
        Checked $nssm @('set',$name,'AppExit','Default','Exit')
        Checked $nssm @('set',$name,'AppStdout',"$workspace\service.log")
        Checked $nssm @('set',$name,'AppStderr',"$workspace\service.log")
        Checked 'icacls.exe' @($workspace,'/grant',"NT SERVICE\${name}:(OI)(CI)F",'/T','/Q')
        $reportPath = Join-Path $workspace 'result.json'
        if (Test-Path $reportPath) { Move-Item $reportPath (Join-Path $workspace ('previous-' + [guid]::NewGuid() + '.json')) }
        Start-Service $name
        while ((Get-Service $name).Status -ne 'Stopped') { Start-Sleep -Seconds 10 }
        if (-not (Test-Path $reportPath)) { throw "$role service exited without a test result; see $workspace\service.log" }
        $report = Get-Content $reportPath -Raw | ConvertFrom-Json
        Copy-Item $reportPath (Join-Path $root "$role-result.json") -Force
        Copy-Item (Join-Path $workspace 'work\runner-build-health.json') (Join-Path $root "$role-build-health.json") -Force
        if (-not $report.passed) { throw "$role service-account checks failed: $($report.error)" }
        Checked $nssm @('remove',$name,'confirm')
        # Remove only these disposable test work/cache directories, after the service stopped.
        foreach ($part in @('work','cache')) {
            $target = [IO.Path]::GetFullPath((Join-Path $workspace $part))
            if (-not $target.StartsWith('D:\portable-checks\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected cleanup path.' }
            Remove-Item -LiteralPath $target -Recurse -Force
            if (Test-Path $target) { throw "Test cleanup failed: $target" }
        }
    }
    Status 'ready' 'Toolchain and both service-account build checks passed. Forge registration is separate; see README.'
} catch {
    Status 'error' $_.Exception.Message
    Write-Error $_
    exit 1
} finally {
    if ($transcriptStarted) { Stop-Transcript }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
