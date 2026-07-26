# Keep the `github-runners` WSL distro alive.
#
# WHY THIS EXISTS
# ---------------
# WSL shuts a distro down once it goes idle. That stops docker.service, whose
# shutdown SIGTERMs every container, and the runners come back only when the
# next command happens to boot the distro again. Observed 2026-07-26 as a
# register -> SIGTERM -> restart loop roughly every 20-30 seconds, with
# journalctl showing a deliberate "Stopping docker.service" (NRestarts=0,
# Result=success) rather than a crash.
#
# Enabling systemd is NOT enough: dockerd running as a systemd service does not
# keep the distro alive. WSL needs a live session holding it open, which is what
# `wsl.exe ... sleep infinity` below provides. Docker Desktop solves the same
# problem for its own distro with a Windows service.
#
# Registered as a logon-triggered scheduled task by install-keepalive-task.ps1.
# Runs forever; if the hold ever drops (distro terminated, WSL restarted, host
# resumed from sleep) it re-establishes it.

$DistroName = 'github-runners'
$LogPath    = Join-Path $env:LOCALAPPDATA 'github-runners-keepalive.log'
$MaxLogKB   = 512

function Write-Log {
    param([string]$Message)
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $Message
    try {
        # Keep the log from growing without bound - this runs forever.
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt ($MaxLogKB * 1KB))) {
            Remove-Item $LogPath -Force -ErrorAction SilentlyContinue
        }
        Add-Content -Path $LogPath -Value $line -ErrorAction SilentlyContinue
    } catch { }
}

Write-Log "keepalive starting for '$DistroName'"

while ($true) {
    try {
        # Make sure dockerd is up before we settle in to hold the session.
        # systemd enables it, but a distro that just cold-booted may still be
        # coming up, and starting it is idempotent.
        & wsl.exe -d $DistroName -u root -- systemctl start docker 2>&1 | Out-Null

        Write-Log "holding distro open"

        # Blocks for as long as the distro lives. Returns when the distro is
        # terminated, WSL is restarted, or the host sleeps/resumes.
        & wsl.exe -d $DistroName -u root -- sleep infinity 2>&1 | Out-Null

        Write-Log "hold dropped (exit $LASTEXITCODE) - re-establishing"
    }
    catch {
        Write-Log "error: $($_.Exception.Message)"
    }

    # Brief pause so a persistently failing distro cannot spin this loop.
    Start-Sleep -Seconds 5
}
