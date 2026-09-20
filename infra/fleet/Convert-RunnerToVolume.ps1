<#
.SYNOPSIS
    Move one live GitHub runner's nested engine onto its own volume
    (fuse-overlayfs in the container's layer -> overlay2 on ext4), without
    ever aborting a job.

.DESCRIPTION
    Why: a runner whose nested engine lives in its own writable layer grows
    until plain `df -h` in a job hangs for hours. github-runner-1 did that
    three times before it was converted on 2026-09-19; see the evidence file
    and docker-compose.runners.yml, which already declares a volume per runner.

    The job is protected the way design 13.1 drains a GitHub runner: its
    custom labels are taken off at GitHub, so no new job can be routed to it,
    and only when GitHub and the runner itself both say it has no job is the
    container touched. Nothing is signalled while it works. If anything fails
    before the container is stopped, the labels go back.

    Then, because these containers were made by the dashboard and carry no
    compose labels, compose cannot replace them in place: the old one is
    renamed and stopped - stopping deregisters it at GitHub - compose creates
    the new one, its cpuset is reapplied (a recreate always drops it), and the
    old container is removed only once the new one is listening for jobs. If
    the new one does not come up, the old one is put back and started.

    One runner per run, and no other container is touched.

.EXAMPLE
    .\Convert-RunnerToVolume.ps1 -Runner github-runner-2
    .\Convert-RunnerToVolume.ps1 -Runner github-runner-3 -DrainMinutes 120
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^github-runner-\d+$')] [string] $Runner,
    [int] $DrainMinutes = 60,
    # Empty the old nested engine while the runner is drained but still up.
    # Its images and build cache go with the old layer anyway, and deleting
    # them here keeps that deletion off the critical path: for a runner
    # compose manages, the old container must be gone before the new one is
    # made, and that wait was 8 minutes for github-runner-3.
    [switch] $PruneFirst,
    [string] $Distro = 'github-runners',
    [string] $ComposeFile = '/mnt/d/docker-compose/GithubRunners/docker-compose.runners.yml',
    [string] $Project = 'githubrunners'
)
$ErrorActionPreference = 'Stop'
$repo = 'D:\docker-compose\GithubRunners'

function Distro { param([string[]] $Arguments) & wsl.exe -d $Distro -u root -- @Arguments }
function DistroOk {
    param([string[]] $Arguments)
    $out = Distro $Arguments
    if ($LASTEXITCODE -ne 0) { throw "in the distro: '$($Arguments -join ' ')' failed: $out" }
    $out
}

$org = ((Get-Content "$repo\.env" | Where-Object { $_ -match '^\s*GITHUB_ORG\s*=' }) -split '=', 2)[1].Trim()
if (-not $org) { throw 'GITHUB_ORG is not set in .env' }
# The deployment's own token, which may administer the org's runners - the
# `gh` login on this host may not. It is set for this process only and never
# printed; gh prefers GH_TOKEN over its stored credentials.
$env:GH_TOKEN = ((Get-Content "$repo\.env" | Where-Object { $_ -match '^\s*GH_TOKEN\s*=' }) -split '=', 2)[1].Trim().Trim('"')
if (-not $env:GH_TOKEN) { throw 'GH_TOKEN is not set in .env' }

# --- what we are about to touch ------------------------------------------------
$state = Distro @('docker', 'inspect', '-f', '{{.State.Running}} {{.HostConfig.CpusetCpus}}', $Runner)
if ($LASTEXITCODE -ne 0) { throw "$Runner does not exist on this engine" }
$running, $cpuset = ($state -split ' ', 2)
if ($running -ne 'true') { throw "$Runner is not running; convert it by hand or start it first" }
# Runners 1-6 are compose services; the ones the dashboard added are not, and
# for those the dashboard's own `docker_ops.create` makes the replacement - the
# same code the "+ Add runner" button uses, which has given every runner it
# makes a volume since 2026-09-17.
$byCompose = (Get-Content "$repo\docker-compose.runners.yml" -Raw) -match "(?m)^\s{2}$([regex]::Escape($Runner)):"
$index = [int]($Runner -replace '\D')
Write-Host ("$Runner will be made again by {0}" -f $(if ($byCompose) { 'compose' } else { "the dashboard (index $index)" }))
$volume = Distro @('docker', 'inspect', '-f',
    '{{range .Mounts}}{{if eq .Destination "/var/lib/docker"}}{{.Name}}{{end}}{{end}}', $Runner)
if ($volume) { Write-Host "$Runner already has a volume ($volume); nothing to do."; return }

$dotRunner = DistroOk @('docker', 'exec', $Runner, 'cat', '/root/actions-runner/.runner') | Out-String
# The runner writes .runner with a byte-order mark, which ConvertFrom-Json
# refuses.
$registration = ($dotRunner.Trim() -replace "^﻿", '') | ConvertFrom-Json
$id, $name = $registration.agentId, $registration.agentName
Write-Host "$Runner is registered at $org as $name (id $id); cpuset $cpuset"

# --- drain it at GitHub -----------------------------------------------------------
$before = (gh api "orgs/$org/actions/runners/$id/labels" --jq '[.labels[] | select(.type == "custom") | .name]' | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw "GitHub did not say which labels $name has" }
Write-Host "custom labels: $($before -join ', ')"
if (-not $before) {
    Write-Warning "no custom labels to take off: a job asking only for self-hosted, Linux or X64 can still reach it while we wait"
} else {
    gh api -X DELETE "orgs/$org/actions/runners/$id/labels" --silent
    if ($LASTEXITCODE -ne 0) { throw "GitHub did not take the labels off $name" }
    Write-Host "labels off: GitHub sends it no new job"
}

