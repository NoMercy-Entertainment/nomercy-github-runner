#!/usr/bin/env bash
# Run only on the new, dedicated ARM64 appliance host, before copying data.
set -euo pipefail
[[ $(hostname) == rnr-windows-arm64 ]] || { echo 'Wrong host' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends qemu-system-arm qemu-utils swtpm rsync libcapstone4 musl
disk=/dev/sdb
root=/var/lib/runner-appliances
[[ $(blockdev --getsize64 "$disk") == 343597383680 ]] || { echo 'Unexpected data disk size' >&2; exit 1; }
if ! blkid "$disk" >/dev/null 2>&1; then
    [[ -z $(lsblk -n -o FSTYPE,MOUNTPOINTS "$disk" | tr -d '[:space:]') ]] || exit 1
    [[ $(lsblk -nr -o NAME "$disk" | wc -l) == 1 ]] || exit 1
    mkfs.ext4 -L arm64-appliances "$disk"
fi
[[ $(blkid -s LABEL -o value "$disk") == arm64-appliances ]] || exit 1
mkdir -p "$root"
uuid=$(blkid -s UUID -o value "$disk")
grep -q "UUID=$uuid " /etc/fstab || printf 'UUID=%s %s ext4 defaults 0 2\n' "$uuid" "$root" >> /etc/fstab
mountpoint -q "$root" || mount "$root"
mkdir -p "$root/windows-arm64" /var/lib/swtpm/windows-arm64-runner /opt/nomercy-qemu-11.1.2
# Do not enable or launch the Windows guest before the final offline sync.
systemctl is-enabled windows-arm64-runner.service >/dev/null 2>&1 && exit 1
df -h "$root"
