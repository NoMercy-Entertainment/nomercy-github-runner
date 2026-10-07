$ErrorActionPreference='Stop'
$root='C:\ProgramData\nomercy\portable'
$action=New-ScheduledTaskAction -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' -Argument ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\Finish-PortableArm.ps1"')
$trigger=New-ScheduledTaskTrigger -AtStartup
$trigger.Delay='PT30S'
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'NoMercyPortableArmFinish' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
# The original worker will be re-enrolled after return. Never share its live identity.
Stop-Service -Name 'rnr-agent' -ErrorAction Stop
Set-Service -Name 'rnr-agent' -StartupType Disabled
@{phase='prepared';detail='Will install and test automatically at the next Windows boot.';utc=(Get-Date).ToUniversalTime().ToString('o')} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'status.json') -Encoding utf8
Export-ScheduledTask -TaskName 'NoMercyPortableArmFinish' | Set-Content (Join-Path $root 'task-installed.xml')
Write-Host 'Portable task installed; original worker disabled; no installation was started.'
