<#
.SYNOPSIS
    Carries the Windows worker into a Hyper-V guest (T-23, W10b-2) and runs
    Install-WindowsWorker.ps1 there. Run elevated, after Initialize-WindowsGuest.ps1
    has configured the guest and it answers PowerShell Direct.

.DESCRIPTION
    Install-WindowsWorker.ps1 was written for the physical host: it enrols
    itself by SSH-ing into the control plane with the platform key
    ($s.Root\ssh\id_ed25519, under D:\HyperV\runner-platform), and it builds
    the agent with `git archive` from this repository's own clone. Neither
    exists inside a guest. So the host stays the orchestrator - the same
    shape Initialize-RunnerPlatform.ps1 already uses for the Linux workers -
    and carries everything the guest is missing across PowerShell Direct
    before running the installer there with its SSH-free equivalents
    (-TlsBundle, -NssmSource, -RunnerBinary).

    One session (`New-PSSession -VMName`), then, in order:
      1. the target directory in the guest, cleared and recreated so a stale
         file from an earlier run cannot survive;
      2. `git archive HEAD` of `agent` and `infra/hyperv`, made here, copied
         in and exploded with tar.exe - no .git, so Install-WindowsWorker.ps1
         must not try to run git against it (its -TlsBundle branch does not);
      3. the VERSION this HEAD is, so the guest can report it without git;
      4. the Windows templates directory, copied as a plain directory tree
         rather than through the archive above, because a template's fetched
         payload (images/windows/fetch-actions-runner.ps1) is gitignored and
         `git archive` would silently leave it behind;
      5. the pinned NSSM and Forgejo runner binary, by their settings.psd1
         paths - both are host paths, unreachable from the guest;
      6. the guest's host id enrolled with the controller from here (this
         machine holds the platform SSH key the guest does not), and the
         resulting certificate bundle copied in;
      7. Install-WindowsWorker.ps1, run inside the guest with -HostId,
         -ListenAddress, -TlsBundle and the guest-local -NssmSource /
         -RunnerBinary, plus -WindowsStorage so its runners get their own
         fixed VHDX on the data disk settings.psd1's WindowsGuests entry
         names.

    Install-WindowsWorker.ps1 skips its own health check inside the guest
    (no SSH key there to ask the control plane with); this script does that
    check itself afterwards, from here, and only reports success once it has
    seen the worker come up healthy.

    Idempotent: running it again re-copies and re-deploys, exactly as
    Install-WindowsWorker.ps1 already does with app.previous inside the
    guest.

    Does not format the guest's second disk. New-WindowsGuest.ps1 attaches it
    raw and unpartitioned; nothing in this platform brings it online yet, so
    the drive settings.psd1 names as the storage root (D:\runner-disks) will
    not exist until that is done by hand, before capacity is ever raised on
    this worker.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Name = 'rnr-windows-1',
    [Parameter(Mandatory)] [System.Management.Automation.PSCredential] $Credential
)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings

# PowerShell Direct reads a bare user name as a domain account and answers
# "The credential is invalid" - the guest's local account has to be named
# ".\<user>" (Initialize-WindowsGuest.ps1 found this first).
if ($Credential.UserName -notmatch '[\\@]') {
    $Credential = [System.Management.Automation.PSCredential]::new(
        ".\$($Credential.UserName)", $Credential.Password)
}

if (-not (Test-Elevated)) {
    throw "Run this elevated: PowerShell Direct needs an administrator on the host."
}
if (-not $s.VMs.ContainsKey($Name)) {
    throw "settings.psd1 has no VM named $Name."
}
if (-not $s.WindowsGuests.ContainsKey($Name)) {
    throw "settings.psd1's WindowsGuests has no entry for $Name."
}
$spec = $s.VMs[$Name]
$g = $s.WindowsGuests[$Name]

$vm = Get-VM -Name $Name -ErrorAction SilentlyContinue
if (-not $vm) { throw "$Name does not exist; run New-WindowsGuest.ps1 first." }
if ($vm.State -ne 'Running') { throw "$Name is $($vm.State), not Running." }

$cpName = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })[0]
$cp = $s.VMs[$cpName].Address
$sshKey = Get-KeyPath $s
if (-not (Test-Path -LiteralPath $sshKey)) {
    throw "The platform SSH key ($sshKey) is not on this machine; run this from the machine that orchestrates the platform."
}

if (-not $PSCmdlet.ShouldProcess($Name, "carry the Windows worker in and install it there")) {
    return
}

