# NoMercy GitHub Actions Runners - Windows uninstaller
#
# Removes the runners, their isolated Docker engine, the WSL distribution and
# the keepalive task. Asks before deleting your data.
#
#   .\nomercy-github-runners-uninstall.ps1
#
# ASCII only - Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI.

[CmdletBinding()]
param(
    [string] $DistroName = 'nomercy-runners',
    [string] $Org,
    [string] $Token,
    [switch] $KeepData,
    [switch] $DeleteData,
    [switch] $NonInteractive
)

$ErrorActionPreference = 'Stop'

foreach ($p in 'DistroName','Org','Token') {
    $v = (Get-Variable -Name $p -ValueOnly -ErrorAction SilentlyContinue)
    if ($v -is [string]) { Set-Variable -Name $p -Value $v.Trim() }
}

function Write-Head($t) {
    Write-Host ''; Write-Host $t -ForegroundColor Cyan
    Write-Host ('-' * $t.Length) -ForegroundColor DarkCyan
}
function Write-Ok($t)   { Write-Host "  [ ok ] $t" -ForegroundColor Green }
function Write-Warn($t) { Write-Host "  [warn] $t" -ForegroundColor Yellow }
function Write-Info($t) { Write-Host "  $t" -ForegroundColor Gray }

function Invoke-Wsl {
    param([string[]] $WslArgs)
    # WSL writes warnings to stderr routinely, and with ErrorActionPreference
    # Stop those become terminating errors. An uninstaller that dies partway
    # is worse than one that never started.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return ((& wsl.exe @WslArgs 2>&1 | Out-String) -replace "`0", '') }
    finally { $ErrorActionPreference = $prev }
}

# Deregister through the API rather than trusting the container to do it on
# shutdown. The graceful path depends on the runner's start.sh handling
# SIGTERM correctly, and an installer cannot assume the version it fetched
# does. This is the fallback that makes the uninstall reliable regardless.
function Remove-RunnerViaApi {
    param([string] $Organisation, [string] $Pat, [string] $AgentName)
    if (-not $Organisation -or -not $Pat -or -not $AgentName) { return $false }
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    $h = @{
        Authorization          = "Bearer $Pat"
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent'           = 'nomercy-runner-uninstall'
    }
    try {
        $list = Invoke-RestMethod -Uri "https://api.github.com/orgs/$Organisation/actions/runners?per_page=100" `
                    -Headers $h -TimeoutSec 25
        $match = $list.runners | Where-Object { $_.name -eq $AgentName } | Select-Object -First 1
        if (-not $match) { return $true }   # already gone
        Invoke-RestMethod -Method Delete -Headers $h -TimeoutSec 25 `
            -Uri "https://api.github.com/orgs/$Organisation/actions/runners/$($match.id)" | Out-Null
        return $true
    } catch { return $false }
}

function Read-YesNo {
    param([string] $Prompt, [bool] $DefaultYes = $false)
    if ($NonInteractive) { return $DefaultYes }
    if ($DefaultYes) { $h = 'Y/n' } else { $h = 'y/N' }
    while ($true) {
        Write-Host ''
        Write-Host "  $Prompt [$h]" -ForegroundColor White
        $a = (Read-Host '  >').Trim().ToLower()
        if (-not $a) { return $DefaultYes }
        if ($a -in @('y','yes')) { return $true }
        if ($a -in @('n','no'))  { return $false }
    }
}

Write-Host ''
Write-Host '  NoMercy GitHub Actions Runners' -ForegroundColor Cyan
Write-Host '  Uninstaller' -ForegroundColor DarkCyan

# --------------------------------------------------------------------------
# find the install
# --------------------------------------------------------------------------

Write-Head 'Looking for the installation'

$distros = Invoke-Wsl @('--list', '--quiet')
if ($distros -notmatch [regex]::Escape($DistroName)) {
    Write-Warn "No WSL distribution named '$DistroName' was found."
    Write-Info 'Nothing to remove. If you used a different name, pass -DistroName.'
    Write-Host ''
    exit 0
}
Write-Ok "Found distribution '$DistroName'"

$runners = (Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'ps', '-a',
                         '--format', '{{.Names}}')).Trim()
$runnerList = @()
if ($runners) {
    $runnerList = @($runners -split "`n" | ForEach-Object { $_.Trim() } |
                    Where-Object { $_ -like 'nomercy-runner-*' })
}
Write-Info "Runners found: $($runnerList.Count)"

# Where the data lives, taken from the distro itself rather than guessed.
$dataPath = $null
$basePath = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss\*' `
             -ErrorAction SilentlyContinue |
             Where-Object { $_.DistributionName -eq $DistroName } |
             Select-Object -First 1).BasePath
if ($basePath) {
    $dataPath = $basePath -replace '^\\\\\?\\', ''
    Write-Info "Data location: $dataPath"
}

# --------------------------------------------------------------------------
# 1. deregister from GitHub, BEFORE removing anything
# --------------------------------------------------------------------------

Write-Head 'Deregistering runners from GitHub'
Write-Info 'This happens first. Removing the containers before deregistering'
Write-Info 'leaves dead entries in the organisation with no way to identify them.'

$failed = @()

