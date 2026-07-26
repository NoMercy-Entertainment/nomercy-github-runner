# Provision the dedicated WSL distro and Docker Engine for the GitHub Actions
# runners, with its virtual disk on D: so the runners share no storage with the
# BeastStack production stack running on Docker Desktop.
#
# Idempotent-ish: halts rather than overwriting if the distro already exists.
# Run from an elevated PowerShell prompt.
#
#   .\scripts\provision-distro.ps1
#
# Verified working 2026-07-26 against WSL 2.7.11.0 / Ubuntu 24.04 LTS.

$ErrorActionPreference = 'Stop'

$DistroName   = 'github-runners'
$DistroImage  = 'Ubuntu-24.04'
$InstallPath  = 'D:\Docker\GithubRunners\Data'
$RepoRoot     = Split-Path -Parent $PSScriptRoot

# WSL emits UTF-16; decode it so PowerShell string matching works.
function Invoke-Wsl {
    param([string[]]$WslArgs)
    (& wsl.exe @WslArgs 2>&1 | Out-String) -replace "`0", ""
}

Write-Host '== checking preconditions ==' -ForegroundColor Cyan

$existing = Invoke-Wsl @('--list', '--quiet')
if ($existing -match [regex]::Escape($DistroName)) {
    throw "Distro '$DistroName' already exists. Refusing to overwrite it. " +
          "Remove it deliberately with 'wsl --unregister $DistroName' if that is what you want."
}

# Run the runner containers' own Docker daemon on a disk that is not
# docker_data.vhdx. If this lands anywhere else the whole point is lost.
New-Item -ItemType Directory -Force $InstallPath | Out-Null

Write-Host "== installing $DistroImage as '$DistroName' at $InstallPath ==" -ForegroundColor Cyan
Invoke-Wsl @('--install', $DistroImage, '--name', $DistroName, '--location', $InstallPath, '--no-launch')

$vhdx = Get-ChildItem $InstallPath -Filter *.vhdx -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $vhdx) {
    throw "No .vhdx found under $InstallPath. The distro did not install where it was told to; " +
          "do not continue, the isolation requirement is unmet."
}
Write-Host "   virtual disk: $($vhdx.FullName)" -ForegroundColor Green

Write-Host '== enabling systemd ==' -ForegroundColor Cyan
Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c', "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf")
Invoke-Wsl @('--terminate', $DistroName)
Start-Sleep -Seconds 3
Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'systemctl', 'is-system-running', '--wait')

Write-Host '== installing Docker Engine ==' -ForegroundColor Cyan
# Pass the installer as a file rather than an inline string: quoting a bash
# script through PowerShell -> wsl.exe mangles $(...) and redirections.
# tr -d '\r' guards against CRLF line endings from the Windows filesystem.
$installer = Join-Path $RepoRoot 'scripts\install-docker.sh'
if (-not (Test-Path $installer)) { throw "Missing $installer" }
$wslInstaller = '/mnt/' + $installer.Substring(0,1).ToLower() + ($installer.Substring(2) -replace '\\','/')
Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'bash', '-c',
             "tr -d '\r' < $wslInstaller > /tmp/install-docker.sh && bash /tmp/install-docker.sh")

Write-Host '== verifying the daemon survives a restart ==' -ForegroundColor Cyan
# This is what makes the boot task viable: dockerd must come back with no
# manual intervention, or the runners will not return after a reboot.
Invoke-Wsl @('--terminate', $DistroName)
Start-Sleep -Seconds 3
$info = Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'info', '--format',
                     'server={{.ServerVersion}} storage={{.Driver}} root={{.DockerRootDir}}')
Write-Host "   $($info.Trim())" -ForegroundColor Green

Write-Host '== isolation check ==' -ForegroundColor Cyan
$newEngine = (Invoke-Wsl @('-d', $DistroName, '-u', 'root', '--', 'docker', 'ps', '-aq')).Trim()
if ($newEngine) { throw "New engine unexpectedly already has containers: $newEngine" }
Write-Host '   new engine has no containers, as expected' -ForegroundColor Green
Write-Host "   Docker Desktop containers: $((docker ps -q | Measure-Object -Line).Lines)" -ForegroundColor Green

Write-Host ''
Write-Host "Done. The runners' engine is at '$DistroName', disk on D:, separate from Docker Desktop." -ForegroundColor Green
Write-Host "Manage it with:  wsl -d $DistroName -u root -- docker ps" -ForegroundColor Yellow
