#!/usr/bin/env bash
# Run on the old macOS host only after the controller confirms both ARM runners drained.
set -euo pipefail
[[ $(hostname) == macos-runner ]] || exit 1
unit=windows-arm64-runner.service
pid=$(systemctl show "$unit" -p MainPID --value)
[[ $pid -gt 0 ]] || exit 1
date -u '+ACPI-SHUTDOWN-REQUESTED %FT%TZ'
python3 - <<'PY'
import socket
with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(10)
    client.connect('/var/lib/runner-appliances/windows-arm64/monitor.sock')
    def response():
        data = b''
        while not data.endswith(b'(qemu) '):
            chunk = client.recv(65536)
            if not chunk:
                raise RuntimeError('Monitor closed before acknowledgement')
            data += chunk
        return data
    response()
    client.sendall(b'system_powerdown\n')
    response()
    print('ACPI command acknowledged by QEMU')
PY
for ((attempt=0; attempt<300; attempt++)); do
    if [[ $(systemctl show "$unit" -p MainPID --value) == 0 ]]; then
        [[ $(systemctl is-active "$unit" || true) == inactive ]] || exit 1
        [[ $(systemctl show "$unit" -p ExecMainStatus --value) == 0 ]] || exit 1
        systemctl disable "$unit"
        date -u '+CLEAN-SHUTDOWN-CONFIRMED %FT%TZ'
        exit 0
    fi
    sleep 2
done
echo 'ACPI shutdown timed out; no forced termination was performed' >&2
exit 1
