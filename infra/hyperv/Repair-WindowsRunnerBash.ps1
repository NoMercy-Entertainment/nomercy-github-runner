[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [string] $ResultPath = 'D:\HyperV\runner-platform\stage\windows-bash-repair.txt'
)

$ErrorActionPreference = 'Stop'
try {
    $credential = Get-Credential -Message "Enter the local administrator for $Name (for example .\rnr-admin)"
    if (-not $credential) { throw 'No guest credential was entered.' }
    if ($credential.UserName -notmatch '[\\@]') {
        $credential = [pscredential]::new(".\$($credential.UserName)", $credential.Password)
    }
    $result = Invoke-Command -VMName $Name -Credential $credential -ScriptBlock {
        $bashDir = 'C:\Program Files\Git\bin'
        $bash = Join-Path $bashDir 'bash.exe'
        if (-not (Test-Path -LiteralPath $bash)) {
            throw "Git Bash is not installed at $bash"
        }
        $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        if (($machinePath -split ';') -notcontains $bashDir) {
            [Environment]::SetEnvironmentVariable(
                'Path', ($machinePath.TrimEnd(';') + ';' + $bashDir), 'Machine')
        }
        $newPath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $resolved = @($newPath -split ';' | ForEach-Object {
            Join-Path $_ 'bash.exe'
        } | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1)
        if (-not $resolved) { throw 'bash.exe is still absent from the machine PATH.' }
        "READY $resolved"
    }
    $result | Set-Content -LiteralPath $ResultPath
    Write-Host $result
} catch {
    "ERROR $($_.Exception.Message)" | Set-Content -LiteralPath $ResultPath
    Write-Error $_
    exit 1
}