function Restore-Labels {
    if (-not $before) { return }
    $call = @('api', '-X', 'POST', "orgs/$org/actions/runners/$id/labels")
    foreach ($label in $before) { $call += @('-f', "labels[]=$label") }
    & gh @call --silent 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "labels put back: $($before -join ', ')" }
    else { Write-Warning "could not put the labels back on $name; add $($before -join ', ') by hand" }
}

try {
    $deadline = (Get-Date).AddMinutes($DrainMinutes)
    while ($true) {
        $busyAtForge = gh api "orgs/$org/actions/runners/$id" --jq '.busy'
        $workers = (Distro @('docker', 'exec', $Runner, 'sh', '-c', 'pgrep -c Runner.Worker || true')).Trim()
        if ($busyAtForge -eq 'false' -and $workers -eq '0') { break }
        if ((Get-Date) -gt $deadline) {
            throw "it still has a job after $DrainMinutes minutes (GitHub busy=$busyAtForge, workers=$workers)"
        }
        Write-Host ("  waiting for its job: GitHub busy={0}, workers={1}" -f $busyAtForge, $workers)
        Start-Sleep -Seconds 20
    }
    Write-Host 'it has no job; converting'
}
catch {
    Restore-Labels
    throw
}

if ($PruneFirst) {
    Write-Host 'emptying the old nested engine (its cache goes with the old layer anyway)...'
    Distro @('docker', 'exec', $Runner, 'sh', '-c',
        'timeout 900 docker system prune -af --volumes >/dev/null 2>&1; timeout 300 docker builder prune -af >/dev/null 2>&1; true') | Out-Null
    Write-Host '  emptied'
}

# --- convert -------------------------------------------------------------------------
$old = "$Runner-old"
DistroOk @('docker', 'rename', $Runner, $old) | Out-Null
Write-Host 'stopping the old one (it deregisters itself at GitHub)...'
Distro @('docker', 'stop', '-t', '60', $old) | Out-Null

# The old runner had its own five seconds to deregister while it stopped, and
# on this fleet that is not enough: `config.sh remove` times out and leaves an
# offline record behind at GitHub for every conversion. Its container is gone
# by now and its replacement registers afresh, so the record is deleted here.
gh api -X DELETE "orgs/$org/actions/runners/$id" --silent 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "old registration $name deleted at GitHub" }
else { Write-Warning "could not delete the old registration $name (id $id); it will sit there offline" }

function Restore-Container {
    param([string] $Why)
    Write-Warning "$Why - putting the old $Runner back"
    Distro @('docker', 'rm', '-f', $Runner) | Out-Null
    Distro @('docker', 'rename', $old, $Runner) | Out-Null
    Distro @('docker', 'start', $Runner) | Out-Null
    throw "$Why (the old $Runner is running again; it registers afresh with its labels)"
}

if ($byCompose) {
    Distro @('docker', 'compose', '-p', $Project, '-f', $ComposeFile, 'up', '-d', '--no-deps', $Runner) | Out-Null
} else {
    $made = Distro @('docker', 'exec', 'runner-dashboard', 'python', '-c',
        "from app import read_env; import docker_ops; print(docker_ops.create($index, read_env()))")
    Write-Host "  dashboard: $made"
}
Distro @('docker', 'inspect', $Runner) | Out-Null
if ($LASTEXITCODE -ne 0) { Restore-Container 'the replacement was not created' }
if ($cpuset) {
    Distro @('docker', 'update', '--cpuset-cpus', $cpuset, $Runner) | Out-Null
    Write-Host "cpuset $cpuset put back"
}

$listening = $false
foreach ($i in 1..90) {
    $log = Distro @('docker', 'logs', $Runner) 2>&1 | Out-String
    if ($log -match 'Listening for Jobs') { $listening = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $listening) { Restore-Container 'the new one did not start listening within 3 minutes' }

$log = (Distro @('docker', 'logs', $Runner) 2>&1 | Out-String) -split "`n" |
    Where-Object { $_ -match 'Nested Docker|Registering|Listening for Jobs' } | Select-Object -Last 3
$log | ForEach-Object { Write-Host "  $_" }
# The new container registered afresh, with the labels from the environment.
# Checked rather than assumed: a runner whose custom label is missing looks
# healthy at GitHub and never receives a job.
$new = (DistroOk @('docker', 'exec', $Runner, 'cat', '/root/actions-runner/.runner') | Out-String)
$newId = (($new.Trim() -replace "^﻿", '') | ConvertFrom-Json).agentId
$now = gh api "orgs/$org/actions/runners/$newId/labels" --jq '[.labels[] | select(.type == "custom") | .name]' | ConvertFrom-Json
$missing = @($before | Where-Object { $_ -notin $now })
if ($missing) {
    Write-Warning "the new registration is missing $($missing -join ', '); adding them"
    $call = @('api', '-X', 'POST', "orgs/$org/actions/runners/$newId/labels")
    foreach ($label in $missing) { $call += @('-f', "labels[]=$label") }
    & gh @call --silent
}
Write-Host "new registration id $newId with labels: $($now -join ', ')"

# Compose-managed runners have no old container left here: compose removed it
# itself before making the new one, which is why their downtime is longer.
if ((Distro @('docker', 'ps', '-aq', '--filter', "name=^$old$"))) {
    Write-Host 'removing the old container (its layer with it)...'
    Distro @('docker', 'rm', $old) | Out-Null
}
Distro @('docker', 'inspect', '-f',
    'new: {{range .Mounts}}{{.Destination}}={{.Type}} {{end}}cpuset={{.HostConfig.CpusetCpus}}', $Runner)
Write-Host "$Runner is converted."