$hostStage = Join-Path $env:TEMP "rnr-guest-worker-$Name"
Remove-Item -Recurse -Force $hostStage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostStage | Out-Null
$session = $null
try {
    # --- what goes over, made here ------------------------------------------------
    $version = (& git -C $script:RepoRoot rev-parse --short HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'git rev-parse failed' }
    $dirty = & git -C $script:RepoRoot status --porcelain -- agent infra/hyperv infra/windows/templates
    if ($dirty) { Write-Warning "uncommitted changes are not deployed; HEAD $version is" }
    $codeTar = Join-Path $hostStage 'code.tar'
    & git -C $script:RepoRoot archive --format=tar -o $codeTar HEAD agent infra/hyperv
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }

    Write-Host "opening PowerShell Direct to $Name..."
    $session = New-PSSession -VMName $Name -Credential $Credential

    # --- the target directory, cleared so a stale file cannot survive -------------
    $guestSrc = 'C:\ProgramData\nomercy\src'
    Invoke-Command -Session $session -ScriptBlock {
        param($SrcRoot)
        $ErrorActionPreference = 'Stop'
        Remove-Item -Recurse -Force $SrcRoot -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $SrcRoot | Out-Null
    } -ArgumentList $guestSrc

    # --- the repository archive: agent + the scripts that run it ------------------
    $guestTar = 'C:\Windows\Temp\rnr-guest-worker-code.tar'
    Copy-Item -ToSession $session -Path $codeTar -Destination $guestTar -Force
    Invoke-Command -Session $session -ScriptBlock {
        param($TarPath, $Dest)
        $ErrorActionPreference = 'Stop'
        & tar.exe -xf $TarPath -C $Dest
        if ($LASTEXITCODE -ne 0) { throw 'extracting the repository archive in the guest failed' }
        Remove-Item -Force $TarPath
    } -ArgumentList $guestTar, $guestSrc

    # --- the version this HEAD is, so the guest need not run git ------------------
    $guestVersionFile = Join-Path $guestSrc 'VERSION'
    Invoke-Command -Session $session -ScriptBlock {
        param($Path, $Text)
        $ErrorActionPreference = 'Stop'
        [IO.File]::WriteAllText($Path, $Text)
    } -ArgumentList $guestVersionFile, $version

    # --- the Windows templates: a plain copy, not the archive ----------------------
    # git archive would silently drop a template's fetched payload
    # (infra/windows/templates/*/agent/ is gitignored -
    # images/windows/fetch-actions-runner.ps1 puts it there) - copy the
    # directory as it stands on this host instead, so whatever is already
    # fetched here reaches the guest too.
    $localTemplates = Join-Path $script:RepoRoot 'infra\windows\templates'
    $guestInfraWindows = Join-Path $guestSrc 'infra\windows'
    Invoke-Command -Session $session -ScriptBlock {
        param($Path)
        $ErrorActionPreference = 'Stop'
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    } -ArgumentList $guestInfraWindows
    Copy-Item -ToSession $session -Recurse -Force -Path $localTemplates -Destination $guestInfraWindows

    # --- the pinned NSSM and Forgejo runner binary: host paths, copied in ---------
    $guestNssm = Join-Path $guestSrc 'nssm.exe'
    Copy-Item -ToSession $session -Path $s.Windows.NssmSource -Destination $guestNssm -Force
    $runnerLeaf = Split-Path $s.Windows.RunnerBinary -Leaf
    $guestRunnerBinary = Join-Path $guestSrc $runnerLeaf
    Copy-Item -ToSession $session -Path $s.Windows.RunnerBinary -Destination $guestRunnerBinary -Force

    # --- enrolled from here: this machine holds the platform SSH key --------------
    Write-Host "enrolling $Name with the controller..."
    $endpoint = "https://$($spec.Address):$($s.AgentPort)"
    Invoke-Guest $s $cp ("sudo docker exec rnr-controller python -m control enrol $Name hyperv-windows $endpoint" +
        " && sudo rm -rf /tmp/bundle && sudo docker cp rnr-controller:/data/control-tls/workers/$Name /tmp/bundle" +
        " && sudo tar -C /tmp/bundle -cf /tmp/bundle.tar . && sudo chown `$(id -un) /tmp/bundle.tar && sudo rm -rf /tmp/bundle") -Quiet
    $bundleTar = Join-Path $hostStage 'bundle.tar'
    Receive-FromGuest $s $cp '/tmp/bundle.tar' $bundleTar
    Invoke-Guest $s $cp 'rm -f /tmp/bundle.tar' -Quiet
    $guestBundle = Join-Path $guestSrc 'bundle.tar'
    Copy-Item -ToSession $session -Path $bundleTar -Destination $guestBundle -Force

    # --- the installer, run inside the guest ---------------------------------------
    Write-Host "installing the worker inside $Name..."
    $guestInstaller = Join-Path $guestSrc 'infra\hyperv\Install-WindowsWorker.ps1'
    Invoke-Command -Session $session -ScriptBlock {
        param($Installer, $HostId, $ListenAddress, $TlsBundlePath, $NssmPath, $RunnerPath, $StorageRootPath)
        $ErrorActionPreference = 'Stop'
        & $Installer -HostId $HostId -ListenAddress $ListenAddress -TlsBundle $TlsBundlePath `
            -NssmSource $NssmPath -RunnerBinary $RunnerPath -WindowsStorage -StorageRoot $StorageRootPath
    } -ArgumentList $guestInstaller, $Name, $spec.Address, $guestBundle, $guestNssm, $guestRunnerBinary, $g.StorageRoot

    # --- healthy, checked from here: the guest has no SSH key to ask itself --------
    Write-Host "waiting for $Name to report healthy..."
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 5
        $status = Invoke-Guest $s $cp 'sudo docker exec rnr-controller python -m control status' | Out-String
    } until ($status -match "(?m)^\s+$Name\s+\S+\s+healthy" -or (Get-Date) -gt $deadline)
    Write-Host $status
    if ($status -notmatch "(?m)^\s+$Name\s+\S+\s+healthy") { throw "$Name did not report healthy within 90 s" }
    Write-Host "$Name is joined up at $version, capacity $($g.MaxRunners) x $($g.RunnerMemGB) GB, storage root $($g.StorageRoot)."
}
finally {
    if ($session) { Remove-PSSession $session -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $hostStage -ErrorAction SilentlyContinue
}
