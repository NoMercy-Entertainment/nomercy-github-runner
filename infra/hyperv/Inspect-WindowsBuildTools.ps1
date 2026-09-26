[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [string] $ResultPath = 'D:\HyperV\runner-platform\stage\windows-build-tools-inspection.json'
)

$ErrorActionPreference = 'Stop'
try {
    $credential = Get-Credential -Message "Enter the local administrator for $Name"
    if (-not $credential) { throw 'No guest credential was entered.' }
    if ($credential.UserName -notmatch '[\\@]') {
        $credential = [pscredential]::new(".\$($credential.UserName)", $credential.Password)
    }
    $result = Invoke-Command -VMName $Name -Credential $credential -ScriptBlock {
        $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
        $installation = if (Test-Path -LiteralPath $vswhere) {
            & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        } else { '' }
        $compiler = if ($installation) {
            Get-ChildItem -LiteralPath (Join-Path $installation 'VC\Tools\MSVC') -Filter cl.exe -Recurse -ErrorAction SilentlyContinue |
                Where-Object FullName -Like '*\Hostx64\x64\cl.exe' | Select-Object -First 1 -ExpandProperty FullName
        } else { $null }
        [pscustomobject]@{
            Name = $env:COMPUTERNAME
            VSWhere = Test-Path -LiteralPath $vswhere
            Installation = [string]$installation
            Compiler = [string]$compiler
            FreeGB = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
            BuildToolsBootstrapper = [bool](Get-Command winget -ErrorAction SilentlyContinue)
        }
    }
    $result | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
} catch {
    @{ Error = $_.Exception.Message } | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
    throw
}
