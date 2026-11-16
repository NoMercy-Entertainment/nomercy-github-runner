<# Increase only the running Linux VM's dynamic-memory buffer; never restarts anything. #>
[CmdletBinding()]
param(
    [switch]$Apply,
    [Parameter(Mandatory)][string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
$targetName = 'rnr-linux-1'
$targetBuffer = 150
$physicalReserve = 32GB
$commitReserve = 20GB

function Read-MemoryBudget {
    $os = Get-CimInstance Win32_OperatingSystem
    $samples = Get-Counter -Counter '\Memory\Committed Bytes','\Memory\Commit Limit' -MaxSamples 1
    $committed = [int64]($samples.CounterSamples | Where-Object {$_.Path -like '*\committed bytes'}).CookedValue
    $limit = [int64]($samples.CounterSamples | Where-Object {$_.Path -like '*\commit limit'}).CookedValue
    if ($limit -le 0) { throw 'Cannot verify the host commit budget.' }
    [pscustomobject]@{free_physical=[int64]$os.FreePhysicalMemory * 1KB; free_commit=$limit-$committed}
}

$vm = Get-VM -Name $targetName
$initialUptime = $vm.Uptime
$settings = Get-VMMemory -VMName $targetName
if ($vm.State -ne 'Running' -or -not $settings.DynamicMemoryEnabled) {
    throw 'Expected the existing running Linux VM with dynamic memory enabled.'
}
if ($settings.Buffer -notin @(100,$targetBuffer)) { throw 'Memory buffer changed concurrently; review first.' }
$budget = Read-MemoryBudget
$predicted = [math]::Min([double]$settings.Maximum,[double]$vm.MemoryDemand * (1 + $targetBuffer / 100))
$extra = [int64][math]::Max([double]0,[double]($predicted - $vm.MemoryAssigned))
if ($budget.free_physical - $extra -lt $physicalReserve -or
    $budget.free_commit - $extra -lt $commitReserve) {
    throw 'The projected buffer would leave less than 32 GiB physical or 20 GiB commit free. No change made.'
}
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$before = [ordered]@{
    at=[DateTime]::UtcNow.ToString('o'); vm=$targetName; old_buffer=$settings.Buffer; new_buffer=$targetBuffer
    assigned_bytes=$vm.MemoryAssigned; demand_bytes=$vm.MemoryDemand; predicted_extra_bytes=$extra
    free_physical_bytes=$budget.free_physical; free_commit_bytes=$budget.free_commit; applied=[bool]$Apply
}
$before | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'memory-buffer-before.json') -Encoding UTF8
$before | ConvertTo-Json -Depth 5
if (-not $Apply -or $settings.Buffer -eq $targetBuffer) { return }
$oldBuffer = $settings.Buffer
try {
    Set-VMMemory -VMName $targetName -Buffer $targetBuffer
    for ($sample = 0; $sample -lt 6; $sample++) {
        $afterBudget = Read-MemoryBudget
        if ($afterBudget.free_physical -lt $physicalReserve -or $afterBudget.free_commit -lt $commitReserve) {
            throw 'Host reserve fell below the required margin after the buffer change.'
        }
        if ($sample -lt 5) { Start-Sleep -Seconds 5 }
    }
    $afterVm = Get-VM -Name $targetName
    $afterSettings = Get-VMMemory -VMName $targetName
    if ($afterVm.State -ne 'Running' -or $afterVm.Uptime -lt $initialUptime -or $afterSettings.Buffer -ne $targetBuffer) {
        throw 'Runtime verification did not match the requested online change.'
    }
    [ordered]@{at=[DateTime]::UtcNow.ToString('o'); buffer=$afterSettings.Buffer
        assigned_bytes=$afterVm.MemoryAssigned; demand_bytes=$afterVm.MemoryDemand
        free_physical_bytes=$afterBudget.free_physical; free_commit_bytes=$afterBudget.free_commit
        uptime=[string]$afterVm.Uptime; state=[string]$afterVm.State} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'memory-buffer-after.json') -Encoding UTF8
}
catch {
    Set-VMMemory -VMName $targetName -Buffer $oldBuffer
    throw
}
