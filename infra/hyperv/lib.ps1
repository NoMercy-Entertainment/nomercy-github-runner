# Shared by the runner-platform scripts. Dot-source it: . "$PSScriptRoot\lib.ps1"
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Ssh       = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$script:Scp       = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
$script:SshKeygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
$script:RepoRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Get-RunnerPlatformSettings {
    Import-PowerShellDataFile (Join-Path $PSScriptRoot 'settings.psd1')
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-WslPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    '/mnt/' + $full.Substring(0, 1).ToLower() + $full.Substring(2).Replace('\', '/')
}

function Invoke-InDistro {
    # A command in the distro whose engine does the conversions. Git Bash
    # mangles /mnt paths, which is why this goes through wsl.exe directly.
    param([Parameter(Mandatory)] $Settings, [Parameter(Mandatory)] [string[]] $Arguments)
    & wsl.exe -d $Settings.Distro -- @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "in $($Settings.Distro): '$($Arguments -join ' ')' failed ($LASTEXITCODE)"
    }
}

function Get-CommitHeadroomGB {
    $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    [math]::Round(($m.CommitLimit - $m.CommittedBytes) / 1GB, 1)
}

function Get-KeyPath($Settings) { Join-Path $Settings.Root 'ssh\id_ed25519' }

function Get-SshOptions($Settings) {
    @('-i', (Get-KeyPath $Settings),
      '-o', 'StrictHostKeyChecking=accept-new',
      '-o', ("UserKnownHostsFile=" + (Join-Path $Settings.Root 'ssh\known_hosts')),
      '-o', 'ConnectTimeout=10',
      '-o', 'BatchMode=yes')
}

function Invoke-Guest {
    # One command on a guest, as its admin. Output is returned; a non-zero exit
    # throws, naming the guest and the command.
    param([Parameter(Mandatory)] $Settings, [Parameter(Mandatory)] [string] $Address,
          [Parameter(Mandatory)] [string] $Command, [switch] $Quiet)
    $out = & $script:Ssh @(Get-SshOptions $Settings) "$($Settings.AdminUser)@$Address" $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "on ${Address}: '$Command' failed ($LASTEXITCODE): $($out | Out-String)"
    }
    if (-not $Quiet) { $out }
}

function Send-ToGuest {
    param([Parameter(Mandatory)] $Settings, [Parameter(Mandatory)] [string] $Address,
          [Parameter(Mandatory)] [string] $Source, [Parameter(Mandatory)] [string] $Destination)
    & $script:Scp @(Get-SshOptions $Settings) -q $Source "$($Settings.AdminUser)@${Address}:$Destination"
    if ($LASTEXITCODE -ne 0) { throw "copying $Source to ${Address}:$Destination failed" }
}

function Receive-FromGuest {
    param([Parameter(Mandatory)] $Settings, [Parameter(Mandatory)] [string] $Address,
          [Parameter(Mandatory)] [string] $Source, [Parameter(Mandatory)] [string] $Destination)
    & $script:Scp @(Get-SshOptions $Settings) -q "$($Settings.AdminUser)@${Address}:$Source" $Destination
    if ($LASTEXITCODE -ne 0) { throw "copying ${Address}:$Source here failed" }
}

function Wait-Guest {
    # Until the guest answers over SSH and cloud-init has finished with it.
    param([Parameter(Mandatory)] $Settings, [Parameter(Mandatory)] [string] $Address,
          [int] $TimeoutSeconds = 600)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        & $script:Ssh @(Get-SshOptions $Settings) "$($Settings.AdminUser)@$Address" 'cloud-init status --wait >/dev/null 2>&1; true' 2>$null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 5
    }
    throw "$Address did not answer over SSH within $TimeoutSeconds s"
}

function Expand-Template {
    param([Parameter(Mandatory)] [string] $Path, [Parameter(Mandatory)] [hashtable] $Values)
    $text = Get-Content -Raw -LiteralPath $Path
    foreach ($key in $Values.Keys) { $text = $text.Replace("{{$key}}", [string]$Values[$key]) }
    if ($text -match '\{\{[A-Z_]+\}\}') { throw "$Path still has a placeholder: $($Matches[0])" }
    $text
}

function Write-LfFile {
    # Guests read these; a carriage return in a YAML or unit file breaks them.
    param([Parameter(Mandatory)] [string] $Path, [Parameter(Mandatory)] [string] $Text)
    [IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
}
