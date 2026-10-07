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
    operator adds a runner to a fleet.

    Every step is idempotent; run it again to deploy a newer HEAD. The code
    it replaces is kept beside it as app.previous, so going back is moving
    that directory back and starting the service.

    -WindowsStorage gives each runner created from then on its own fixed
    VHDX (docs/windows-runner-storage.md). A runner that already exists
    keeps its plain directory - the agent never adopts one - until it is
    recreated.

    -HostId, -ListenAddress, -TlsBundle, -NssmSource, -RunnerBinary,
    -MaxRunners and -RunnerMemGB let this install a worker that is not this
    host (T-23, W10b-2): a Hyper-V guest, carried into by
    Install-WindowsGuestWorker.ps1 over PowerShell Direct. Left out, every
    one of them defaults to exactly what this script already did -
    settings.psd1's 'Windows' block and this host's own rnr-internal address
    - so `.\Install-WindowsWorker.ps1 -WindowsStorage` still installs
    BEAST-UNIT. -TlsBundle also decides where the agent's code and
    certificate come from: given, a tar already holding the certificate is
    imported (and deleted once extracted) and the agent is copied from an
    already-exploded `git archive HEAD` tree beside this script, because a
    guest has neither the platform SSH key that enrolling needs nor a git
    checkout that `git archive` needs; left out, this script enrols over SSH
    and builds the agent from HEAD itself, exactly as before. The health
    check needs that same SSH key, so it is skipped, with a printed note,
    when -TlsBundle is given and the key is not on this machine. -TlsBundle
    requires -HostId alongside it - a guest must never silently enrol as
    BEAST-UNIT.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch] $WindowsStorage,
    [string] $StorageRoot = 'D:\runner-disks',
    [string] $HostId,
    [string] $ListenAddress,
    [string] $TlsBundle,
    [string] $NssmSource,
    [string] $RunnerBinary,
    [ValidateSet('x64', 'arm64')] [string] $Architecture = 'x64',
    [string] $RunnerTemplate,
    [string] $RunnerSha256,
    [string[]] $FirewallRemoteAddress,
    [int] $MaxRunners,
    [int] $RunnerMemGB
)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings
$w = $s.Windows
if (-not (Test-Elevated)) { throw 'Run this elevated.' }

# A guest must never silently install as BEAST-UNIT: -TlsBundle without
# -HostId would default $HostId to $w.HostId below and enrol as beast-unit
# while presenting a certificate issued for whatever name was actually used.
if ($TlsBundle -and -not $PSBoundParameters.ContainsKey('HostId')) {
    throw '-TlsBundle requires -HostId; pass both together.'
}

if (-not $HostId)        { $HostId = $w.HostId }
if (-not $ListenAddress) { $ListenAddress = $s.HostAddress }
if (-not $NssmSource)    { $NssmSource = $w.NssmSource }
if (-not $RunnerBinary)  { $RunnerBinary = $w.RunnerBinary }
if (-not $RunnerTemplate) { $RunnerTemplate = $w.Template }
if (-not $RunnerSha256) { $RunnerSha256 = $w.RunnerSha256 }
if (-not $MaxRunners)    { $MaxRunners = $w.MaxRunners }
if (-not $RunnerMemGB)   { $RunnerMemGB = $w.RunnerMemGB }
if ($Architecture -eq 'arm64' -and -not ($PSBoundParameters.ContainsKey('RunnerTemplate') -and
        $PSBoundParameters.ContainsKey('RunnerBinary') -and $PSBoundParameters.ContainsKey('RunnerSha256'))) {
    throw 'An ARM64 worker requires explicit -RunnerTemplate, -RunnerBinary and -RunnerSha256.'
}
if ($Architecture -eq 'arm64' -and $WindowsStorage) {
    throw 'WindowsStorage requires Hyper-V VHD cmdlets inside the worker; this QEMU guest does not have them.'
}
if ($TlsBundle -and -not (Test-Path -LiteralPath $TlsBundle)) {
    throw "-TlsBundle $TlsBundle does not exist."
}

$listenIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $ListenAddress }
if (-not $listenIp) { throw "$ListenAddress is not on this machine (looked at every IPv4 address bound here)." }
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
$pythonSettings = if ($Architecture -eq 'arm64') { $s.WindowsArm } else { $w }
if (-not (Test-Path $python)) {
    $zip = Join-Path $env:TEMP 'rnr-python-embed.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $pythonSettings.PythonUrl -OutFile $zip
    Assert-Hash $zip $pythonSettings.PythonSha256
    Expand-Archive -LiteralPath $zip -DestinationPath $pythonDir -Force
    Remove-Item $zip
}
if ($Architecture -eq 'arm64') {
    # Reject an earlier x64 agent installation instead of silently running
    # its Python through Windows' additional x64 emulation layer.
    $pythonBytes = [IO.File]::ReadAllBytes($python)
    $peOffset = [BitConverter]::ToInt32($pythonBytes, 0x3c)
    if ([BitConverter]::ToUInt16($pythonBytes, $peOffset + 4) -ne 0xaa64) {
        throw "The ARM64 worker requires native ARM64 Python: $python"
    }
}
# The embeddable package reads its search path from its ._pth file and
# ignores PYTHONPATH and the working directory; the agent's code is added there.
$pth = Get-ChildItem $pythonDir -Filter 'python*._pth' | Select-Object -First 1
$lines = Get-Content -LiteralPath $pth.FullName
if ($lines -notcontains '..\app') { Add-Content -LiteralPath $pth.FullName -Value '..\app' }

# --- the agent, from HEAD -----------------------------------------------------
# On this host, straight from a git checkout. On a guest (-TlsBundle), there
# is no git and no clone - Install-WindowsGuestWorker.ps1 already laid an
# exploded `git archive HEAD` tree beside this script (no .git; git cannot
# run against it) and the version it recorded when making that archive on
# the real host.
$fresh = Join-Path $agentDir 'app.new'
Remove-Item -Recurse -Force $fresh -ErrorAction SilentlyContinue
if ($TlsBundle) {
    $versionFile = Join-Path $script:RepoRoot 'VERSION'
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "$versionFile is missing; Install-WindowsGuestWorker.ps1 should have written it."
    }
    $version = (Get-Content -Raw -LiteralPath $versionFile).Trim()
    # Copy-Item nests the source as a subdirectory of the destination only
    # when the destination already exists - create $fresh first, so the
    # result is $fresh\agent\... here too, matching what tar.exe -C $fresh
    # produces below from a `git archive HEAD agent` tar (its paths keep the
    # `agent/` prefix): AppDirectory is $appDir and `python -m agent` needs
    # an `agent` package one level inside it.
    New-Item -ItemType Directory -Path $fresh | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $script:RepoRoot 'agent') -Destination $fresh
} else {
    $version = (& git -C $script:RepoRoot rev-parse --short HEAD).Trim()
    $tar = Join-Path $env:TEMP 'rnr-agent.tar'
    & git -C $script:RepoRoot archive --format=tar -o $tar HEAD agent
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
    New-Item -ItemType Directory -Path $fresh | Out-Null
    & tar.exe -xf $tar -C $fresh
    if ($LASTEXITCODE -ne 0) { throw 'extracting the agent failed' }
    Remove-Item $tar
}

# --- NSSM and the template ---------------------------------------------------------
$nssm = Join-Path $binDir 'nssm.exe'
Assert-Hash $NssmSource $w.NssmSha256
if (-not (Test-Path $nssm)) { Copy-Item -LiteralPath $NssmSource -Destination $nssm }
Assert-Hash $nssm $w.NssmSha256

# Every template the repository has, not only the Forgejo one: a worker can
# only build the cells whose template is on it, and it now says which those
# are - its capabilities carry the list. A template whose payload is fetched
# rather than committed is skipped with a word, instead of being installed
# empty for a create to fail on.
$source = Join-Path $PSScriptRoot '..\windows\templates'
foreach ($dir in Get-ChildItem -Directory $source) {
    if ($Architecture -eq 'arm64' -and $dir.Name -notlike '*-arm64') { continue }
    if ($Architecture -eq 'x64' -and $dir.Name -like '*-arm64') { continue }
    $into = Join-Path $templates $dir.Name
    if ($dir.Name -like 'actions-runner-*' -and
        -not (Test-Path (Join-Path $dir.FullName 'agent\config.cmd'))) {
        Write-Host "  $($dir.Name): no payload yet - run images\windows\fetch-actions-runner.ps1"
        continue
    }
    New-Item -ItemType Directory -Force -Path $into | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $dir.FullName '*') -Destination $into
    Write-Host "  template $($dir.Name)"
}
$template = Join-Path $templates $RunnerTemplate
if (-not (Test-Path -LiteralPath $template -PathType Container)) {
    throw "Runner template $RunnerTemplate was not installed."
}
Assert-Hash $RunnerBinary $RunnerSha256
Copy-Item -Force -LiteralPath $RunnerBinary -Destination (Join-Path $template 'forgejo-runner.exe')

