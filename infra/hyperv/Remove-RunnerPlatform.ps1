<#
.SYNOPSIS
    Rolls phase 5 back: the runner-platform VMs, their NAT and their switch.
    Run elevated. Asks before each removal unless -Confirm:$false.

.DESCRIPTION
    Removes exactly what New-RunnerPlatformVMs.ps1 made, by the names in
    settings.psd1, and nothing else - the macOS appliance and every other VM
    are never looked at. The WSL fleet and dashboard were never changed by
    phase 5, so there is nothing to restore there.

    A worker still holding runners is refused: remove them through the
    controller first (`python -m control capacity <fleet> 0`), which drains
    them and deletes their records at the forge. Removing the VM under them
    would strand those records. -Force skips that check, for a worker whose
    runners are already known to be gone.

    The prepared images and the SSH key under the platform root are kept;
    delete the folder by hand if they are not wanted.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param([switch] $Force)
. "$PSScriptRoot\lib.ps1"
$s = Get-RunnerPlatformSettings
if (-not (Test-Elevated)) { throw 'Run this elevated.' }

$workers = @($s.VMs.Keys | Where-Object { $s.VMs[$_].Role -eq 'linux-worker' })
if (-not $Force) {
    foreach ($name in $workers) {
        if (-not (Get-VM -Name $name -ErrorAction SilentlyContinue)) { continue }
        try {
            $units = Invoke-Guest $s $s.VMs[$name].Address 'sudo docker ps -aq --filter label=nomercy.runner_id | wc -l'
        } catch {
            throw "Could not ask $name whether it still holds runners: $($_.Exception.Message). Use -Force if they are known to be gone."
        }
        if ([int]($units | Select-Object -Last 1) -gt 0) {
            throw "$name still holds $units runner unit(s). Scale their fleets to 0 through the controller first."
        }
    }
}

foreach ($name in $s.VMs.Keys) {
    $vm = Get-VM -Name $name -ErrorAction SilentlyContinue
    if (-not $vm) { continue }
    if ($PSCmdlet.ShouldProcess($name, 'shut down and remove the VM and its disk')) {
        if ($vm.State -ne 'Off') { Stop-VM -Name $name -Force }
        Remove-VM -Name $name -Force
        Remove-Item -Recurse -Force (Join-Path $s.Root "vms\$name") -ErrorAction SilentlyContinue
    }
}

$nat = Get-NetNat -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $s.Nat }
if ($nat -and $PSCmdlet.ShouldProcess($s.Nat, 'remove NetNat')) {
    Remove-NetNat -Name $s.Nat -Confirm:$false
}
$switch = Get-VMSwitch -Name $s.Switch -ErrorAction SilentlyContinue
if ($switch) {
    $users = @(Get-VMNetworkAdapter -All | Where-Object { $_.SwitchName -eq $s.Switch -and -not $_.IsManagementOs })
    if ($users.Count -gt 0) {
        Write-Warning "$($s.Switch) is still used by $($users.VMName -join ', '); left in place."
    } elseif ($PSCmdlet.ShouldProcess($s.Switch, 'remove switch')) {
        Remove-VMSwitch -Name $s.Switch -Force
    }
}
