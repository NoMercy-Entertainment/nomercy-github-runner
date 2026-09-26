[CmdletBinding()]
param(
    [string]$VMName = 'rnr-windows-1',
    [string]$ResultPath = 'D:\HyperV\runner-platform\stage\php-access-repair-result.json'
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ State = 'failed'; Detail = $null; Packages = @() }
try {
    $credential = Get-Credential -Message "Enter the local administrator username and password for $VMName"
    if (-not $credential) { throw 'No guest credential was entered.' }
    if ($credential.UserName -notmatch '[\\@]') {
        $credential = [pscredential]::new(".\$($credential.UserName)", $credential.Password)
    }
    $packages = Invoke-Command -VMName $VMName -Credential $credential -ScriptBlock {
        $ErrorActionPreference = 'Stop'
        $dirs = @(Get-Item 'C:\Program Files\WinGet\Packages\PHP.PHP.8.4*' -ErrorAction Stop |
            Where-Object PSIsContainer)
        if (-not $dirs) { throw 'No machine-wide PHP 8.4 package was found.' }
        foreach ($dir in $dirs) {
            & icacls.exe $dir.FullName /grant '*S-1-5-11:(OI)(CI)RX' /T /C | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not grant read/execute on $($dir.FullName)" }
            $dir.FullName
        }
    }
    $result.State = 'ready'
    $result.Packages = @($packages)
} catch {
    $result.Detail = $_.Exception.Message
} finally {
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultPath -Encoding utf8
    Write-Host "PHP access repair: $($result.State) $($result.Detail)"
}
