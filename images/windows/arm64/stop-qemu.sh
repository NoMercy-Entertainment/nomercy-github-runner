#!/usr/bin/env bash
# Clean ACPI shutdown, including during shutdown of the dedicated Hyper-V host.
set -euo pipefail
socket=/var/lib/runner-appliances/windows-arm64/monitor.sock
pid=${1:-}
# Windows can shut itself down before systemd invokes ExecStop.
[[ "$pid" =~ ^[1-9][0-9]*$ ]] || exit 0
python3 - "$socket" "$pid" <<'PY'
import os, select, socket, sys
try:
    process = os.pidfd_open(int(sys.argv[2]))
except ProcessLookupError:
    sys.exit(0)
try:
    if select.select([process], [], [], 0)[0]:
        sys.exit(0)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(sys.argv[1])
        def response():
            data = b''
            while not data.endswith(b'(qemu) '):
                chunk = client.recv(65536)
                if not chunk:
                    if select.select([process], [], [], 0)[0]:
                        return
                    raise RuntimeError('Monitor closed before acknowledgement')
                data += chunk
            return data
        response()
        client.sendall(b'system_powerdown\n')
        response()
    # The kernel signals the process descriptor when QEMU exits. No repeated
    # process lookup, sleeps or polling during the Windows shutdown.
    if not select.select([process], [], [], 560)[0]:
        raise TimeoutError('Windows did not complete ACPI shutdown within 560 seconds')
finally:
    os.close(process)
PY
