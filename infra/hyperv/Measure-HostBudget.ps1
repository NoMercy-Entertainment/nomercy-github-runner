<# Collect host/VM memory budgets without changing VM state. Run elevated. #>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$vms = @(Get-VM | ForEach-Object {
    $vmMemory = Get-VMMemory -VMName $_.Name
    [ordered]@{
        name = $_.Name; state = [string]$_.State
        assigned_bytes = $_.MemoryAssigned; startup_bytes = $vmMemory.Startup
        dynamic = $vmMemory.DynamicMemoryEnabled
        cpus = (Get-VMProcessor -VMName $_.Name).Count
        disks = @(Get-VMHardDiskDrive -VMName $_.Name | Select-Object -ExpandProperty Path)
    }
})
[ordered]@{
    measured_at = [DateTime]::UtcNow.ToString('o')
    physical_bytes = [int64]$os.TotalVisibleMemorySize * 1KB
    free_physical_bytes = [int64]$os.FreePhysicalMemory * 1KB
    commit_limit = $memory.CommitLimit; committed_bytes = $memory.CommittedBytes
    vms = $vms
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
