<#
.SYNOPSIS
    Run the dashboard where the state store is (T-0603), beside the WSL one
    rather than instead of it. No elevation.

.DESCRIPTION
    The dashboard in the WSL distro reads its own `/data/control.db`, which
    does not exist there: the controller's store lives on the control plane.
    That is why its v2 page is empty while fifteen runners are managed.

    This copies the deployment's dashboard data - history, users, the session
    key - onto the control plane and starts a dashboard there against the
    controller's own volume. Both run: the WSL one keeps serving the public
    name until the portproxy is pointed at this one, and keeps its volume
    untouched, so going back is that one rule again.

    The session key is copied deliberately. A cookie signed by the old
    dashboard is accepted by this one, so the switch does not sign anyone
    out - which is also why the public URL must not change: the OIDC
    redirect is built from `DASH_PUBLIC_URL` and is registered at the issuer
    for that name.

    The dashboard gets no Docker socket here. Every runner it shows is
    reached through its worker's agent.

.EXAMPLE
    .\Install-ControlPlaneDashboard.ps1
    .\Install-ControlPlaneDashboard.ps1 -SkipData   # code and settings only
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Distro = 'github-runners',
    [string] $Source = 'runner-dashboard',
    [string] $EnvFile = 'D:\docker-compose\GithubRunners\.env',
    # Carried to the control plane so the dashboard behaves exactly as it
    # does today: the same sign-in, the same forges, the same fleets.
    [string[]] $Settings = @('DASH_PUBLIC_URL', 'OIDC_ISSUER', 'OIDC_CLIENT_ID',
        'OIDC_CLIENT_SECRET', 'GH_TOKEN', 'GITHUB_ORG', 'RUNNER_LABELS',
        'FORGEJO_INSTANCE_URL', 'FORGEJO_API_TOKEN'),
    [switch] $SkipData
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$control = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'control-plane' })
if ($control.Count -ne 1) { throw 'settings.psd1 must name exactly one control-plane VM' }
$cp = $s.VMs[$control[0]].Address
# The commit the control plane is at: the dashboard's cells name the unit
# images each worker built under that tag.
$version = (& git -C $repo rev-parse --short HEAD 2>$null)
if (-not $version) { throw 'git rev-parse failed; the unit images are tagged by commit' }
$stage = Join-Path $s.Root 'stage\dashboard'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null

if (-not $PSCmdlet.ShouldProcess($cp, 'run the dashboard beside the store')) { return }

# --- the deployment's settings, read and never written back -------------------
$values = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$' -and $Settings -contains $Matches[1]) {
        $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}
$missing = @($Settings | Where-Object { -not $values[$_] })
if ($missing) { throw "not set in ${EnvFile}: $($missing -join ', ')" }
# Plus what the platform itself decides, the same values the controller has.
$win = $s.Windows
$arm = $s.WindowsArm
$mac = $s.MacOS
$unitMemGB = ($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'linux-worker' } |
    ForEach-Object { $s.VMs[$_].RunnerMemGB } | Measure-Object -Minimum).Minimum
$lines = ($Settings | ForEach-Object { "$_=$($values[$_])" }) + @(
    "RUNNER_UNIT_IMAGE_GITHUB_LINUX=nomercy/runner-unit-github:$version",
    "RUNNER_UNIT_MEMORY_GITHUB_LINUX=$($s.GitHub.RunnerMemGB)g",
    "GITHUB_DRAIN_GROUP=$($s.GitHub.DrainGroup)",
    "RUNNER_UNIT_IMAGE_GITHUB_WINDOWS=$($s.GitHub.WindowsTemplate)",
    "RUNNER_UNIT_MEMORY_GITHUB_WINDOWS=$($s.GitHub.WindowsRunnerMemGB)g",
    "RUNNER_UNIT_MEMORY_FORGEJO_LINUX=${unitMemGB}g",
    "FORGEJO_RUNNER_ARTIFACT_WINDOWS=$($win.Template) sha256:$($win.RunnerSha256)",
    "RUNNER_UNIT_IMAGE_FORGEJO_WINDOWS=$($win.Template)",
    "RUNNER_UNIT_MEMORY_FORGEJO_WINDOWS=$($win.RunnerMemGB)g",
    "FORGEJO_RUNNER_ARTIFACT_WINDOWS_ARM64=$($arm.ForgejoTemplate) sha256:$($arm.ForgejoSha256)",
    "FORGEJO_RUNNER_LABELS_WINDOWS_ARM64=$($arm.ForgejoLabels)",
    "RUNNER_UNIT_IMAGE_FORGEJO_WINDOWS_ARM64=$($arm.ForgejoTemplate)",
    "RUNNER_UNIT_MEMORY_FORGEJO_WINDOWS_ARM64=$($arm.RunnerMemGB)g",
    "RUNNER_UNIT_IMAGE_GITHUB_WINDOWS_ARM64=$($arm.GitHubTemplate)",
    "RUNNER_UNIT_MEMORY_GITHUB_WINDOWS_ARM64=$($arm.RunnerMemGB)g",
    "FORGEJO_RUNNER_ARTIFACT_MACOS=$($mac.ForgejoBinary) sha256:$($mac.ForgejoSha256)",
    "FORGEJO_RUNNER_LABELS_MACOS=$($mac.ForgejoLabels)",
    "RUNNER_UNIT_IMAGE_FORGEJO_MACOS=$($mac.ForgejoTemplate)",
    "RUNNER_UNIT_IMAGE_GITHUB_MACOS=$($mac.GitHubTemplate)")
