<#
.SYNOPSIS
    Puts the software on the runner-platform VMs and joins them up, over SSH.
    Not elevated. Run after New-RunnerPlatformVMs.ps1; safe to run again.

.DESCRIPTION
    1. Waits for each guest to answer and finish cloud-init.
    2. Control plane: Docker, the controller image built from this
       repository's committed code (HEAD), its settings, its authority
       (`python -m control init-pki`, the key never leaves the VM), and the
       controller running.
    3. Each Linux worker: enrolled by the controller - its certificate issued
       and pinned there - then Docker, the agent as a service with that
       certificate, the unit image built from the same commit, and a firewall
       letting in only the control plane's calls and the host's SSH.
    4. Waits for every worker to report healthy to the controller.

    **What leaves the host for the control plane is only what the controller
    needs** from .env: FORGEJO_INSTANCE_URL and FORGEJO_API_TOKEN, so it can
    mint registration tokens and delete records. OIDC, GH_TOKEN and the rest
    stay. The file is written 0600 on the guest and deleted here afterwards.

    **The pilot takes no production job.** FORGEJO_RUNNER_LABELS for the
    controller is `-PilotLabel`: Forgejo matches a job to a runner by label
    name, and no workflow asks for that one. The GitHub cell is not set up:
    a GitHub runner always carries self-hosted, Linux and X64, which a
    production job could ask for (spec 13.1) - that waits for a runner group
    no repository may use.

    Nothing is scaled. `docker exec rnr-controller python -m control capacity
    forgejo-linux-x64 1` on the control plane is the first runner.
#>
[CmdletBinding()]
param(
    # Only the control plane: its image rebuilt from HEAD, its settings
    # rewritten, its controller restarted. The workers keep the certificates
    # they were enrolled with, and their agents are left alone.
    [switch] $ControlPlaneOnly,
    [string] $PilotLabel = 'rnr-pilot:docker://node:20',
    [string] $EnvFile = (Join-Path (Resolve-Path "$PSScriptRoot\..\..").Path '.env')
)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

$control = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })
$workers = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'linux-worker' } | Sort-Object)
if ($control.Count -ne 1) { throw 'settings.psd1 must name exactly one control-plane VM' }
$cp = $s.VMs[$control[0]].Address

$version = (& git -C $script:RepoRoot rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'git rev-parse failed' }
$dirty = & git -C $script:RepoRoot status --porcelain -- dashboard agent images/linux/unit infra
if ($dirty) { Write-Warning "uncommitted changes are not deployed; HEAD $version is" }

