<#
.SYNOPSIS
    Fills the machine's tool cache with the Pythons and Nodes that
    setup-python and setup-node ask for, so a job finds them instead of
    downloading them.

.DESCRIPTION
    Both forges tell a job its runner's own tool cache, D:\runners\<id>\cache\
    toolcache (RUNNER_TOOL_CACHE). The agent fills it at create, and again
    when the runner starts after a cleared cache, from this machine-wide copy
    (agent/runtimes/windows_process.py, `_seed_toolcache`): every version
    here that carries its `<arch>.complete` marker.

    The layout is the one @actions/tool-cache reads:
        <Root>\Python\3.12.10\x64\python.exe     <Root>\Python\3.12.10\x64.complete
        <Root>\node\22.21.0\x64\node.exe         <Root>\node\22.21.0\x64.complete
    setup-python looks for `Python\<version>\<arch>` and setup-node for
    `node\<version>\<arch>`, take the highest version that matches the spec
    ('3.12', '22') and download only when none does.

    Python comes from the Python Software Foundation's own nuget.org
    packages: the same CPython builds, already laid out, with pip, and no
    installer. setup-python's own Windows packages run the python.org
    installer, which first deletes every registration of the same minor
    version - it would unregister the machine's Python 3.12 that `python`
    on the PATH resolves to. The nuget build has no Tcl/Tk: `import
    tkinter` needs a job's own Python. Node is nodejs.org's own zip.
    Each download is checked against the hash its publisher lists before
    anything of it is unpacked.

    Idempotent: a version whose marker is present is left alone. A newer
    patch release replaces the older one of its line here; runners keep
    what they were given until their cache is cleared or they are recreated.

    Run it elevated, in the guest. Install-RunnerTools.ps1 calls it.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('x64', 'arm64')]
    [string] $Architecture = 'x64',
    [string] $Root = 'C:\ProgramData\nomercy\toolcache',
    [string[]] $PythonVersions = @('3.10', '3.11', '3.12', '3.13', '3.14'),
    [string[]] $NodeMajors = @('20', '22', '24', '26')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$pythonPackage = if ($Architecture -eq 'arm64') { 'pythonarm64' } else { 'python' }
