# NoMercy GitHub Actions Runners - Windows installer
#
# Sets up self-hosted runners in a dedicated WSL2 distribution with its own
# Docker engine, so the runners' storage is a separate pool from whatever
# Docker you already run. A runaway build cannot fill your default Docker
# storage and break your own containers.
#
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\nomercy-github-runners-setup.ps1
#
# Non-interactive:
#   .\nomercy-github-runners-setup.ps1 -Org NoMercy-Entertainment -Token ghp_xxx `
#       -DataPath D:\NoMercyRunners -RunnerCount 4 -NonInteractive
#
# ASCII only, deliberately. Windows PowerShell 5.1 reads a BOM-less .ps1 as
# ANSI, so a stray non-ASCII character breaks string parsing in ways that are
# painful to diagnose.

[CmdletBinding()]
param(
    [string] $Org,
    [string] $Token,
    [string] $DataPath,
    [string] $RunnerGroup,
    [string] $Labels,
    [int]    $RunnerCount = 0,
    [string] $CpuLimit,
    [string] $MemLimit,
    [int]    $DiskCeilingGb = 0,
    [string] $DistroName = 'nomercy-runners',
    [int]    $DashboardPort = 0,
    [switch] $NoDashboard,
    [switch] $NonInteractive
)

$ErrorActionPreference = 'Stop'

# Trim every text parameter before anything looks at it. A value of " " for
# the runner group is not blank - it reaches config.sh as a real group name,
# which fails with "Could not find any self-hosted runner group named ' '"
# and leaves the container restart-looping. Whitespace is never meaningful in
# any of these values, so normalise it once here rather than at each use.
foreach ($p in 'Org','Token','DataPath','RunnerGroup','Labels','CpuLimit','MemLimit','DistroName') {
    $v = (Get-Variable -Name $p -ValueOnly -ErrorAction SilentlyContinue)
    if ($v -is [string]) { Set-Variable -Name $p -Value $v.Trim() }
}

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

$RUNNER_VERSION   = '2.336.0'
$DISTRO_IMAGE     = 'Ubuntu-24.04'
$MIN_FREE_GB      = 40
$DEFAULT_LABELS   = 'self-hosted,Linux,X64'
$DEFAULT_COUNT    = 2
$DEFAULT_CEILING  = 250
$DEFAULT_PORT     = 9200
$RUNNER_IMAGE     = 'ghcr.io/nomercy-entertainment/nomercy-github-runner:latest'
$script:ReadyCount = 0

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

function Write-Head($text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Cyan
    Write-Host ('-' * $text.Length) -ForegroundColor DarkCyan
}
function Write-Ok($text)   { Write-Host "  [ ok ] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [warn] $text" -ForegroundColor Yellow }
function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }

function Fail($text, $hint) {
    Write-Host ''
    Write-Host "  [FAIL] $text" -ForegroundColor Red
    if ($hint) { Write-Host "         $hint" -ForegroundColor DarkYellow }
    Write-Host ''
    exit 1
}

# WSL emits UTF-16LE. Piping it straight into PowerShell string matching gives
# text with a NUL between every character, so every -match silently fails.
function Invoke-Wsl {
    param([string[]] $WslArgs)
    # Anything a native command writes to stderr becomes a terminating error
    # while ErrorActionPreference is Stop. WSL writes warnings there as a
    # matter of course - an unrecognised key in .wslconfig, for example - so
    # without this the install aborts over something entirely harmless.
    # Real failures are caught by checking $LASTEXITCODE at the call sites.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = (& wsl.exe @WslArgs 2>&1 | Out-String)
        return ($raw -replace "`0", '')
    } finally {
        $ErrorActionPreference = $prev
    }
}

# --------------------------------------------------------------------------
# input helpers
# --------------------------------------------------------------------------

function Read-Answer {
    param(
        [string] $Prompt,
        [string] $Default,
        [scriptblock] $Validate,
        [string] $ValidationHint
    )
    if ($NonInteractive) {
        if ($Default) { return $Default }
        Fail "Missing required value: $Prompt" "Pass it as a parameter when using -NonInteractive."
    }
    while ($true) {
        if ($Default) { $shown = "$Prompt [$Default]" } else { $shown = "$Prompt" }
        Write-Host ''
        Write-Host "  $shown" -ForegroundColor White
        $answer = Read-Host '  >'
        if (-not $answer) { $answer = $Default }
        if (-not $answer) { Write-Warn 'A value is required.'; continue }
        if ($Validate -and -not (& $Validate $answer)) {
            Write-Warn $ValidationHint
            continue
        }
        return $answer
    }
}

