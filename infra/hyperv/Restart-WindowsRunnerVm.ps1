[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [string] $ResultPath = 'D:\HyperV\runner-platform\stage\windows-runner-restart.txt'
)

$ErrorActionPreference = 'Stop'
try {
    $vm = Get-VM -Name $Name
    if ($vm.State -ne 'Running') { throw "$Name is $($vm.State), expected Running." }
    $shutdown = Get-VMIntegrationService -VMName $Name -Name 'Shutdown'
    if (-not $shutdown.Enabled) { throw "$Name has no enabled shutdown integration service." }
    Stop-VM -Name $Name -ErrorAction Stop
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-VM -Name $Name).State -ne 'Off' -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
    }
    if ((Get-VM -Name $Name).State -ne 'Off') {
        throw "$Name did not shut down gracefully within three minutes."
    }
    Start-VM -Name $Name -ErrorAction Stop
    "STARTED $Name" | Set-Content -LiteralPath $ResultPath
} catch {
    "ERROR $($_.Exception.Message)" | Set-Content -LiteralPath $ResultPath
    Write-Error $_
    exit 1
}
