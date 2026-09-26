[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [string] $ResultPath = 'D:\HyperV\runner-platform\stage\windows-runner-tools-install.txt'
)

$ErrorActionPreference = 'Stop'
$session = $null
try {
    $credential = Get-Credential -Message "Enter the local administrator for $Name"
    if (-not $credential) { throw 'No guest credential was entered.' }
    if ($credential.UserName -notmatch '[\\@]') {
        $credential = [pscredential]::new(".\$($credential.UserName)", $credential.Password)
    }
    $session = New-PSSession -VMName $Name -Credential $credential
    $guestDir = 'C:\ProgramData\nomercy\runner-tools'
    Invoke-Command -Session $session -ArgumentList $guestDir -ScriptBlock {
        param($directory)
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    foreach ($file in @('Install-RunnerTools.ps1', 'Install-CppBuildTools.ps1', 'Install-AndroidSdk.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "..\windows\guest\$file") `
            -Destination (Join-Path $guestDir $file) -ToSession $session -Force
    }
    'STARTED' | Set-Content -LiteralPath $ResultPath
    Invoke-Command -Session $session -ArgumentList $guestDir -ScriptBlock {
        param($directory)
        & (Join-Path $directory 'Install-RunnerTools.ps1')
    } *>&1 | ForEach-Object {
        $line = ($_ | Out-String).TrimEnd()
        if ($line) {
            Add-Content -LiteralPath $ResultPath -Value $line
            Write-Host $line
        }
    }
    'READY' | Add-Content -LiteralPath $ResultPath
} catch {
    "ERROR $($_.Exception.Message)" | Add-Content -LiteralPath $ResultPath
    throw
} finally {
    if ($session) { Remove-PSSession $session }
}
