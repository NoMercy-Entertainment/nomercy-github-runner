<#
.SYNOPSIS
    Lets the platform reach a Windows Hyper-V guest over SSH with its own key,
    so the guest's agent can be redeployed without an elevated session here.
    Run elevated, once per guest.
.DESCRIPTION
    Install-WindowsGuestWorker.ps1 carries the worker into its guest over
    PowerShell Direct, which needs an elevated session on this host: every
    agent redeploy therefore waited for someone to run it by hand. The ARM64
    guest is reached over SSH with the platform key instead, and needs no one.
    This gives a Hyper-V guest the same door, and nothing wider:
      - OpenSSH Server, added as a Windows capability when it is missing;
      - the platform's public key ($s.Root\ssh\id_ed25519.pub) as the only
        entry in administrators_authorized_keys, readable by SYSTEM and
        Administrators only, as sshd requires for that file;
      - password authentication switched off in sshd_config;
      - inbound TCP 22 allowed from this host's rnr-internal address only
        (settings.psd1 HostAddress), and the service set to start on boot.
    Idempotent: run it again and it rewrites the same state. Changes nothing
    about the agent or the runners.
#>
[CmdletBinding()]
param(
    [string] $Name = 'rnr-windows-1',
    [Parameter(Mandatory)] [System.Management.Automation.PSCredential] $Credential
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$s = Get-RunnerPlatformSettings
if ($Credential.UserName -notmatch '[\\@]') {
    $Credential = [System.Management.Automation.PSCredential]::new(
        ".\$($Credential.UserName)", $Credential.Password)
}
$publicKey = (Get-Content -Raw -LiteralPath (Join-Path $s.Root 'ssh\id_ed25519.pub')).Trim()
if ($publicKey -notmatch '^ssh-ed25519 ') { throw 'The platform public key is not an ed25519 key.' }
$guest = $s.VMs[$Name]
if (-not $guest) { throw "settings.psd1 has no VM $Name." }

Write-Host "opening PowerShell Direct to $Name..."
$session = New-PSSession -VMName $Name -Credential $Credential
try {
    Invoke-Command -Session $session -ArgumentList $publicKey, $s.HostAddress -ScriptBlock {
        param($PublicKey, $HostAddress)
        $ErrorActionPreference = 'Stop'
        if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
            Write-Host 'adding OpenSSH Server...'
            Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
        }
        Start-Service sshd          # creates C:\ProgramData\ssh on first start
        $config = 'C:\ProgramData\ssh\sshd_config'
        $text = Get-Content -Raw -LiteralPath $config
        $text = $text -replace '(?m)^\s*#?\s*PasswordAuthentication\s+\S+\s*$', 'PasswordAuthentication no'
        if ($text -notmatch '(?m)^PasswordAuthentication no') { $text += "`r`nPasswordAuthentication no`r`n" }
        [IO.File]::WriteAllText($config, $text)

        $keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
        [IO.File]::WriteAllText($keys, "$PublicKey`r`n")
        & icacls.exe $keys /inheritance:r /grant 'SYSTEM:F' /grant '*S-1-5-32-544:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not restrict administrators_authorized_keys.' }

        Get-NetFirewallRule -Name 'rnr-ssh-from-host' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
        New-NetFirewallRule -Name 'rnr-ssh-from-host' -DisplayName 'SSH from the platform host only' `
            -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress $HostAddress -Action Allow | Out-Null

        Set-Service sshd -StartupType Automatic
        Restart-Service sshd
        Write-Host "sshd: $((Get-Service sshd).Status), key-only, from $HostAddress"
    }
} finally {
    Remove-PSSession $session
}

$key = Join-Path $s.Root 'ssh\id_ed25519'
$probe = & ssh.exe -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new `
    -o "UserKnownHostsFile=$(Join-Path $s.Root 'ssh\known_hosts')" -i $key `
    "$($Credential.UserName.Split('\')[-1])@$($guest.Address)" hostname 2>&1
if ($LASTEXITCODE -ne 0) { throw "SSH with the platform key did not get in: $probe" }
Write-Host "$Name answers SSH with the platform key: $probe"
