<#
.SYNOPSIS
    Put the control agent in the WSL distro, so the fleet that runs there can
    be adopted by the controller. No elevation.

.DESCRIPTION
    The ten GitHub runners and three Forgejo runners that serve today are
    containers on the engine inside the `github-runners` distro. That distro
    is a worker like any other - its own engine, its own units - and this
    installs the agent in it, enrolled with the controller, as a systemd
    service.

    Nothing about the running fleet changes here. The agent only serves the
    control verbs; adopting the runners is the separate step afterwards, and
    adopting does not rebuild, re-register or restart anything.

    The control plane cannot reach the distro's network directly: WSL's
    addresses are behind the host. `Publish-WslAgent.ps1` bridges that hop
    with a portproxy, and must be run elevated once (and again whenever WSL
    restarts, which changes the distro's address).

.EXAMPLE
    .\Install-WslAgent.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Distro = 'github-runners',
    [string] $HostId = 'wsl-linux-1',
    # What the agent listens on inside the distro, and what the controller
    # dials on the host - the internal switch address it already reaches the
    # Windows worker on, on a port of its own.
    [int]    $AgentPort = 8443,
    [int]    $PublishedPort = 8453,
    # What this worker declares it can hold: the thirteen runners that are
    # already there, and the memory ceiling each of them has.
    [int]    $MaxRunners = 16,
    [int64]  $RunnerMemoryBytes = 32GB
)
$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'hyperv') 'lib.ps1')
$s = Get-RunnerPlatformSettings
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$control = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })
if ($control.Count -ne 1) { throw 'settings.psd1 must name exactly one control-plane VM' }
$cp = $s.VMs[$control[0]].Address
$version = (& git -C $repo rev-parse --short HEAD 2>$null)
if (-not $version) { $version = 'unknown' }

function Distro([string] $Command, [switch] $Quiet) {
    $out = & wsl.exe -d $Distro -u root -- bash -lc $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "in ${Distro}: '$Command' failed ($LASTEXITCODE): $($out | Out-String)"
    }
    if (-not $Quiet) { $out }
}

$address = (Distro "hostname -I") -join ' '
$wslIp = ($address -split '\s+' | Where-Object {
    $_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -notmatch '^172\.1[78]\.' -and $_ -ne '127.0.0.1'
} | Select-Object -First 1)
if (-not $wslIp) { throw "no usable address in '$address'" }
Write-Host "$Distro is at $wslIp; the controller will dial $($s.HostAddress):$PublishedPort"

if (-not $PSCmdlet.ShouldProcess($Distro, 'install the control agent')) { return }

# --- enrolled by the controller, which issues and pins its certificate --------
# The endpoint is the host's internal-switch address, where the portproxy
# listens: a certificate is checked by subject and fingerprint, never by
# address, so the hop in between changes nothing.
$endpoint = "https://$($s.HostAddress):$PublishedPort"
Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $HostId hyperv-linux $endpoint" +
    " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$HostId /tmp/bundle" +
    " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar" +
    " && sudo rm -rf /tmp/bundle") -Quiet

$stage = Join-Path $s.Root "stage\$HostId"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Receive-FromGuest $s $cp '/tmp/bundle.tar' (Join-Path $stage 'bundle.tar')
Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet

$agentConfig = [ordered]@{
    host_id    = $HostId
    runtime    = 'linux-container'
    listen     = "${wslIp}:$AgentPort"
    controller = "https://${cp}:$($s.ReceiverPort)"
    tls        = @{ cert = '/etc/runner-agent/agent.crt'; key = '/etc/runner-agent/agent.key'
                    ca = '/etc/runner-agent/ca.pem' }
    capacity   = @{ max_runners = $MaxRunners
                    memory_bytes = [int64]$RunnerMemoryBytes * $MaxRunners }
    version    = $version
}
Write-LfFile (Join-Path $stage 'agent.json') ($agentConfig | ConvertTo-Json -Depth 4)
Write-LfFile (Join-Path $stage 'VERSION') "$version`n"
Write-LfFile (Join-Path $stage 'runner-agent.service') (Get-Content -Raw `
    (Join-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'hyperv') 'guest\runner-agent.service'))
Write-LfFile (Join-Path $stage 'setup.sh') (Get-Content -Raw `
    (Join-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'hyperv') 'guest\setup-wsl-agent.sh'))

$tar = Join-Path $s.Root 'stage\agent-code.tar'
& tar -C $repo -cf $tar --exclude='__pycache__' --exclude='tests' agent images/linux/unit
if ($LASTEXITCODE -ne 0) { throw 'could not pack the agent' }

# What this worker's unit images are built from: the images its runners run
# today. The unit image adds the three entry points the agent drives, which
# is what makes a rebuilt runner drivable at all.
Write-LfFile (Join-Path $stage 'BASE_github') "$($s.GitHub.BaseImage)`n"
Write-LfFile (Join-Path $stage 'BASE_forgejo') "$($s.Forgejo.BaseImage)`n"

# --- into the distro ------------------------------------------------------------
$linuxStage = '/tmp/rnr-stage'
Distro "rm -rf $linuxStage && mkdir -p -m 700 $linuxStage/bundle" -Quiet
$here = (ConvertTo-WslPath $stage)
$code = (ConvertTo-WslPath $tar)
Distro "cp '$code' $linuxStage/code.tar && cp '$here'/* $linuxStage/ && cd $linuxStage && tar -xf code.tar && tar -C bundle -xf bundle.tar" -Quiet
Write-Host (Distro "cd $linuxStage && bash setup.sh")
Distro "rm -rf $linuxStage" -Quiet
Remove-Item -Force (Join-Path $stage 'bundle.tar')

Write-Host ""
Write-Host "The agent serves on ${wslIp}:$AgentPort inside $Distro."
Write-Host "Now, elevated, so the controller can reach it:"
Write-Host "  .\infra\fleet\Publish-WslAgent.ps1"