Write-LfFile (Join-Path $stage 'dashboard.env') (($lines -join "`n") + "`n")

# --- the data, copied consistently while the old dashboard keeps serving ------
if (-not $SkipData) {
    $script = @'
set -e
docker exec -i SOURCE python - <<'PY'
import os, shutil, sqlite3
os.makedirs("/data/copy", exist_ok=True)
# A backup, not a file copy: history.db is being written to right now, and
# half a WAL is not a database.
src = sqlite3.connect("file:/data/history.db?mode=ro", uri=True)
dst = sqlite3.connect("/data/copy/history.db")
src.backup(dst)
dst.close(); src.close()
for name in ("users.json", "auth.json", "secret.key", "state.json"):
    path = os.path.join("/data", name)
    if os.path.exists(path):
        shutil.copy2(path, "/data/copy/" + name)
runs = sqlite3.connect("file:/data/copy/history.db?mode=ro", uri=True)
print("runs:", runs.execute("select count(*) from runs").fetchone()[0])
PY
docker exec SOURCE sh -c "rm -f /data/copy/history.db-wal /data/copy/history.db-shm"
docker exec SOURCE tar -C /data/copy -cf /tmp/dash.tar .
docker cp SOURCE:/tmp/dash.tar TARBALL
docker exec SOURCE sh -c "rm -rf /data/copy /tmp/dash.tar"
'@ -replace "`r`n", "`n" -replace 'SOURCE', $Source -replace 'TARBALL', (ConvertTo-WslPath (Join-Path $stage 'dash.tar'))
    $scriptPath = Join-Path $stage 'copy.sh'
    Write-LfFile $scriptPath $script
    Write-Host (& wsl.exe -d $Distro -u root -- bash (ConvertTo-WslPath $scriptPath))
    if ($LASTEXITCODE -ne 0) { throw 'could not copy the dashboard data' }
}

# --- onto the control plane ------------------------------------------------------
Write-LfFile (Join-Path $stage 'compose.yml') (Expand-Template `
    (Join-Path $PSScriptRoot 'guest\controller-compose.yml') @{ ADDRESS = $cp })

Invoke-Guest $s $cp 'rm -rf /tmp/rnr-dash && mkdir -p -m 700 /tmp/rnr-dash' -Quiet
foreach ($f in Get-ChildItem $stage -File) {
    Send-ToGuest $s $cp $f.FullName "/tmp/rnr-dash/$($f.Name)"
}
$install = @'
set -e
sudo install -m 600 /tmp/rnr-dash/dashboard.env /etc/runner-platform/dashboard.env
sudo install -m 644 /tmp/rnr-dash/compose.yml /etc/runner-platform/compose.yml
if [ -f /tmp/rnr-dash/dash.tar ]; then
  # Into the store's own volume, beside control.db, without disturbing it.
  sudo docker run --rm -v runner-platform_controller-data:/data \
    -v /tmp/rnr-dash/dash.tar:/tmp/dash.tar:ro alpine \
    sh -c "tar -C /data -xf /tmp/dash.tar && ls -la /data"
fi
sudo docker compose -f /etc/runner-platform/compose.yml up -d dashboard
rm -rf /tmp/rnr-dash
'@ -replace "`r`n", "`n"
$installPath = Join-Path $stage 'install.sh'
Write-LfFile $installPath $install
Send-ToGuest $s $cp $installPath '/tmp/rnr-dash/install.sh'
Write-Host (Invoke-Guest $s $cp 'bash /tmp/rnr-dash/install.sh')

# --- prove it answers, rather than assume it ------------------------------------
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 5
    $code = Invoke-Guest $s $cp `
        "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://${cp}:9200/login" `
        -Quiet:$false
} until ("$code" -match '200|302' -or (Get-Date) -gt $deadline)
if ("$code" -notmatch '200|302') { throw "the dashboard on $cp did not answer (last: $code)" }
Write-Host "dashboard on ${cp}:9200 answers ($code)"
Write-Host ""
Write-Host "It is not serving the public name yet. To switch, elevated:"
Write-Host "  netsh interface portproxy delete v4tov4 listenport=9200 listenaddress=<lan>"
Write-Host "  netsh interface portproxy add v4tov4 listenport=9200 listenaddress=<lan> ``"
Write-Host "      connectport=9200 connectaddress=$cp"
Write-Host "Back again: the same two lines with the distro's address."
