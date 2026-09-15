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

# The dashboard's LAN publication is re-established here as well, because the
# thing that breaks it is the same thing this loop already watches for: the
# distro going away and coming back on a different NAT address. See
# Publish-Dashboard below. Keep in step with publish-dashboard-lan.ps1, which
# stays the manual one-shot for setting this up by hand.
$DashPort     = 9200
$DashListenOn = '192.168.178.19'

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

function Test-DashboardListener {
    param([string]$Address, [int]$Port)

    # Check the LISTENER, not the portproxy rule. A registered rule proves
    # nothing: if IP Helper tried to bind before DHCP handed out $Address, the
    # rule sits there looking correct while nothing listens on it and every
    # connection is refused. That is exactly how the dashboard went dark on
    # 2026-09-10 - the rule was present and pointed at the right distro address
    # the whole time.
    [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
           Where-Object LocalAddress -eq $Address)
}

function Publish-Dashboard {
    param([string]$Distro, [string]$ListenOn, [int]$Port, [int]$Attempts = 12)

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $raw = ((& wsl.exe -d $Distro -u root -- hostname -I) -replace "`0", "").Trim()

            # hostname -I lists every address; the docker bridges (172.17/172.18)
            # are not the one to talk to.
            $wslIp = ($raw -split '\s+' |
                Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -notmatch '^172\.1[78]\.' -and $_ -ne '127.0.0.1' } |
                Select-Object -First 1)

            $haveListenAddr = [bool](Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                                     Where-Object IPAddress -eq $ListenOn)

            if ($wslIp -and $haveListenAddr) {
                # Rewrite unconditionally instead of parsing netsh output to work
                # out whether it needs changing. This runs only at boot and after
                # a dropped hold, so it is cheap, and one code path heals both
                # failures: a rule pointing at a dead distro address, and a rule
                # that never bound a listener.
                & netsh interface portproxy delete v4tov4 `
                        listenport=$Port listenaddress=$ListenOn 2>&1 | Out-Null
                & netsh interface portproxy add v4tov4 `
                        listenport=$Port listenaddress=$ListenOn `
                        connectport=$Port connectaddress=$wslIp 2>&1 | Out-Null

                if (Test-DashboardListener -Address $ListenOn -Port $Port) {
                    Write-Log "dashboard published: ${ListenOn}:${Port} -> ${wslIp}:${Port}"
                    return
                }
            }
        }
        catch {
            Write-Log "dashboard publish attempt ${i}: $($_.Exception.Message)"
        }

        # The usual reason an early attempt fails is the LAN address not being up
        # yet this soon after logon, so back off and retry rather than giving up
        # until the next time the hold drops - which could be days.
        Start-Sleep -Seconds 10
    }

    Write-Log "WARNING dashboard not published after $Attempts attempts - LAN/public access is down"
    Write-Log "         (needs elevation: run install-keepalive-task.ps1 to re-register with RunLevel Highest)"
}

Write-Log "keepalive starting for '$DistroName'"

while ($true) {
    try {
        # Make sure dockerd is up before we settle in to hold the session.
        # systemd enables it, but a distro that just cold-booted may still be
        # coming up, and starting it is idempotent.
        & wsl.exe -d $DistroName -u root -- systemctl start docker 2>&1 | Out-Null

        # Do this every pass, not just at startup. The distro's NAT address is
        # reassigned whenever WSL restarts, and this loop wakes up on exactly
        # that event, so it is the right place to re-point the portproxy.
        Publish-Dashboard -Distro $DistroName -ListenOn $DashListenOn -Port $DashPort

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