$nodeArch = if ($Architecture -eq 'arm64') { 'arm64' } else { 'x64' }
$work = Join-Path $env:TEMP ('rnr-toolcache-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $Root, $work | Out-Null

function Get-Json([string] $Uri) {
    Invoke-RestMethod -UseBasicParsing -Uri $Uri -TimeoutSec 120
}

function ConvertTo-Version([string] $Text) { [version]($Text.TrimStart('v')) }

function Install-Version([string] $Tool, [string] $Version, [scriptblock] $Fill) {
    $versionDir = Join-Path (Join-Path $Root $Tool) $Version
    $archDir = Join-Path $versionDir $Architecture
    $marker = Join-Path $versionDir "$Architecture.complete"
    if (Test-Path -LiteralPath $marker) {
        Write-Host ('cached        : {0} {1}' -f $Tool, $Version)
        return
    }
    if (-not $PSCmdlet.ShouldProcess("$Tool $Version", 'put into the machine tool cache')) { return }
    Write-Host ('installing    : {0} {1}' -f $Tool, $Version)
    if (Test-Path -LiteralPath $archDir) { Remove-Item -Recurse -Force -LiteralPath $archDir }
    New-Item -ItemType Directory -Force -Path $archDir | Out-Null
    & $Fill $archDir
    New-Item -ItemType File -Force -Path $marker | Out-Null
    # Older patch releases of the same line: the newest is what a spec
    # such as '3.12' or '22' resolves to.
    $line = ($Version -split '\.')[0..($(if ($Tool -eq 'Python') { 1 } else { 0 }))] -join '.'
    Get-ChildItem -LiteralPath (Join-Path $Root $Tool) -Directory |
        Where-Object { $_.Name -ne $Version -and ($_.Name -eq $line -or $_.Name.StartsWith("$line.")) } |
        ForEach-Object {
            Write-Host ('  replaced    : {0} {1}' -f $Tool, $_.Name)
            Remove-Item -Recurse -Force -LiteralPath $_.FullName
        }
}

try {
    # ---- Python, from the PSF's nuget.org packages ---------------------------
    $published = @((Get-Json "https://api.nuget.org/v3-flatcontainer/$pythonPackage/index.json").versions |
        Where-Object { $_ -match '^\d+\.\d+\.\d+$' })
    foreach ($line in $PythonVersions) {
        $version = $published | Where-Object { $_.StartsWith("$line.") } |
            Sort-Object { ConvertTo-Version $_ } | Select-Object -Last 1
        if (-not $version) { throw "nuget.org has no $pythonPackage $line release" }
        Install-Version 'Python' $version {
            param($into)
            $package = Join-Path $work "$pythonPackage.$version.nupkg"
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 -OutFile $package `
                -Uri "https://api.nuget.org/v3-flatcontainer/$pythonPackage/$version/$pythonPackage.$version.nupkg"
            $leaf = Get-Json "https://api.nuget.org/v3/registration5-semver1/$pythonPackage/$version.json"
            $entry = Get-Json $leaf.catalogEntry
            if ($entry.packageHashAlgorithm -ne 'SHA512') { throw "unexpected hash algorithm for $pythonPackage $version" }
            $sha = [Security.Cryptography.SHA512]::Create()
            $stream = [IO.File]::OpenRead($package)
            try { $actual = [Convert]::ToBase64String($sha.ComputeHash($stream)) } finally { $stream.Dispose() }
            if ($actual -ne $entry.packageHash) { throw "$pythonPackage $version does not match the hash nuget.org lists" }
            $unpacked = Join-Path $work "python-$version"
            New-Item -ItemType Directory -Force -Path $unpacked | Out-Null
            & tar.exe -xf $package -C $unpacked
            if ($LASTEXITCODE -ne 0) { throw "unpacking $pythonPackage $version failed" }
            Copy-Item -Recurse -Force -Path (Join-Path $unpacked 'tools\*') -Destination $into
            # setup-python's own builds carry python3.exe too.
            Copy-Item -Force -LiteralPath (Join-Path $into 'python.exe') -Destination (Join-Path $into 'python3.exe')
            $python = Join-Path $into 'python.exe'
            $reported = (& $python -c 'import platform; print(platform.python_version())').Trim()
            if ($reported -ne $version) { throw "Python $version reports itself as $reported" }
            & $python -m pip --version
            if ($LASTEXITCODE -ne 0) { throw "Python $version has no working pip" }
        }
    }

    # ---- Node, from nodejs.org ----------------------------------------------
    $releases = Get-Json 'https://nodejs.org/dist/index.json'
    foreach ($major in $NodeMajors) {
        $release = $releases | Where-Object { $_.version.StartsWith("v$major.") -and $_.files -contains "win-$nodeArch-zip" } |
            Sort-Object { ConvertTo-Version $_.version } | Select-Object -Last 1
        if (-not $release) { throw "nodejs.org has no Node $major for win-$nodeArch" }
        $version = $release.version.TrimStart('v')
        Install-Version 'node' $version {
            param($into)
            $name = "node-v$version-win-$nodeArch"
            $zip = Join-Path $work "$name.zip"
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 -OutFile $zip -Uri "https://nodejs.org/dist/v$version/$name.zip"
            $sums = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri "https://nodejs.org/dist/v$version/SHASUMS256.txt").Content
            $expected = ($sums -split "`n" | Where-Object { $_ -match "\s$([regex]::Escape("$name.zip"))\s*$" } |
                         ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
            if (-not $expected) { throw "SHASUMS256.txt of Node $version does not list $name.zip" }
            if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower() -ne $expected.ToLower()) {
                throw "$name.zip does not match the hash nodejs.org lists"
            }
            & tar.exe -xf $zip -C $work
            if ($LASTEXITCODE -ne 0) { throw "unpacking $name.zip failed" }
            Copy-Item -Recurse -Force -Path (Join-Path (Join-Path $work $name) '*') -Destination $into
            $reported = (& (Join-Path $into 'node.exe') --version).Trim()
            if ($reported -ne "v$version") { throw "Node $version reports itself as $reported" }
        }
    }
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host "machine tool cache ($Root):"
foreach ($tool in 'Python', 'node') {
    $dir = Join-Path $Root $tool
    $complete = @(Get-ChildItem -LiteralPath $dir -Filter "$Architecture.complete" -Recurse -Depth 1 -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Directory.Name } | Sort-Object { ConvertTo-Version $_ })
    Write-Host ('  {0,-7} {1}' -f $tool, ($complete -join ', '))
}
Write-Host 'A runner gets these at create, or when it starts after its cache was cleared.'
