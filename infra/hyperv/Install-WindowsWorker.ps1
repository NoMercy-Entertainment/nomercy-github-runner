<#
.SYNOPSIS
    Makes this host the platform's Windows worker (T-0701, as OPEN-2 decided):
    the agent as a service, enrolled with the controller. Run elevated, after
    New-RunnerPlatformVMs.ps1 and Initialize-RunnerPlatform.ps1.

.DESCRIPTION
    Installs, under C:\ProgramData\nomercy and nowhere else:
      - a Python of its own (the embeddable package, SHA-256 pinned), because
        this host's Python is a per-user Store app a service cannot run;
      - the agent from this repository's HEAD;
      - a verified copy of the NSSM the Windows runner already uses;
      - the Forgejo runner template with the traceably built binary;
      - the agent's certificate, issued by the controller at enrolment, and
        readable by SYSTEM and Administrators only;
      - a firewall rule letting the control plane, and only it, reach the
        agent on the host's rnr-internal address;
      - the service `rnr-agent`, running as LocalSystem, which it needs to
        make each runner's service.

    Changes nothing that runs. The Windows runner (`forgejo-runner`) and its
    files are only read, to copy nssm.exe. The agent makes nothing until the
    controller asks it to, and the controller builds nothing until an
    operator raises a fleet's capacity.

    Every step is idempotent; run it again to deploy a newer HEAD.
#>
[CmdletBinding(SupportsShouldProcess)]
param()
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings
$w = $s.Windows
if (-not (Test-Elevated)) { throw 'Run this elevated.' }

$hostIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $s.HostAddress }
if (-not $hostIp) { throw "$($s.HostAddress) is not on this host; run New-RunnerPlatformVMs.ps1 first." }
$cpName = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })[0]
$cp = $s.VMs[$cpName].Address

function Assert-Hash([string]$Path, [string]$Sha256) {
    $have = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
    if ($have -ne $Sha256.ToLower()) { throw "$Path is $have, not the pinned $Sha256" }
}

$root = $w.Root
$agentDir = Join-Path $root 'agent'
$pythonDir = Join-Path $agentDir 'python'
$appDir = Join-Path $agentDir 'app'
$tlsDir = Join-Path $agentDir 'tls'
$binDir = Join-Path $root 'bin'
$templates = Join-Path $root 'templates'
foreach ($d in $agentDir, $appDir, $tlsDir, $binDir, $templates) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# --- Python --------------------------------------------------------------------
$python = Join-Path $pythonDir 'python.exe'
if (-not (Test-Path $python)) {
    $zip = Join-Path $env:TEMP 'rnr-python-embed.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $w.PythonUrl -OutFile $zip
    Assert-Hash $zip $w.PythonSha256
    Expand-Archive -LiteralPath $zip -DestinationPath $pythonDir -Force
    Remove-Item $zip
}
# The embeddable package reads its search path from its ._pth file and
# ignores PYTHONPATH and the working directory; the agent's code is added there.
$pth = Get-ChildItem $pythonDir -Filter 'python*._pth' | Select-Object -First 1
$lines = Get-Content -LiteralPath $pth.FullName
if ($lines -notcontains '..\app') { Add-Content -LiteralPath $pth.FullName -Value '..\app' }

# --- the agent, from HEAD ---------------------------------------------------------
$version = (& git -C $script:RepoRoot rev-parse --short HEAD).Trim()
$tar = Join-Path $env:TEMP 'rnr-agent.tar'
& git -C $script:RepoRoot archive --format=tar -o $tar HEAD agent
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$fresh = Join-Path $agentDir 'app.new'
Remove-Item -Recurse -Force $fresh -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $fresh | Out-Null
& tar.exe -xf $tar -C $fresh
if ($LASTEXITCODE -ne 0) { throw 'extracting the agent failed' }
Remove-Item $tar

# --- NSSM and the template ---------------------------------------------------------
$nssm = Join-Path $binDir 'nssm.exe'
Assert-Hash $w.NssmSource $w.NssmSha256
if (-not (Test-Path $nssm)) { Copy-Item -LiteralPath $w.NssmSource -Destination $nssm }
Assert-Hash $nssm $w.NssmSha256