if ($runnerList.Count -eq 0) {
    Write-Info 'No runner containers to deregister.'
} else {
    foreach ($r in $runnerList) {
        # Ask the runner what it is registered as, rather than assuming the
        # container name matches. The .runner file is written with a UTF-8
        # BOM, which strict JSON parsers reject.
        $raw = (Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'exec', $r,
                             'cat', '/root/actions-runner/.runner')).Trim()
        $agent = $null
        if ($raw) {
            try   { $agent = ($raw.TrimStart([char]0xFEFF) | ConvertFrom-Json).agentName }
            catch { $agent = $null }
        }

        if (-not $agent) {
            Write-Warn "$r - could not read its registration (it may already be deregistered)"
            continue
        }

        # Stopping the container runs start.sh's shutdown handler, which
        # deregisters cleanly using a proper removal token. -t 60 matters:
        # Engine 29.x sets StopTimeout to 1s, which kills deregistration
        # mid-flight and orphans the registration.
        Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'stop', '-t', '60', $r) | Out-Null

        $gone = $false
        $post = (Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'logs', '--tail', '20', $r))
        if ($post -match 'removal of runner .* succeeded' -or $post -match 'Runner removed successfully') {
            $gone = $true
        }

        if (-not $gone) {
            # The container did not deregister itself. Do it directly.
            if (-not $Token) {
                if (-not $NonInteractive) {
                    Write-Warn "$r ($agent) did not deregister itself."
                    Write-Info 'A token is needed to remove it from GitHub directly.'
                    $sec = Read-Host '  Token (input hidden, blank to skip)' -AsSecureString
                    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
                    try { $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($b) }
                    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
                }
            }
            if (-not $Org -and $Token -and -not $NonInteractive) {
                $Org = Read-Host '  Organisation'
            }
            if (Remove-RunnerViaApi -Organisation $Org -Pat $Token -AgentName $agent) {
                $gone = $true
                Write-Ok "$r ($agent) removed via the GitHub API"
            }
        }

        if ($gone) { Write-Ok "$r ($agent) deregistered" }
        else       { Write-Warn "$r ($agent) could not be deregistered"; $failed += $agent }
    }
}

# --------------------------------------------------------------------------
# 2. remove the containers
# --------------------------------------------------------------------------

Write-Head 'Removing containers'
foreach ($r in $runnerList) {
    Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'rm', '-f', $r) | Out-Null
    Write-Ok "Removed $r"
}
Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'rm', '-f',
             'nomercy-runner-dashboard') | Out-Null

# --------------------------------------------------------------------------
# 3. keepalive task
# --------------------------------------------------------------------------

Write-Head 'Removing the keepalive task'
$taskName = "NoMercy Runners - Keep $DistroName Alive"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask  -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Ok "Removed scheduled task"
} else {
    Write-Info 'No keepalive task found.'
}

# The script and its log outlived the task. Named per distro, so removing them
# leaves any other installation's alone.
$keepDir = Join-Path $env:LOCALAPPDATA 'NoMercyRunners'
foreach ($leftover in @("keepalive-$DistroName.ps1", "keepalive-$DistroName.log")) {
    $p = Join-Path $keepDir $leftover
    if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
}
# Only if this was the last one - the directory is shared between installations.
if ((Test-Path $keepDir) -and -not (Get-ChildItem $keepDir -Force)) {
    Remove-Item $keepDir -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------
# 4. the distribution (this removes the virtual disk with it)
# --------------------------------------------------------------------------

Write-Head 'Removing the WSL distribution'
Write-Warn "'wsl --unregister' deletes the distribution AND its virtual disk."
Write-Info 'Everything the runners stored - images, caches, workspaces - goes with it.'

$removeDistro = $true
if (-not $NonInteractive) {
    $removeDistro = Read-YesNo "Remove the distribution '$DistroName'" $true
}

if ($removeDistro) {
    Invoke-Wsl @('--terminate', $DistroName) | Out-Null
    Start-Sleep -Seconds 2
    $out = Invoke-Wsl @('--unregister', $DistroName)
    if ($LASTEXITCODE -eq 0) { Write-Ok "Unregistered '$DistroName'" }
    else { Write-Warn "Could not unregister the distribution: $($out.Trim())" }
} else {
    Write-Info "Left '$DistroName' in place."
}

# --------------------------------------------------------------------------
# 5. the data directory - only ever with explicit consent
# --------------------------------------------------------------------------

Write-Head 'Data directory'

if ($dataPath -and (Test-Path $dataPath)) {
    $sizeGb = 0
    try {
        $sizeGb = [math]::Round((Get-ChildItem $dataPath -Recurse -File -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum).Sum / 1GB, 1)
    } catch { }

    Write-Host ''
    Write-Host "    $dataPath   ($sizeGb GB)" -ForegroundColor Yellow
    Write-Host ''

    # Default is No. The operator chose this path; it might not be
    # exclusively ours, and deleting it is not reversible.
    $delete = $false
    if ($DeleteData) { $delete = $true }
    elseif ($KeepData -or $NonInteractive) { $delete = $false }
    else { $delete = Read-YesNo 'Delete this directory and everything in it' $false }

    if ($delete) {
        try {
            Remove-Item $dataPath -Recurse -Force -ErrorAction Stop
            Write-Ok 'Data directory deleted'
        } catch {
            Write-Warn "Could not delete it: $($_.Exception.Message)"
            Write-Info "Remove it by hand if you want it gone: $dataPath"
        }
    } else {
        Write-Ok 'Kept. Delete it yourself whenever you are ready.'
        Write-Info $dataPath
    }
} else {
    Write-Info 'No data directory found (the distribution removal took it).'
}

# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

Write-Head 'Done'
Write-Host ''
if ($failed.Count -gt 0) {
    Write-Warn 'These runners may still be listed in your organisation:'
    foreach ($f in $failed) { Write-Host "    $f" -ForegroundColor Red }
    Write-Host ''
    Write-Info 'Remove them at:'
    if ($Org) { Write-Info "  https://github.com/organizations/$Org/settings/actions/runners" }
    else      { Write-Info '  https://github.com/organizations/<your-org>/settings/actions/runners' }
} else {
    Write-Ok 'All runners were deregistered from GitHub.'
}
Write-Host ''
Write-Info 'Your own Docker was never touched by these runners, and is unaffected.'
Write-Host ''
