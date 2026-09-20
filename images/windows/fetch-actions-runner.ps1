<#
.SYNOPSIS
    Put GitHub's own runner into the Windows template, checked against the
    hash GitHub publishes for it. No elevation.

.DESCRIPTION
    The Forgejo runner for Windows is built here, from source, because
    Forgejo publishes none. GitHub publishes its runner, so this fetches
    that instead of building it - and the check is what makes the difference
    safe: the SHA-256 in `images/windows/manifest.json` is the one GitHub
    states in the release's own notes, read from the API, not the hash of
    whatever happened to download.

    The archive is unpacked into the template's `agent\` directory, beside
    the three entry points the worker runs. GitHub's runner ships its own
    `run.cmd`; keeping it one level down is what lets the template have the
    `run.cmd` the job host starts.

.EXAMPLE
    .\images\windows\fetch-actions-runner.ps1
    .\images\windows\fetch-actions-runner.ps1 -Version 2.340.0 -Sha256 abc...
#>
[CmdletBinding()]
param(
    [string] $Version = '2.336.0',
    [string] $Sha256,
    [string] $Repo = 'D:\docker-compose\GithubRunners'
)
$ErrorActionPreference = 'Stop'

$artefact = "actions-runner-win-x64"
$manifestPath = Join-Path $Repo 'images\windows\manifest.json'
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$recorded = @($manifest.$artefact | Where-Object { $_.version -eq $Version })
if (-not $Sha256) {
    if (-not $recorded) {
        throw "no $artefact $Version in the manifest, and no -Sha256 given: " +
              "the hash must come from GitHub's release notes, never from " +
              "the download"
    }
    $Sha256 = $recorded[0].sha256
}
$asset = "actions-runner-win-x64-$Version.zip"
$url = "https://github.com/actions/runner/releases/download/v$Version/$asset"
$template = Join-Path $Repo "infra\windows\templates\actions-runner-v$Version-windows"
if (-not (Test-Path $template)) {
    throw "no template at $template - its three entry points come from the repository"
}

$stage = Join-Path ([IO.Path]::GetTempPath()) "rnr-actions-runner-$Version"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$zip = Join-Path $stage $asset
if (-not (Test-Path $zip)) {
    Write-Host "fetching $asset"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
}

$got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
if ($got -ne $Sha256.ToLower()) {
    Remove-Item -LiteralPath $zip -Force
    throw "$asset does not match what GitHub published: expected $Sha256, got $got"
}
Write-Host "sha256 matches what GitHub published for v$Version"

$agent = Join-Path $template 'agent'
Remove-Item -Recurse -Force $agent -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $agent | Out-Null
Expand-Archive -LiteralPath $zip -DestinationPath $agent -Force
foreach ($needed in 'config.cmd', 'run.cmd') {
    if (-not (Test-Path (Join-Path $agent $needed))) {
        throw "the archive has no $needed - is this the right asset?"
    }
}
Write-Host "template ready: $template"
Write-Host "  $(@(Get-ChildItem $agent).Count) entries in agent\"
Write-Host "Install it on the worker with Install-WindowsWorker.ps1 (elevated)."
