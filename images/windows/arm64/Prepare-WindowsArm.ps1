# Run once from an elevated PowerShell console in the installed ARM64 guest.
# The VirtIO ISO and this payload ISO must both be attached to QEMU.
$ErrorActionPreference = 'Stop'
Start-Transcript -Path 'C:\windows-arm-preparation.log' -Append | Out-Null
try {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'ARM64') {
        throw "Expected an ARM64 guest; processor architecture is $env:PROCESSOR_ARCHITECTURE."
    }

    Write-Host 'Locating the ARM64 VirtIO network driver...'
    $volumes = Get-PSDrive -PSProvider FileSystem
    $driver = $null
    foreach ($volume in $volumes) {
        $candidate = Join-Path $volume.Root 'NetKVM\w11\ARM64\netkvm.inf'
        if (Test-Path -LiteralPath $candidate) { $driver = $candidate; break }
    }
    if (-not $driver) { throw 'The attached VirtIO ISO has no ARM64 NetKVM driver.' }

    Write-Host "Installing $driver..."
    & pnputil.exe /add-driver $driver /install
    # PnPUtil returns ERROR_NO_MORE_ITEMS (259) when this exact driver is
    # already published and current for the device. The network test below
    # still verifies that it actually works.
    if ($LASTEXITCODE -notin @(0, 259)) { throw "Installing $driver failed: $LASTEXITCODE" }
    Get-NetAdapter | Where-Object Status -eq 'Disabled' | Enable-NetAdapter -Confirm:$false

    $online = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-NetConnection -ComputerName 'www.microsoft.com' -Port 443 -InformationLevel Quiet) {
            $online = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $online) { throw 'Network driver installed, but outbound HTTPS is unavailable.' }

    Write-Host 'Checking the OpenSSH Server Windows capability...'
    $ssh = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ($ssh.State -ne 'Installed') {
        Write-Host 'Installing OpenSSH Server...'
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    }
    $publicKey = Join-Path $PSScriptRoot 'admin-key.pub'
    if (-not (Test-Path -LiteralPath $publicKey)) {
        throw 'The guest payload ISO has no admin-key.pub.'
    }
    $authorized = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    Get-Content -LiteralPath $publicKey | Set-Content -LiteralPath $authorized -Encoding Ascii
    & icacls.exe $authorized /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not lock down the SSH authorized key.' }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
    if (-not (Get-NetFirewallRule -Name 'rnr-windows-arm-ssh' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'rnr-windows-arm-ssh' -DisplayName 'Windows ARM runner SSH' `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 | Out-Null
    }

    Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' `
        -Name fDenyTSConnections -Value 0
    Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

    Write-Host 'Windows ARM64 network, SSH and RDP are ready.'
} finally {
    Stop-Transcript | Out-Null
}
