<#
.SYNOPSIS
    Put the control agent on the machine that hosts the macOS appliance, so
    the runner inside it can be adopted (T-0802). No elevation.

.DESCRIPTION
    The macOS runner is a guest of its own machine - a Hyper-V VM running
    QEMU - and that guest forwards one port, its SSH. Its agent therefore
    runs on the machine, not inside the guest: from there it can reach both
    the guest and, later, the hypervisor side that boots it.

    This enrols that machine as a worker with the controller, which issues
    its certificate, and installs the agent as a systemd service with a
    configuration naming the guest. Nothing about the runner, its launchd
    job or its registration is touched: adopting it is a separate, later
    step (`python -m control adopt`).

    The machine is reached with the key the exporter already uses, and the
    guest with the password its image was built with - the same two
    credentials that were already on this host, neither of them new.

.EXAMPLE
    .\Install-ApplianceHost.ps1
    .\Install-ApplianceHost.ps1 -Address 172.19.136.46 -HostId macos-appliance-1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Address = '172.19.136.46',
    [string] $HostId = 'macos-appliance-1',
    [string] $User = 'runner',
    [string] $KeyPath = "$env:USERPROFILE\.ssh\macos_runner",
    # Where the guest's SSH is, from the machine itself: the port the QEMU
    # container publishes, and the account docker-osx's image ships with.
    [string] $GuestHost = '127.0.0.1',
    [int]    $GuestPort = 50922,
    [string] $GuestUser = 'user',
    [string] $GuestPassword = 'alpine',
    # What the guest can hold. Its QEMU is given 12 GB and the appliance runs
    # one runner today; the second (T-0804) is the GitHub one.
    [int]    $MaxRunners = 2,
    [int64]  $GuestMemoryBytes = 12GB,
    # T-0805 (W3a, design 10.5): one owned QEMU guest per runner instead of
    # one shared appliance (agent.runtimes.macos_pool). Off by default: a
    # worker installed without -AppliancePool gets today's single-appliance
    # configuration, unchanged.
    [switch] $AppliancePool,
    [string] $PoolImage,
    [string] $PoolBaseDisk,
    [string] $PoolBaseSystem
)
$ErrorActionPreference = 'Stop'
if ($AppliancePool -and $PoolImage -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "-AppliancePool requires -PoolImage to be a real sha256:<64 hex> image id, not '$PoolImage'"
}
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$control = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })
if ($control.Count -ne 1) { throw 'settings.psd1 must name exactly one control-plane VM' }
$cp = $s.VMs[$control[0]].Address
$version = (& git -C $repo rev-parse --short HEAD 2>$null)
if (-not $version) { $version = 'unknown' }

$ssh = @(Get-Command ssh.exe -ErrorAction SilentlyContinue)
$ssh = if ($ssh) { $ssh[0].Source } else { 'C:\Windows\System32\OpenSSH\ssh.exe' }
$scp = @(Get-Command scp.exe -ErrorAction SilentlyContinue)
$scp = if ($scp) { $scp[0].Source } else { 'C:\Windows\System32\OpenSSH\scp.exe' }
foreach ($tool in @($ssh, $scp)) {
    if (-not (Test-Path $tool)) { throw "no $tool on this host" }
}
$options = @('-i', $KeyPath, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')

function Invoke-ApplianceHost([string] $Command, [switch] $Quiet) {
    $out = & $ssh @options "$User@$Address" $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "on ${Address}: '$Command' failed ($LASTEXITCODE): $($out | Out-String)"
    }
    if (-not $Quiet) { $out }
}
function Send-ToApplianceHost([string] $Source, [string] $Destination) {
    & $scp @options -q $Source "$User@${Address}:$Destination"
    if ($LASTEXITCODE -ne 0) { throw "copying $Source to ${Address}:$Destination failed" }
}

Write-Host "appliance host $HostId at $Address, control plane $cp"
Invoke-ApplianceHost 'true' -Quiet

if (-not $PSCmdlet.ShouldProcess($Address, 'install the control agent')) { return }

# --- enrolled by the controller, which issues and pins its certificate --------
$endpoint = "https://${Address}:$($s.AgentPort)"
Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $HostId hyperv-linux $endpoint" +
    " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$HostId /tmp/bundle" +
    " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar" +
    " && sudo rm -rf /tmp/bundle") -Quiet

$stage = Join-Path $s.Root "stage\$HostId"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Receive-FromGuest $s $cp '/tmp/bundle.tar' (Join-Path $stage 'bundle.tar')
Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet

# --- what the agent is told ---------------------------------------------------
$agentConfig = [ordered]@{
    host_id    = $HostId
    runtime    = 'macos-appliance'
    listen     = "${Address}:$($s.AgentPort)"
    controller = "https://${cp}:$($s.ReceiverPort)"
    tls        = @{ cert = '/etc/runner-agent/agent.crt'; key = '/etc/runner-agent/agent.key'
                    ca = '/etc/runner-agent/ca.pem' }
    guest      = @{ host = $GuestHost; port = $GuestPort; user = $GuestUser
                    password_file = '/etc/runner-agent/guest.pass' }
    tools      = @{
        # launchd in this guest holds the runner as a system daemon, which
        # only root may ask about, and the guest's account may use sudo
        # without a password. The wrapper is that one indirection, in the
        # guest, so nothing here has to smuggle a command inside a path.
        launchctl     = '/usr/local/bin/rnr-launchctl'
        domain        = 'system'
        runner_user   = $GuestUser
        templates     = '/Users/runner/templates' 
        launch_agents = '/Library/LaunchDaemons'
    }
    # This legacy appliance runs listeners in one shared macOS guest. It
    # cannot enforce a RAM limit per listener, so advertise only the slot
    # count. A memory_bytes admission budget would strand the second runner
    # because neither listener has an enforceable per-runner reservation.
    capacity   = @{ max_runners = $MaxRunners }
    version    = $version
}
if ($AppliancePool) {
    # infra/hyperv/appliance_pool_config.py is the single source of these
    # fields (images/macos/pool/README.md); this only merges its JSON in.
    $poolJson = & python (Join-Path $PSScriptRoot 'appliance_pool_config.py') `
        --image $PoolImage --base-disk $PoolBaseDisk --base-system $PoolBaseSystem
    if ($LASTEXITCODE -ne 0) { throw 'appliance_pool_config.py failed to render the pool configuration' }
    $pool = $poolJson | ConvertFrom-Json
    $agentConfig['appliance_pool'] = $pool.appliance_pool
    $agentConfig['capacity'] = $pool.capacity
}
Write-LfFile (Join-Path $stage 'agent.json') ($agentConfig | ConvertTo-Json -Depth 4)
Write-LfFile (Join-Path $stage 'guest.pass') "$GuestPassword`n"
Write-LfFile (Join-Path $stage 'VERSION') "$version`n"
Write-LfFile (Join-Path $stage 'runner-agent.service') (Get-Content -Raw (Join-Path $PSScriptRoot 'guest\runner-agent.service'))
Write-LfFile (Join-Path $stage 'setup.sh') (Get-Content -Raw (Join-Path $PSScriptRoot 'guest\setup-appliance-host.sh'))

# --- the agent's code, as the repository has it --------------------------------
$tar = Join-Path $s.Root 'stage\agent-code.tar'
& tar -C $repo -cf $tar --exclude='__pycache__' --exclude='tests' agent
if ($LASTEXITCODE -ne 0) { throw 'could not pack the agent' }

# --- the one thing that is put inside the guest --------------------------------
# A wrapper that runs launchctl as root, because the runner is a system
# daemon there. Written once, owned by root, and it changes nothing about the
# runner: with it absent, the agent simply cannot ask launchd anything.
$wrapper = @'
printf '#!/bin/sh\nexec /usr/bin/sudo -n /bin/launchctl "$@"\n' > /tmp/rnr-launchctl
sudo -n install -m 755 -o root -g wheel /tmp/rnr-launchctl /usr/local/bin/rnr-launchctl
rm -f /tmp/rnr-launchctl
/usr/local/bin/rnr-launchctl print system/org.forgejo.runner > /dev/null && echo "wrapper ok"
# Where an instance keeps its own directories (agent/naming.py). The account
# this guest was built with is not the one the design names, so the tree is
# made once and given to it; every path under it is still derived from a
# runner_id and from nothing else.
sudo -n mkdir -p /Users/runner/runners /Users/runner/templates
sudo -n chown -R $(id -un):staff /Users/runner
ls -ld /Users/runner/runners
'@ -replace "`r`n", "`n"
$intoGuest = "SSHPASS='$GuestPassword' sshpass -e ssh -o StrictHostKeyChecking=no " +
             "-o PreferredAuthentications=password -o PubkeyAuthentication=no " +
             "-p $GuestPort $GuestUser@$GuestHost " + ("'" + ($wrapper -replace "'", "'\''") + "'")
Write-Host (Invoke-ApplianceHost $intoGuest)

# --- installed ------------------------------------------------------------------
Invoke-ApplianceHost 'rm -rf /tmp/rnr-stage && mkdir -p -m 700 /tmp/rnr-stage/bundle' -Quiet
Send-ToApplianceHost $tar '/tmp/rnr-stage/code.tar'
foreach ($f in Get-ChildItem $stage) { Send-ToApplianceHost $f.FullName "/tmp/rnr-stage/$($f.Name)" }
Write-Host (Invoke-ApplianceHost 'cd /tmp/rnr-stage && tar -xf code.tar && tar -C bundle -xf bundle.tar && sudo bash setup.sh')
Invoke-ApplianceHost 'sudo rm -rf /tmp/rnr-stage' -Quiet
Remove-Item -Force (Join-Path $stage 'bundle.tar'), (Join-Path $stage 'guest.pass')

# --- joined up ------------------------------------------------------------------
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 5
    $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
} until ($status -match "(?m)^\s+$HostId\s+\S+\s+healthy" -or (Get-Date) -gt $deadline)
Write-Host $status
if ($status -notmatch "(?m)^\s+$HostId\s+\S+\s+healthy") {
    throw "$HostId did not report healthy within 90 s"
}
Write-Host "The appliance host is a worker. Adopt the runner it holds with:"
Write-Host "  python -m control adopt forgejo-macos-x64 <name> $HostId org.forgejo.runner ``"
Write-Host "    --plist /Library/LaunchDaemons/org.forgejo.runner.plist"
