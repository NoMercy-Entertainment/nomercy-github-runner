param([switch]$ResumeHost)
$ErrorActionPreference = 'Stop'
$watchStage = 'D:\HyperV\runner-platform\stage\resource-uniformity-20261009\event-watch'
$conf = 'D:\HyperV\runner-platform\stage\arm-isolation-20261009\ssh.conf'
$ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
$scp = 'C:\Windows\System32\OpenSSH\scp.exe'
New-Item -ItemType Directory -Path $watchStage -Force | Out-Null
$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $watchStage /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*$($ownerSid):(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect watcher config directory' }
if (-not $ResumeHost) {
$random = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($random)
$watchToken = [Convert]::ToHexString($random)
$created = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([datetime]::UtcNow, 'W. Europe Standard Time').ToString('yyyy-MM-dd HH:mm:ss')
@{token=$watchToken;port=12308} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $watchStage 'receiver.json') -Encoding utf8
@{token=$watchToken;port=12308;host='10.0.2.2';created_local=$created} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $watchStage 'arm-watch.json') -Encoding utf8
& $scp -F $conf (Join-Path $PSScriptRoot 'watch_arm_servicing.py') (Join-Path $watchStage 'receiver.json') arm-new-host:/tmp/
if ($LASTEXITCODE -ne 0) { throw 'Host watcher transfer failed' }
$setup = @'
set -eu
root=/var/lib/runner-appliances/windows-arm64/recovery-watch
if test -e /etc/systemd/system/arm-recovery-watch.service; then echo 'Watcher service already exists'; exit 1; fi
install -d -m 700 "$root"
install -m 600 /tmp/receiver.json "$root/receiver.json"
install -m 700 /tmp/watch_arm_servicing.py "$root/receiver.py"
rm -f /tmp/receiver.json
cat > /etc/systemd/system/arm-recovery-watch.service <<'UNIT'
[Unit]
Description=ARM Windows servicing event receiver
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /var/lib/runner-appliances/windows-arm64/recovery-watch/receiver.py serve --directory /var/lib/runner-appliances/windows-arm64/recovery-watch
UMask=0077
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl start arm-recovery-watch.service
systemctl is-active arm-recovery-watch.service
'@
$setup | & $ssh -F $conf arm-new-host 'tr -d "\r" | sudo bash -s'
if ($LASTEXITCODE -ne 0) { throw 'Host receiver installation failed' }
} else {
    $expected = (Get-FileHash -LiteralPath (Join-Path $watchStage 'receiver.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $actual = & $ssh -F $conf arm-new-host 'sudo sha256sum /var/lib/runner-appliances/windows-arm64/recovery-watch/receiver.json'
    if ($LASTEXITCODE -ne 0 -or -not $actual.StartsWith($expected)) { throw 'Existing receiver config does not match this installation' }
    & $ssh -F $conf arm-new-host 'sudo systemctl is-active arm-recovery-watch.service'
    if ($LASTEXITCODE -ne 0) { throw 'Existing receiver is not active' }
}
& $scp -O -F $conf -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 (Join-Path $PSScriptRoot 'guest\watch_windows_servicing.py') 'D:\HyperV\runner-platform\stage\resource-uniformity-20261009\install-arm-watch-guest.py' (Join-Path $watchStage 'arm-watch.json') arm-new-guest:C:/Users/admin/
if ($LASTEXITCODE -ne 0) { throw 'Guest watcher transfer failed' }
& $ssh -F $conf -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 arm-new-guest '"C:\ProgramData\nomercy\agent\python\python.exe" C:\Users\admin\install-arm-watch-guest.py'
if ($LASTEXITCODE -ne 0) { throw 'Guest watcher installation failed' }
