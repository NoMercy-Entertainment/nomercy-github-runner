[CmdletBinding()]
param([string]$OutputPath='D:\HyperV\runner-platform\stage\maintenance-20260921\startup-state.json')
$ErrorActionPreference='Continue'
$logs=@('Microsoft-Windows-Hyper-V-VMMS-Admin','Microsoft-Windows-Hyper-V-Worker-Admin','Microsoft-Windows-Hyper-V-Compute-Admin')
$events=@()
foreach ($log in $logs) {
    $events += @(Get-WinEvent -FilterHashtable @{LogName=$log; Level=2,3; StartTime=(Get-Date).AddMinutes(-20)} -MaxEvents 8 -ErrorAction SilentlyContinue |
        Select-Object TimeCreated,Id,LevelDisplayName,Message)
}
[ordered]@{
    at=[DateTime]::UtcNow.ToString('o')
    vms=@(Get-VM | Select-Object Name,State,Status,MemoryAssigned,ProcessorCount)
    host=Get-VMHost | Select-Object NumaSpanningEnabled,LogicalProcessorCount,MemoryCapacity
    events=$events
    jobs=@(Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_ConcreteJob | Select-Object ElementName,JobState,ErrorCode,ErrorDescription,PercentComplete,ElapsedTime)
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