function Read-Secret {
    param([string] $Prompt)
    if ($NonInteractive) { Fail "Missing token" "Pass -Token when using -NonInteractive." }
    Write-Host ''
    Write-Host "  $Prompt" -ForegroundColor White
    $secure = Read-Host '  >' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try   { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Read-YesNo {
    param([string] $Prompt, [bool] $DefaultYes = $true)
    if ($NonInteractive) { return $DefaultYes }
    if ($DefaultYes) { $hint = 'Y/n' } else { $hint = 'y/N' }
    while ($true) {
        Write-Host ''
        Write-Host "  $Prompt [$hint]" -ForegroundColor White
        $a = (Read-Host '  >').Trim().ToLower()
        if (-not $a) { return $DefaultYes }
        if ($a -in @('y','yes')) { return $true }
        if ($a -in @('n','no'))  { return $false }
        Write-Warn "Answer y or n."
    }
}

function Get-FreeGb {
    param([string] $Path)
    try {
        $root = [System.IO.Path]::GetPathRoot((Resolve-PathForce $Path))
        $d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($root.TrimEnd('\'))'"
        if ($d) { return [math]::Round($d.FreeSpace / 1GB, 1) }
    } catch { }
    return $null
}

# Resolve-Path fails on a path that does not exist yet, which is the normal
# case here - the operator is choosing where to put something new.
function Resolve-PathForce {
    param([string] $Path)
    try { return [System.IO.Path]::GetFullPath($Path) }
    catch { return $Path }
}

# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

function Test-Preflight {
    Write-Head 'Checking this machine'

    $os = [Environment]::OSVersion.Version
    if ($os.Build -lt 19041) {
        Fail "Windows build $($os.Build) is too old for WSL2." `
             "WSL2 needs build 19041 (Windows 10 2004) or later."
    }
    Write-Ok "Windows build $($os.Build)"

    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Fail 'wsl.exe was not found.' `
             'Install WSL with:  wsl --install   then reboot and re-run this script.'
    }

    $status = Invoke-Wsl @('--status')
    if ($LASTEXITCODE -ne 0 -and -not $status) {
        Fail 'WSL is present but not responding.' `
             'Try:  wsl --install   or   wsl --update'
    }
    Write-Ok 'WSL is available'

    # A distro is version 1 or 2; runners need 2. --set-default-version only
    # affects new distros, so check rather than assume.
    $ver = Invoke-Wsl @('--version')
    if ($ver -match 'WSL version:\s*([0-9]+)\.') {
        Write-Ok "WSL version $($Matches[1]).x"
    } else {
        Write-Warn 'Could not read the WSL version. Continuing, but WSL2 is required.'
    }

    $existing = Invoke-Wsl @('--list', '--quiet')
    if ($existing -match [regex]::Escape($DistroName)) {
        Fail "A WSL distribution named '$DistroName' already exists." `
             "Remove it with the uninstaller, or pass -DistroName to use a different name."
    }
    Write-Ok "The name '$DistroName' is free"

    if (Get-Command docker.exe -ErrorAction SilentlyContinue) {
        Write-Info 'Docker is installed on this machine. That is fine - the runners'
        Write-Info 'will NOT use it. They get their own engine and their own storage.'
    }
}

# --------------------------------------------------------------------------
# GitHub validation
# --------------------------------------------------------------------------

function Test-GitHubAccess {
    param([string] $Organisation, [string] $Pat)

    # Windows PowerShell 5.1 can still default to TLS 1.0, which api.github.com
    # refuses. Without this the call fails with a confusing connection error.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    $uri = "https://api.github.com/orgs/$Organisation/actions/runners/registration-token"
    try {
        $null = Invoke-RestMethod -Method Post -Uri $uri -TimeoutSec 25 -Headers @{
            Authorization          = "Bearer $Pat"
            Accept                 = 'application/vnd.github+json'
            'X-GitHub-Api-Version' = '2022-11-28'
            'User-Agent'           = 'nomercy-runner-setup'
        }
        return @{ ok = $true }
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        switch ($code) {
            401     { return @{ ok = $false; why = 'The token was rejected (401). Check it was copied in full and has not expired.' } }
            403     { return @{ ok = $false; why = "The token lacks permission (403). It needs admin:org scope, or manage_self_hosted_runners on '$Organisation'." } }
            404     { return @{ ok = $false; why = "Organisation '$Organisation' was not found, or this token cannot see it (404). Check the spelling." } }
            default { return @{ ok = $false; why = "Could not reach the GitHub API: $($_.Exception.Message)" } }
        }
    }
}

# --------------------------------------------------------------------------
# wizard
# --------------------------------------------------------------------------

function Invoke-Wizard {
    Write-Head 'GitHub'

    if (-not $script:Org) {
        $script:Org = Read-Answer -Prompt 'GitHub organisation' -Default 'NoMercy-Entertainment' `
            -Validate { param($v) $v -match '^[A-Za-z0-9._-]+$' } `
            -ValidationHint 'Use the organisation name as it appears in the URL, not the full URL.'
    }

    while ($true) {
        if (-not $script:Token) {
            $script:Token = Read-Secret -Prompt "Personal access token for '$script:Org' (input hidden)"
        }
        Write-Info 'Checking the token against GitHub...'
        $check = Test-GitHubAccess -Organisation $script:Org -Pat $script:Token
        if ($check.ok) { Write-Ok 'Token works and can register runners'; break }

        Write-Warn $check.why
        if ($NonInteractive) { Fail 'Token validation failed.' $check.why }
        $script:Token = $null   # ask again
    }

    if (-not $script:RunnerGroup) {
        $script:RunnerGroup = Read-Answer -Prompt 'Runner group (blank for the org default)' -Default ' '
        if ($script:RunnerGroup -eq ' ') { $script:RunnerGroup = '' }
    }

    Write-Head 'Runners'

    if ($script:RunnerCount -le 0) {
        $script:RunnerCount = [int](Read-Answer -Prompt 'How many runners' -Default "$DEFAULT_COUNT" `
            -Validate { param($v) ($v -match '^\d+$') -and ([int]$v -ge 1) -and ([int]$v -le 32) } `
            -ValidationHint 'Enter a whole number between 1 and 32.')
    }

    if (-not $script:Labels) {
        $script:Labels = Read-Answer -Prompt 'Runner labels (comma separated)' -Default $DEFAULT_LABELS
    }

    if (-not $script:CpuLimit) {
        $script:CpuLimit = Read-Answer -Prompt 'CPU cores per runner (0 = unlimited)' -Default '0' `
            -Validate { param($v) $v -match '^\d+(\.\d+)?$' } `
            -ValidationHint 'Enter a number, for example 4. Use 0 for no limit.'
    }
    if (-not $script:MemLimit) {
        $script:MemLimit = Read-Answer -Prompt 'Memory per runner, e.g. 8G (0 = unlimited)' -Default '0' `
            -Validate { param($v) $v -match '^\d+[GgMm]?$' } `
            -ValidationHint 'Enter a size such as 8G or 8192M. Use 0 for no limit.'
    }

    Write-Head 'Storage'
    Write-Info 'This is where the runners keep everything: their Docker images,'
    Write-Info 'build caches and workspaces. It is a separate pool from any Docker'
    Write-Info 'you already run, so the runners cannot fill your existing storage.'
    Write-Info ''
    Write-Info 'Pick a drive with room to spare. Builds are large.'

    while ($true) {
        if (-not $script:DataPath) {
            $script:DataPath = Read-Answer -Prompt 'Where should the runners store their data' `
                -Default 'C:\NoMercyRunners'
        }
        $script:DataPath = Resolve-PathForce $script:DataPath

        $free = Get-FreeGb $script:DataPath
        if ($null -eq $free) {
            Write-Warn "Could not read free space for '$script:DataPath'. Check the drive exists."
            if ($NonInteractive) { Fail 'Unusable storage path.' $script:DataPath }
            $script:DataPath = $null; continue
        }

        Write-Info ''
        Write-Info "  path : $script:DataPath"
        Write-Info "  free : $free GB"

        if ($free -lt $MIN_FREE_GB) {
            Write-Warn "That volume has $free GB free. At least $MIN_FREE_GB GB is recommended."
            if ($NonInteractive) { Fail 'Not enough free space.' "$free GB available, $MIN_FREE_GB GB needed." }
            if (-not (Read-YesNo 'Use it anyway' $false)) { $script:DataPath = $null; continue }
        }
        break
    }

    if ($script:DiskCeilingGb -le 0) {
        $script:DiskCeilingGb = [int](Read-Answer `
            -Prompt 'Maximum size for the runners disk, in GB' -Default "$DEFAULT_CEILING" `
            -Validate { param($v) ($v -match '^\d+$') -and ([int]$v -ge 20) } `
            -ValidationHint 'Enter a whole number of GB, at least 20.')
        Write-Info 'This is a ceiling, not a reservation. The disk grows as used.'
    }

    Write-Head 'Dashboard'
    if ($NoDashboard) {
        $script:WantDashboard = $false
    } else {
        $script:WantDashboard = Read-YesNo 'Install the web dashboard for managing these runners' $true
    }
    if ($script:WantDashboard -and $script:DashboardPort -le 0) {
        $script:DashboardPort = [int](Read-Answer -Prompt 'Dashboard port' -Default "$DEFAULT_PORT" `
            -Validate { param($v) ($v -match '^\d+$') -and ([int]$v -ge 1024) -and ([int]$v -le 65535) } `
            -ValidationHint 'Enter a port between 1024 and 65535.')
    }
}

# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

function Show-Summary {
    $free = Get-FreeGb $script:DataPath

    Write-Head 'About to install'

    Write-Host ''
    Write-Host '  STORAGE' -ForegroundColor Yellow
    Write-Host "    Location        $script:DataPath"
    Write-Host "    Free on volume  $free GB"
    Write-Host "    Disk ceiling    $script:DiskCeilingGb GB"
    Write-Host ''
    Write-Host '  GITHUB' -ForegroundColor Yellow
    Write-Host "    Organisation    $script:Org"
    if ($script:RunnerGroup) { $grp = $script:RunnerGroup } else { $grp = '(org default)' }
    Write-Host "    Runner group    $grp"
    Write-Host "    Token           validated"
    Write-Host ''
    Write-Host '  RUNNERS' -ForegroundColor Yellow
    Write-Host "    Count           $script:RunnerCount"
    Write-Host "    Labels          $script:Labels"
    if ($script:CpuLimit -eq '0') { $c = 'unlimited' } else { $c = "$script:CpuLimit cores" }
    if ($script:MemLimit -eq '0') { $m = 'unlimited' } else { $m = $script:MemLimit }
    Write-Host "    CPU per runner  $c"
    Write-Host "    Mem per runner  $m"
    Write-Host "    Runner version  $RUNNER_VERSION"
    Write-Host ''
    Write-Host '  ENGINE' -ForegroundColor Yellow
    Write-Host "    WSL distro      $DistroName  ($DISTRO_IMAGE)"
    Write-Host "    Isolation       own Docker engine, own virtual disk"
    if ($script:WantDashboard) {
        Write-Host "    Dashboard       http://localhost:$script:DashboardPort"
    } else {
        Write-Host "    Dashboard       not installed"
    }
    Write-Host ''
    Write-Host '  Nothing has been created yet.' -ForegroundColor DarkGray
}

# --------------------------------------------------------------------------
# install actions
# --------------------------------------------------------------------------

# C:\Users\me\Temp\x.sh -> /mnt/c/Users/me/Temp/x.sh
function ConvertTo-WslPath {
    param([string] $WinPath)
    $full = [System.IO.Path]::GetFullPath($WinPath)
    $drive = $full.Substring(0,1).ToLower()
    return '/mnt/' + $drive + ($full.Substring(2) -replace '\\','/')
}

# Run a bash script inside the distro by handing it over as a FILE.
# Piping a script through PowerShell into wsl.exe mangles $(...) and
# redirections; and the file lives on a Windows filesystem, so CRLF must be
# stripped or the kernel cannot find the interpreter from the shebang.
function Invoke-WslScript {
    param([string] $Body, [string] $Label)
    $tmp = Join-Path $env:TEMP ("nomercy-" + [guid]::NewGuid().ToString('N') + '.sh')
    try {
        # ASCII + LF: this file is read by bash, not PowerShell.
        [System.IO.File]::WriteAllText($tmp, ($Body -replace "`r`n","`n"), [System.Text.Encoding]::ASCII)
        $wslTmp = ConvertTo-WslPath $tmp
        $out = Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
                            "tr -d '\r' < $wslTmp > /tmp/run.sh && bash /tmp/run.sh")
        if ($LASTEXITCODE -ne 0) { Fail "$Label failed." $out.Trim() }
        return $out
    } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

function Install-Distro {
    Write-Head 'Creating the runner environment'

    New-Item -ItemType Directory -Force $script:DataPath | Out-Null
    Write-Info "Downloading and installing $DISTRO_IMAGE. This takes a few minutes."

    $out = Invoke-Wsl @('--install', $DISTRO_IMAGE, '--name', $DistroName,
                        '--location', $script:DataPath, '--no-launch')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the WSL distribution.' $out.Trim() }

    # The whole point of this installer is that storage lands where the
    # operator asked. If it did not, stop rather than build on a wrong base.
    $vhdx = Get-ChildItem $script:DataPath -Filter *.vhdx -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $vhdx) {
        Fail "No virtual disk was created under $script:DataPath." `
             "The distribution did not install where it was told to, so the storage would not be isolated."
    }
    Write-Ok "Virtual disk: $($vhdx.FullName)"

    Write-Info 'Enabling systemd...'
    Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
                 "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf") | Out-Null
    Invoke-Wsl @('--terminate', $DistroName) | Out-Null
    Start-Sleep -Seconds 3
    Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'systemctl', 'is-system-running', '--wait') | Out-Null
    Write-Ok 'systemd is running'
}

function Install-Engine {
    Write-Head 'Installing the isolated Docker engine'
    Write-Info 'This engine is only for the runners. Your own Docker is untouched.'

    $script = @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git >/dev/null
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
systemctl enable --now docker
docker info --format 'engine={{.ServerVersion}} storage={{.Driver}}'
'@
    $r = Invoke-WslScript -Body $script -Label 'Docker installation'
    Write-Ok ($r.Trim() -split "`n" | Select-Object -Last 1)

    # Prove the daemon comes back on its own. If it does not, the keepalive
    # task cannot bring the runners back after a reboot and the operator would
    # only find out the next time the machine restarts.
    Write-Info 'Checking the engine restarts on its own...'
    Invoke-Wsl @('--terminate', $DistroName) | Out-Null
    Start-Sleep -Seconds 3
    $check = Invoke-WslScript -Body @'
for i in $(seq 1 40); do
  if docker info >/dev/null 2>&1; then echo "up after ${i}s"; exit 0; fi
  sleep 1
done
echo "docker did not start"; exit 1
'@ -Label 'Engine restart check'
    Write-Ok ("Engine " + $check.Trim())

    Write-Info 'Fetching runner sources...'
    $clone = @"
set -euo pipefail
rm -rf /opt/nomercy-runners
mkdir -p /opt/nomercy-runners
git clone --depth 1 https://github.com/NoMercy-Entertainment/nomercy-github-runner.git /opt/nomercy-runners/repo >/dev/null 2>&1
# start.sh must live on the distro's own filesystem, not a /mnt path.
install -m 0755 /opt/nomercy-runners/repo/scripts/start.sh /opt/nomercy-runners/start.sh
echo ok
"@
    Invoke-WslScript -Body $clone -Label 'Fetching runner sources' | Out-Null
    Write-Ok 'Runner sources in place'
}

function Install-Keepalive {
    Write-Head 'Keeping the environment running'

    # WSL shuts down an idle distribution, which stops Docker and kills every
    # runner. Enabling systemd is NOT enough - WSL needs a live session
    # holding the distro open. Without this the runners enter a
    # register/stop/restart loop roughly every 20 seconds.
    $keepDir = Join-Path $env:LOCALAPPDATA 'NoMercyRunners'
    New-Item -ItemType Directory -Force $keepDir | Out-Null
    $keepScript = Join-Path $keepDir 'keepalive.ps1'
    $logPath    = Join-Path $keepDir 'keepalive.log'

    $body = @"
`$distro = '$DistroName'
`$log    = '$logPath'
function Note(`$m) {
  try {
    if ((Test-Path `$log) -and ((Get-Item `$log).Length -gt 512KB)) { Remove-Item `$log -Force }
    Add-Content -Path `$log -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), `$m)
  } catch { }
}
Note "keepalive starting for `$distro"
while (`$true) {
  try {
    & wsl.exe -d `$distro -u root -- systemctl start docker 2>&1 | Out-Null
    Note 'holding distro open'
    & wsl.exe -d `$distro -u root -- sleep infinity 2>&1 | Out-Null
    Note 'hold dropped - re-establishing'
  } catch { Note "error: `$(`$_.Exception.Message)" }
  Start-Sleep -Seconds 5
}
"@
    [System.IO.File]::WriteAllText($keepScript, ($body -replace "`r`n","`n"), [System.Text.Encoding]::ASCII)

    $taskName = "NoMercy Runners - Keep $DistroName Alive"
    # Absolute path: scheduled tasks do not inherit PATH, and a bare
    # powershell.exe fails with 0x80070002.
    $pwsh = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $pwsh `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$keepScript`""
    # Logon trigger, running as this user: WSL distributions are per-user, so
    # a task running as SYSTEM cannot see this one at all.
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -DontStopOnIdleEnd -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -MultipleInstances IgnoreNew
    # Runs forever by design; without this Windows kills it after 3 days and
    # the runners quietly die.
    $settings.ExecutionTimeLimit = 'PT0S'

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 6
    Write-Ok "Scheduled task registered: $taskName"
    Write-Info "Log: $logPath"
    Write-Warn 'Runners return after a reboot once you log in (logon-triggered).'
}

function New-Runners {
    Write-Head "Creating $script:RunnerCount runner(s)"
    Write-Info 'Pulling the runner image. This is large and takes a while.'

    Invoke-WslScript -Body "docker pull $RUNNER_IMAGE >/dev/null 2>&1 && echo pulled" `
                     -Label 'Pulling the runner image' | Out-Null
    Write-Ok 'Runner image pulled'

    $limits = ''
    if ($script:CpuLimit -ne '0') { $limits += " --cpus $script:CpuLimit" }
    if ($script:MemLimit -ne '0') { $limits += " --memory $script:MemLimit" }

    for ($i = 1; $i -le $script:RunnerCount; $i++) {
        $name = "nomercy-runner-$i"
        # --stop-timeout 60: Engine 29.x creates containers with StopTimeout=1
        # (moby/moby#52775), which kills runner deregistration mid-flight and
        # leaves orphaned registrations in the organisation.
        $create = @"
docker rm -f $name >/dev/null 2>&1 || true
docker run -d --name $name \
  --privileged --restart unless-stopped --stop-timeout 60 \
  --label nomercy.runner=true \
  --tmpfs /tmp \
  -v /opt/nomercy-runners/start.sh:/root/start.sh:ro \
  -e GH_TOKEN='$script:Token' \
  -e GITHUB_ORG='$script:Org' \
  -e RUNNER_LABELS='$script:Labels' \
  -e RUNNER_GROUP='$script:RunnerGroup'$limits \
  $RUNNER_IMAGE >/dev/null
echo created
"@
        Invoke-WslScript -Body $create -Label "Creating $name" | Out-Null
        Write-Ok "Created $name"
    }

    Write-Info 'Waiting for the runners to register with GitHub...'
    $ready = 0
    for ($t = 0; $t -lt 40; $t++) {
        Start-Sleep -Seconds 6
        $r = Invoke-WslScript -Body @"
n=0
for c in `$(docker ps --format '{{.Names}}' | grep '^nomercy-runner-'); do
  if docker logs --tail 40 "`$c" 2>&1 | grep -q 'Listening for Jobs'; then n=`$((n+1)); fi
done
echo `$n
"@ -Label 'Registration check'
        $ready = [int]($r.Trim() -split "`n" | Select-Object -Last 1)
        Write-Host "    $ready of $script:RunnerCount ready" -ForegroundColor DarkGray
        if ($ready -ge $script:RunnerCount) { break }
    }

    $script:ReadyCount = $ready

    if ($ready -lt $script:RunnerCount) {
        Write-Warn "$ready of $script:RunnerCount runners registered."
        # Surface the runner's own complaint. Without this the operator sees a
        # timeout and has to go digging, when the container usually says
        # exactly what is wrong - a bad runner group, a rejected token.
        $err = Invoke-WslScript -Body @'
docker logs --tail 40 nomercy-runner-1 2>&1 |
  grep -iE "error|could not|denied|not found|invalid" | tail -4
'@ -Label 'Reading runner log'
        if ($err.Trim()) {
            Write-Host ''
            Write-Host '  The runner reported:' -ForegroundColor Yellow
            foreach ($line in ($err.Trim() -split "`n")) {
                Write-Host "    $($line.Trim())" -ForegroundColor Red
            }
        }
        Write-Host ''
        Write-Info "Full log:  wsl -d $DistroName -u root -- docker logs nomercy-runner-1"
    } else {
        Write-Ok "All $script:RunnerCount runners are listening for jobs"
    }
}

function Install-Dashboard {
    Write-Head 'Installing the dashboard'
    $build = @"
set -euo pipefail
cd /opt/nomercy-runners/repo/dashboard
docker build -t nomercy/runner-dashboard:local . >/dev/null 2>&1
docker rm -f nomercy-runner-dashboard >/dev/null 2>&1 || true
docker run -d --name nomercy-runner-dashboard \
  --restart unless-stopped \
  -p $($script:DashboardPort):9200 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/nomercy-runners:/repo \
  -v nomercy-dashboard-data:/data \
  nomercy/runner-dashboard:local >/dev/null
echo started
"@
    Invoke-WslScript -Body $build -Label 'Dashboard install' | Out-Null
    Write-Ok "Dashboard running on http://localhost:$($script:DashboardPort)"
    Write-Info 'It will ask you to set a password the first time you open it.'
}

function Show-NextSteps {
    $ok = ($script:ReadyCount -ge $script:RunnerCount)
    if ($ok) { Write-Head 'Done' } else { Write-Head 'Finished with problems' }

    Write-Host ''
    if ($ok) {
        Write-Host "  Runners      $script:RunnerCount, registered to $script:Org" -ForegroundColor Green
    } else {
        Write-Host "  Runners      $script:ReadyCount of $script:RunnerCount registered to $script:Org" -ForegroundColor Red
        Write-Host "               The environment is installed; the runners are not all up." -ForegroundColor Red
    }
    Write-Host "  Storage      $script:DataPath" -ForegroundColor Green
    Write-Host "  Isolation    own Docker engine in WSL distro '$DistroName'" -ForegroundColor Green
    if ($script:WantDashboard) {
        Write-Host "  Dashboard    http://localhost:$($script:DashboardPort)" -ForegroundColor Green
    }
    Write-Host ''
    Write-Host '  Useful commands' -ForegroundColor Yellow
    Write-Host "    See the runners     wsl -d $DistroName -u root -- docker ps"
    Write-Host "    Follow one runner   wsl -d $DistroName -u root -- docker logs -f nomercy-runner-1"
    Write-Host "    Stop one            wsl -d $DistroName -u root -- docker stop -t 60 nomercy-runner-1"
    Write-Host "    Remove everything   .\nomercy-github-runners-uninstall.ps1"
    Write-Host ''
    Write-Host '  Your own Docker was not touched. These runners use a separate' -ForegroundColor DarkGray
    Write-Host '  engine and a separate disk, so they cannot fill your storage.' -ForegroundColor DarkGray
    Write-Host ''
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

Write-Host ''
Write-Host '  NoMercy GitHub Actions Runners' -ForegroundColor Cyan
Write-Host '  Standalone installer for Windows' -ForegroundColor DarkCyan

Test-Preflight
Invoke-Wizard
Show-Summary

if (-not (Read-YesNo 'Proceed with the install' $true)) {
    Write-Host ''
    Write-Info 'Cancelled. Nothing was created.'
    exit 0
}

Install-Distro
Install-Engine
Install-Keepalive
New-Runners
if ($script:WantDashboard) { Install-Dashboard }
Show-NextSteps
exit 0
