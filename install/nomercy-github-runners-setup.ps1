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
    $raw = (& wsl.exe @WslArgs 2>&1 | Out-String)
    return ($raw -replace "`0", '')
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

# Install actions are added in the next task. Stopping here keeps this script
# honest: it either does the whole job or it does nothing, never half.
Write-Host ''
Write-Warn 'This build of the installer stops here (wizard only).'
Write-Info 'The install actions land in the next revision of this script.'
Write-Host ''
exit 0
