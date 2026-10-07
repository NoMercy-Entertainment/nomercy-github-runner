# Make QEMU's ACPI power-button request shut Windows down cleanly.
$ErrorActionPreference = 'Stop'
foreach ($mode in @('/setacvalueindex', '/setdcvalueindex')) {
    & powercfg.exe $mode SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3
    if ($LASTEXITCODE -ne 0) { throw "powercfg $mode failed: $LASTEXITCODE" }
}
& powercfg.exe /setactive SCHEME_CURRENT
if ($LASTEXITCODE -ne 0) { throw "Activating shutdown policy failed: $LASTEXITCODE" }
$policy = & powercfg.exe /qh SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION
if ($LASTEXITCODE -ne 0) { throw "Reading shutdown policy failed: $LASTEXITCODE" }
if ([regex]::Matches(($policy -join "`n"), '0x00000003', 'IgnoreCase').Count -ne 2) {
    throw 'Power-button shutdown policy did not apply to both AC and DC.'
}
$policy