# Each runner's job host runs this Python, as the runner's own virtual
# account: every service account may read and run it, and nothing more.
& icacls.exe $pythonDir /grant '*S-1-5-80-0:(OI)(CI)RX' /Q | Out-Null
& icacls.exe $fresh /grant '*S-1-5-80-0:(OI)(CI)RX' /Q | Out-Null

# --- the certificate: enrolled by the controller, or imported already enrolled ----
if ($TlsBundle) {
    # A guest: Install-WindowsGuestWorker.ps1 already enrolled $HostId from a
    # machine that holds the platform SSH key ($s.Root\ssh\id_ed25519, which
    # is not on this one) and copied the resulting tar in. Delete it the
    # moment it is extracted, same as the SSH-enrolled $bundle below - it
    # holds the same private key now sitting, ACL'd, in $tlsDir, and nothing
    # should keep a second, unprotected copy of it lying around.
    & tar.exe -xf $TlsBundle -C $tlsDir
    if ($LASTEXITCODE -ne 0) { throw "extracting -TlsBundle $TlsBundle failed" }
    Remove-Item -Force -LiteralPath $TlsBundle
} else {
    $endpoint = "https://$($ListenAddress):$($s.AgentPort)"
    Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $HostId hyperv-windows $endpoint" +
        " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$HostId /tmp/bundle" +
        " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar && sudo rm -rf /tmp/bundle") -Quiet
    $bundle = Join-Path $env:TEMP 'rnr-bundle.tar'
    Receive-FromGuest $s $cp '/tmp/bundle.tar' $bundle
    Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet
    & tar.exe -xf $bundle -C $tlsDir
    Remove-Item $bundle
}
& icacls.exe $tlsDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null

# --- configuration ------------------------------------------------------------------
$config = [ordered]@{
    host_id    = $HostId
    runtime    = 'windows-process'
    listen     = "$($ListenAddress):$($s.AgentPort)"
    controller = "https://${cp}:$($s.ReceiverPort)"
    tls        = @{ cert = (Join-Path $tlsDir 'agent.crt'); key = (Join-Path $tlsDir 'agent.key')
                    ca = (Join-Path $tlsDir 'ca.pem') }
    tools      = @{ nssm = $nssm; python = $python; templates = $templates; short_workspaces = 'D:\w' }
    capacity   = @{ max_runners = $MaxRunners; memory_bytes = [int64]$RunnerMemGB * $MaxRunners * 1GB
                    architecture = $Architecture }
    version    = $version
}
if ($WindowsStorage) {
    $config['windows_storage'] = [ordered]@{ enabled = $true; root = $StorageRoot }
}
$configPath = Join-Path $agentDir 'agent.json'
Write-LfFile $configPath ($config | ConvertTo-Json -Depth 4)
& icacls.exe $configPath /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' /Q | Out-Null

# --- the firewall -------------------------------------------------------------------
$rule = 'rnr-agent (control plane only)'
$allowedSource = if ($FirewallRemoteAddress) { $FirewallRemoteAddress } else { $cp }
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $ListenAddress -LocalPort $s.AgentPort -RemoteAddress $allowedSource | Out-Null

# --- the service ----------------------------------------------------------------------
$service = 'rnr-agent'
$existing = Get-Service -Name $service -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq 'Running') { & $nssm stop $service | Out-Null }
# Swap in the new code only while the agent is stopped. Runners keep running:
# they are services of their own, each with its own job host.
$previous = Join-Path $agentDir 'app.previous'
if (Test-Path $appDir) {
    if (Test-Path $previous) { Remove-Item -Recurse -Force $previous }
    Move-Item -LiteralPath $appDir -Destination $previous
}
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

if ($TlsBundle -and -not (Test-Path -LiteralPath (Get-KeyPath $s))) {
    # This machine has no way to ask the control plane anything - the same
    # SSH key the enrolment step above would have needed. The caller
    # (Install-WindowsGuestWorker.ps1), which does have it, checks instead.
    Write-Host "Skipping the health check: $(Get-KeyPath $s) is not on this machine, so the control plane cannot be reached from here."
} else {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 5
        $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
    } until ($status -match "(?m)^\s+$HostId\s+\S+\s+healthy" -or (Get-Date) -gt $deadline)
    Write-Host $status
    if ($status -notmatch "(?m)^\s+$HostId\s+\S+\s+healthy") { throw "$HostId did not report healthy within 60 s" }
}
Write-Host "The Windows worker is joined up at $version."