$template = Join-Path $templates $w.Template
New-Item -ItemType Directory -Force -Path $template | Out-Null
Copy-Item -Force -Path (Join-Path $PSScriptRoot "..\windows\templates\$($w.Template)\*") -Destination $template
Assert-Hash $w.RunnerBinary $w.RunnerSha256
Copy-Item -Force -LiteralPath $w.RunnerBinary -Destination (Join-Path $template 'forgejo-runner.exe')

# Each runner's job host runs this Python, as the runner's own virtual
# account: every service account may read and run it, and nothing more.
& icacls.exe $pythonDir /grant '*S-1-5-80-0:(OI)(CI)RX' /Q | Out-Null
& icacls.exe $fresh /grant '*S-1-5-80-0:(OI)(CI)RX' /Q | Out-Null

# --- enrolled by the controller ---------------------------------------------------
$endpoint = "https://$($s.HostAddress):$($s.AgentPort)"
Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $($w.HostId) hyperv-windows $endpoint" +
    " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$($w.HostId) /tmp/bundle" +
    " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar && sudo rm -rf /tmp/bundle") -Quiet
$bundle = Join-Path $env:TEMP 'rnr-bundle.tar'
Receive-FromGuest $s $cp '/tmp/bundle.tar' $bundle
Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet
& tar.exe -xf $bundle -C $tlsDir
Remove-Item $bundle
& icacls.exe $tlsDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null

# --- configuration ------------------------------------------------------------------
$config = [ordered]@{
    host_id    = $w.HostId
    runtime    = 'windows-process'
    listen     = "$($s.HostAddress):$($s.AgentPort)"
    controller = "https://${cp}:$($s.ReceiverPort)"
    tls        = @{ cert = (Join-Path $tlsDir 'agent.crt'); key = (Join-Path $tlsDir 'agent.key')
                    ca = (Join-Path $tlsDir 'ca.pem') }
    tools      = @{ nssm = $nssm; python = $python; templates = $templates }
    capacity   = @{ max_runners = $w.MaxRunners; memory_bytes = [int64]$w.RunnerMemGB * $w.MaxRunners * 1GB }
    version    = $version
}
$configPath = Join-Path $agentDir 'agent.json'
Write-LfFile $configPath ($config | ConvertTo-Json -Depth 4)
& icacls.exe $configPath /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' /Q | Out-Null

# --- the firewall -------------------------------------------------------------------
$rule = 'rnr-agent (control plane only)'
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $s.HostAddress -LocalPort $s.AgentPort -RemoteAddress $cp | Out-Null

# --- the service ----------------------------------------------------------------------
$service = 'rnr-agent'
$existing = Get-Service -Name $service -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq 'Running') { & $nssm stop $service | Out-Null }
# Swap in the new code only while the agent is stopped. Runners keep running:
# they are services of their own, each with its own job host.
if (Test-Path $appDir) { Remove-Item -Recurse -Force $appDir }
Move-Item -LiteralPath $fresh -Destination $appDir
if (-not $existing) {
    & $nssm install $service $python '-m' 'agent' '--config' $configPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'nssm install failed' }
}
$log = Join-Path $agentDir 'agent.log'
foreach ($setting in @(@('AppDirectory', $appDir), @('AppStdout', $log), @('AppStderr', $log),
                       @('AppStopMethodConsole', '15000'), @('Start', 'SERVICE_AUTO_START'),
                       @('DisplayName', 'Runner platform agent'))) {
    & $nssm set $service @setting | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm set $($setting[0]) failed" }
}
& $nssm start $service | Out-Null
Start-Sleep -Seconds 3
Get-Service $service | Format-Table Name, Status -AutoSize | Out-String | Write-Host
Get-Content -LiteralPath $log -Tail 3 -ErrorAction SilentlyContinue

$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 5
    $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
} until ($status -match "(?m)^\s+$($w.HostId)\s+\S+\s+healthy" -or (Get-Date) -gt $deadline)
Write-Host $status
if ($status -notmatch "(?m)^\s+$($w.HostId)\s+\S+\s+healthy") { throw "$($w.HostId) did not report healthy within 60 s" }
Write-Host "The Windows worker is joined up at $version."