$stage = Join-Path $s.Root 'stage'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
    # --- what goes over ---------------------------------------------------------
    $tar = Join-Path $stage 'code.tar'
    & git -C $script:RepoRoot archive --format=tar -o $tar HEAD dashboard agent images/linux/unit scripts/install-docker.sh
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }

    $wanted = 'FORGEJO_INSTANCE_URL', 'FORGEJO_API_TOKEN'
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$' -and $wanted -contains $Matches[1]) {
            $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    foreach ($k in $wanted) { if (-not $values[$k]) { throw "$k is not set in $EnvFile" } }
    # Each unit is held to what a worker declared room for per runner, so one
    # runaway job cannot take its worker - and the agent on it - down.
    $unitMemGB = ($workers | ForEach-Object { $s.VMs[$_].RunnerMemGB } | Measure-Object -Minimum).Minimum
    $win = $s.Windows
    $controllerEnv = ($wanted | ForEach-Object { "$_=$($values[$_])" }) + @(
        "FORGEJO_RUNNER_LABELS=$PilotLabel",
        "RUNNER_UNIT_IMAGE_FORGEJO_LINUX=nomercy/runner-unit-forgejo:$version",
        "RUNNER_UNIT_MEMORY_FORGEJO_LINUX=${unitMemGB}g",
        # The Windows cell (Install-WindowsWorker.ps1): available once its
        # self-built artefact is named, units made from its template.
        "FORGEJO_RUNNER_ARTIFACT_WINDOWS=$($win.Template) sha256:$($win.RunnerSha256)",
        "FORGEJO_RUNNER_LABELS_WINDOWS=$($win.PilotLabel)",
        "RUNNER_UNIT_IMAGE_FORGEJO_WINDOWS=$($win.Template)",
        "RUNNER_UNIT_MEMORY_FORGEJO_WINDOWS=$($win.RunnerMemGB)g",
        "CONTROL_INTERVAL=15")

    # --- the control plane ------------------------------------------------------
    Write-Host "waiting for $($control[0]) ($cp)..."
    Wait-Guest $s $cp
    $cpStage = Join-Path $stage 'control'
    New-Item -ItemType Directory -Force -Path $cpStage | Out-Null
    Write-LfFile (Join-Path $cpStage 'controller.env') (($controllerEnv -join "`n") + "`n")
    Write-LfFile (Join-Path $cpStage 'controller-compose.yml') (Expand-Template `
        (Join-Path $PSScriptRoot 'guest\controller-compose.yml') @{ ADDRESS = $cp })
    Write-LfFile (Join-Path $cpStage 'VERSION') "$version`n"
    Write-LfFile (Join-Path $cpStage 'setup.sh') (Get-Content -Raw (Join-Path $PSScriptRoot 'guest\setup-control-plane.sh'))

    Invoke-Guest $s $cp 'rm -rf /tmp/rnr-stage && mkdir -p -m 700 /tmp/rnr-stage' -Quiet
    Send-ToGuest $s $cp $tar '/tmp/rnr-stage/code.tar'
    foreach ($f in Get-ChildItem $cpStage) { Send-ToGuest $s $cp $f.FullName "/tmp/rnr-stage/$($f.Name)" }
    Invoke-Guest $s $cp 'cd /tmp/rnr-stage && tar -xf code.tar && sudo bash setup.sh'
    Invoke-Guest $s $cp 'sudo rm -rf /tmp/rnr-stage' -Quiet
    Remove-Item -Force (Join-Path $cpStage 'controller.env')

    if ($ControlPlaneOnly) {
        Write-Host (Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status')
        Write-Host "The control plane is at $version."
        return
    }

    # --- the forgejo base image, which is not public -----------------------------
    $baseTar = Join-Path $stage 'forgejo-base.tar'
    Invoke-InDistro $s @('docker', 'save', '-o', (ConvertTo-WslPath $baseTar),
        'ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest')

    # --- each worker ----------------------------------------------------------------
    foreach ($name in $workers) {
        $w = $s.VMs[$name]
        $endpoint = "https://$($w.Address):$($s.AgentPort)"
        Write-Host "waiting for $name ($($w.Address))..."
        Wait-Guest $s $w.Address

        # Enrolled by the controller, which issues and pins its certificate.
        Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $name hyperv-linux $endpoint" +
            " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$name /tmp/bundle" +
            " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar" +
            " && sudo rm -rf /tmp/bundle") -Quiet
        $wStage = Join-Path $stage $name
        New-Item -ItemType Directory -Force -Path $wStage | Out-Null
        Receive-FromGuest $s $cp '/tmp/bundle.tar' (Join-Path $wStage 'bundle.tar')
        Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet

        $agentConfig = [ordered]@{
            host_id    = $name
            runtime    = 'linux-container'
            listen     = "$($w.Address):$($s.AgentPort)"
            controller = "https://${cp}:$($s.ReceiverPort)"
            tls        = @{ cert = '/etc/runner-agent/agent.crt'; key = '/etc/runner-agent/agent.key'
                            ca = '/etc/runner-agent/ca.pem' }
            capacity   = @{ max_runners = $w.MaxRunners; memory_bytes = [int64]$w.RunnerMemGB * $w.MaxRunners * 1GB }
            version    = $version
        }
        Write-LfFile (Join-Path $wStage 'agent.json') ($agentConfig | ConvertTo-Json -Depth 4)
        Write-LfFile (Join-Path $wStage 'VERSION') "$version`n"
        Write-LfFile (Join-Path $wStage 'CONTROL_PLANE') "$cp`n"
        Write-LfFile (Join-Path $wStage 'HOST_ADDRESS') "$($s.HostAddress)`n"
        Write-LfFile (Join-Path $wStage 'AGENT_PORT') "$($s.AgentPort)`n"
        Write-LfFile (Join-Path $wStage 'runner-agent.service') (Get-Content -Raw (Join-Path $PSScriptRoot 'guest\runner-agent.service'))
        Write-LfFile (Join-Path $wStage 'setup.sh') (Get-Content -Raw (Join-Path $PSScriptRoot 'guest\setup-worker.sh'))

        Invoke-Guest $s $w.Address 'rm -rf /tmp/rnr-stage && mkdir -p -m 700 /tmp/rnr-stage/bundle' -Quiet
        Send-ToGuest $s $w.Address $tar '/tmp/rnr-stage/code.tar'
        Send-ToGuest $s $w.Address $baseTar '/tmp/rnr-stage/forgejo-base.tar'
        foreach ($f in Get-ChildItem $wStage) { Send-ToGuest $s $w.Address $f.FullName "/tmp/rnr-stage/$($f.Name)" }
        Invoke-Guest $s $w.Address ('cd /tmp/rnr-stage && tar -xf code.tar && tar -C bundle -xf bundle.tar' +
            ' && sudo bash setup.sh')
        Invoke-Guest $s $w.Address 'sudo rm -rf /tmp/rnr-stage' -Quiet
        Remove-Item -Force (Join-Path $wStage 'bundle.tar')
    }

    # --- joined up ------------------------------------------------------------------
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 5
        $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
        $healthy = @($workers | Where-Object { $status -match "(?m)^\s+$_\s+\S+\s+healthy" })
    } until ($healthy.Count -eq $workers.Count -or (Get-Date) -gt $deadline)
    Write-Host $status
    if ($healthy.Count -ne $workers.Count) { throw 'not every worker reported healthy within 90 s' }
    Write-Host "Joined up at $version. First runner, on the control plane:"
    Write-Host "  sudo docker exec rnr-controller python -m control capacity forgejo-linux-x64 1"
}
finally {
    # The stage held the controller's settings and a worker's key.
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
