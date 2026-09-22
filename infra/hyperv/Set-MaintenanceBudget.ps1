<# Apply the explicitly selected 80 GiB Linux / 32 GiB WSL maintenance budget.
   Existing boot/checkpoint disks are never resized or removed. Run elevated.
   Quiesce first; finish guest builds and shut down WSL before ApplyBudget. #>
[CmdletBinding()]
param(
    [ValidateSet('Quiesce', 'ApplyBudget')][string]$Phase = 'Quiesce',
    [string]$OutputPath = 'D:\HyperV\runner-platform\stage\maintenance-20260921\host-change.json'
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script elevated.'
}
$result = [ordered]@{ phase = $Phase; at = [DateTime]::UtcNow.ToString('o'); completed = $false }
try {
    $runners = @(Get-Service -Name 'rnr-*' | Where-Object Name -ne 'rnr-agent')
    if (@($runners | Where-Object Status -ne 'Stopped').Count) {
        throw 'Runner services must already be stopped; this script never aborts them.'
    }
    foreach ($runner in $runners) { Set-Service -Name $runner.Name -StartupType Manual }
    $task = Get-ScheduledTask -TaskName 'GitHub Runners - Keep WSL Distro Alive'
    Disable-ScheduledTask -InputObject $task | Out-Null
    if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task }
    $result.manual_runner_services = @($runners.Name)
    $result.keepalive_disabled = $true
    if ($Phase -eq 'ApplyBudget') {
        $config = Get-Content -LiteralPath 'C:\Users\phill\.wslconfig' -Raw
        if ($config -notmatch '(?m)^memory=32GB\s*$') { throw 'The approved WSL 32GB configuration must be installed first.' }
        $vm = Get-VM -Name 'rnr-linux-1'
        $result.old_linux_memory = (Get-VMMemory -VMName $vm.Name).Startup
        $result.old_linux_cpus = (Get-VMProcessor -VMName $vm.Name).Count
        if ($vm.State -ne 'Off') {
            Stop-VM -Name $vm.Name -Confirm:$false
            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-VM -Name $vm.Name).State -ne 'Off' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
        }
        if ((Get-VM -Name $vm.Name).State -ne 'Off') { throw 'Linux did not shut down gracefully.' }
        Set-VMMemory -VMName $vm.Name -DynamicMemoryEnabled $false -StartupBytes 80GB
        Set-VMProcessor -VMName $vm.Name -Count 56
        $dataRoot = [IO.Path]::GetFullPath('D:\HyperV\runner-platform\data')
        New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
        $disks = @(
            @{ vm = 'rnr-linux-1'; name = 'linux-runner-data.vhdx'; size = 2304GB },
            @{ vm = 'macos-runner'; name = 'macos-appliance-data.vhdx'; size = 768GB }
        )
        $result.data_disks = @()
        foreach ($disk in $disks) {
            $path = [IO.Path]::GetFullPath((Join-Path $dataRoot $disk.name))
            if (-not $path.StartsWith($dataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid data disk path.' }
            if (-not (Test-Path -LiteralPath $path)) { New-VHD -Path $path -Dynamic -SizeBytes $disk.size | Out-Null }
            $vhd = Get-VHD -Path $path
            if ($vhd.Size -ne $disk.size -or $vhd.VhdType -ne 'Dynamic' -or $vhd.ParentPath) { throw "Unexpected data disk: $path" }
            $attachments = @(Get-VM | Get-VMHardDiskDrive | Where-Object Path -eq $path)
            if (@($attachments | Where-Object VMName -ne $disk.vm).Count) { throw 'Data disk belongs to another VM.' }
            if (-not $attachments.Count) { Add-VMHardDiskDrive -VMName $disk.vm -ControllerType SCSI -Path $path }
            $result.data_disks += @{ vm = $disk.vm; path = $path; bytes = $disk.size }
        }
        Start-VM -Name $vm.Name
        $result.linux_memory_bytes = (Get-VMMemory -VMName $vm.Name).Startup
        $result.linux_cpus = (Get-VMProcessor -VMName $vm.Name).Count
    }
    $result.completed = $true
} catch {
    $result.error = $_.Exception.Message
    throw
} finally {
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
}
