[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [ValidateSet('x64', 'arm64')]
    [string] $Architecture = 'x64',
    [string] $ResultPath = 'D:\HyperV\runner-platform\stage\windows-build-tools-result.json'
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '..\windows\guest\Install-CppBuildTools.ps1'
$destination = 'C:\ProgramData\nomercy\Install-CppBuildTools.ps1'
$guestResult = 'C:\ProgramData\nomercy\cpp-build-tools-result.json'
$taskName = 'NoMercyInstallCppBuildTools'
$session = $null
try {
    $credential = Get-Credential -Message "Enter the local administrator for $Name"
    if (-not $credential) { throw 'No guest credential was entered.' }
    if ($credential.UserName -notmatch '[\\@]') {
        $credential = [pscredential]::new(".\$($credential.UserName)", $credential.Password)
    }
    $session = New-PSSession -VMName $Name -Credential $credential
    Invoke-Command -Session $session -ScriptBlock {
        New-Item -ItemType Directory -Path 'C:\ProgramData\nomercy' -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -ToSession $session -Force
    Invoke-Command -Session $session -ArgumentList $taskName, $destination, $Architecture, $guestResult -ScriptBlock {
        param($name, $script, $arch, $result)
        Remove-Item -LiteralPath $result -Force -ErrorAction SilentlyContinue
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $script +
            '" -Architecture ' + $arch + ' -ResultPath "' + $result + '"'
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName $name
    }
    $deadline = (Get-Date).AddMinutes(75)
    do {
        Start-Sleep -Seconds 10
        $state = Invoke-Command -Session $session -ArgumentList $taskName, $guestResult -ScriptBlock {
            param($name, $result)
            if (Test-Path -LiteralPath $result) {
                Get-Content -LiteralPath $result -Raw
            } else {
                [pscustomobject]@{ State = 'starting'; Detail = (Get-ScheduledTask -TaskName $name).State } |
                    ConvertTo-Json
            }
        }
        $state | Set-Content -LiteralPath $ResultPath -Encoding UTF8
        $parsed = $state | ConvertFrom-Json
        Write-Host "$($parsed.State): $($parsed.Detail)"
        if ($parsed.State -in @('ready', 'reboot-required', 'error')) { break }
    } while ((Get-Date) -lt $deadline)
    if ($parsed.State -notin @('ready', 'reboot-required')) {
        throw "C++ Build Tools installation ended in state $($parsed.State)."
    }
} catch {
    if (-not (Test-Path -LiteralPath $ResultPath)) {
        @{ State = 'error'; Detail = $_.Exception.Message } | ConvertTo-Json |
            Set-Content -LiteralPath $ResultPath -Encoding UTF8
    }
    throw
} finally {
    if ($session) { Remove-PSSession $session }
}
