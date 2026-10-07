#!/usr/bin/env bash
# One Windows ARM64 QEMU guest on the same Linux host that runs macOS QEMU.
set -euo pipefail

root=/var/lib/runner-appliances/windows-arm64
firmware=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd
iso="$root/Windows11Arm64-github-0db35390-arm64-20260929.iso"
drivers="$root/virtio-win.iso"
disk="$root/windows-arm64.qcow2"
runner_disk="$root/windows-arm64-runners.qcow2"
vars="$root/AAVMF_VARS.fd"
tpm_root=/var/lib/swtpm/windows-arm64-runner
tpm_socket="$tpm_root/swtpm.sock"

for file in "$firmware" "$iso" "$disk" "$runner_disk" "$vars"; do
    if [[ ! -f "$file" ]]; then
        echo "Missing $file" >&2
        exit 1
    fi
done
if [[ ! -f "$drivers" ]]; then
    echo "Missing $drivers (Windows ARM64 network driver media)" >&2
    exit 1
fi

rm -f -- "$tpm_socket"
swtpm socket --tpm2 --tpmstate "dir=$tpm_root" \
    --ctrl "type=unixio,path=$tpm_socket" --daemon

# Windows uses local time for its hardware clock (W. Europe Standard Time).
# Scope TZ to this guest; the Ubuntu host and other guests keep their timezone.
export TZ=Europe/Amsterdam
exec /opt/nomercy-qemu-11.1.2/bin/qemu-system-aarch64 \
    -name windows-arm64-runner \
    -L /opt/nomercy-qemu-11.1.2/share/qemu \
    -machine virt-8.2,gic-version=3 \
    -accel tcg,thread=multi \
    -cpu max,pauth-impdef=on \
    -smp 8 -m 8G \
    -rtc base=localtime,clock=host \
    -drive "if=pflash,format=raw,unit=0,readonly=on,file=$firmware" \
    -drive "if=pflash,format=raw,unit=1,file=$vars" \
    -device ramfb \
    -device qemu-xhci -device usb-kbd -device usb-tablet \
    -drive "if=none,id=system,format=qcow2,file=$disk" \
    -device nvme,drive=system,serial=RNRWINARM64,bootindex=1 \
    -drive "if=none,id=runnerdata,format=qcow2,file=$runner_disk" \
    -drive "if=none,id=install,format=raw,media=cdrom,readonly=on,file=$iso" \
    -device usb-storage,drive=install,bootindex=2 \
    -drive "if=none,id=drivers,format=raw,media=cdrom,readonly=on,file=$drivers" \
    -device usb-storage,drive=drivers \
    -netdev 'user,id=net0,hostfwd=tcp:127.0.0.1:52222-:22,hostfwd=tcp:127.0.0.1:53389-:3389,hostfwd=tcp:0.0.0.0:8445-:8443' \
    -device virtio-net-pci,netdev=net0 \
    -device nvme,drive=runnerdata,serial=RNRARM64DATA \
    -chardev "socket,id=chrtpm,path=$tpm_socket" \
    -tpmdev emulator,id=tpm0,chardev=chrtpm \
    -device tpm-tis-device,tpmdev=tpm0 \
    -display none -vnc 127.0.0.1:3 \
    -monitor "unix:$root/monitor.sock,server=on,wait=off"
