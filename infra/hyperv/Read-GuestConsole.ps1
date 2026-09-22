<# Read-only Hyper-V console thumbnail; no keyboard input or VM state changes. #>
[CmdletBinding()]
param([string]$VmName='rnr-linux-1', [string]$OutputPath='D:\HyperV\runner-platform\stage\maintenance-20260921\linux-console.rgb565')
$ErrorActionPreference='Stop'
$vm=Get-VM -Name $VmName
$settings=Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_VirtualSystemSettingData |
    Where-Object { $_.VirtualSystemIdentifier -eq $vm.Id -and $_.VirtualSystemType -eq 'Microsoft:Hyper-V:System:Realized' }
if (@($settings).Count -ne 1) { throw 'No unique current VM settings.' }
$service=Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_VirtualSystemManagementService
$result=Invoke-CimMethod -InputObject $service -MethodName GetVirtualSystemThumbnailImage -Arguments @{
    TargetSystem=$settings; WidthPixels=[uint16]1024; HeightPixels=[uint16]768
}
if ($result.ReturnValue -ne 0) { throw "Thumbnail failed: $($result.ReturnValue)" }
[IO.File]::WriteAllBytes($OutputPath, $result.ImageData)
Get-VMNetworkAdapter -VMName $VmName | Select-Object Name,IPAddresses,MacAddress,Status |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath ($OutputPath+'.network.json') -Encoding UTF8
