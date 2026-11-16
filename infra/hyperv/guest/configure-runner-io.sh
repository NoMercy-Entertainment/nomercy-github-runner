#!/usr/bin/env bash
# Bound build writeback bursts and keep reads responsive on the worker data disk.
# Run on the Linux worker, never on the physical Hyper-V host. No service restart.
set -euo pipefail

action="${1:---check}"
case "$action" in --check|--apply) ;; *) echo 'usage: configure-runner-io.sh [--check|--apply]' >&2; exit 2 ;; esac
mountpoint=/var/lib/runner-data
read -r device filesystem uuid < <(findmnt -rn -M "$mountpoint" -o SOURCE,FSTYPE,UUID)
[[ "$filesystem" == ext4 && "$uuid" =~ ^[a-fA-F0-9-]{36}$ ]] || {
  echo 'Expected a mounted ext4 runner data filesystem with a UUID' >&2; exit 1;
}
device="$(readlink -f "$device")"
[[ "$device" =~ ^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+)$ ]] || {
  echo "Expected a whole runner data disk, found $device" >&2; exit 1;
}
scheduler="/sys/block/${device##*/}/queue/scheduler"
grep -qw mq-deadline "$scheduler" || { echo 'mq-deadline is unavailable' >&2; exit 1; }
# The persistent rule must match the data filesystem, including after device reorder.
udevadm info -q property -n "$device" | grep -Fxq "ID_FS_UUID=$uuid" || {
  echo 'udev does not identify the runner data filesystem' >&2; exit 1;
}

echo "data=$device uuid=$uuid scheduler=$(cat "$scheduler")"
sysctl vm.dirty_background_bytes vm.dirty_bytes vm.dirty_background_ratio vm.dirty_ratio
[[ "$action" == --apply ]] || exit 0
[[ "$(id -u)" == 0 ]] || { echo 'Apply requires root on the Linux VM' >&2; exit 1; }

config=/etc/sysctl.d/90-runner-io.conf
rule=/etc/udev/rules.d/90-runner-io.rules
backup="$(mktemp -d /var/lib/runner-data/runner-io-backup-XXXXXXXX)"
chmod 700 "$backup"
# Save runtime values in an order that also restores ratio-based configurations.
sysctl vm.dirty_background_ratio vm.dirty_ratio > "$backup/runtime.conf"
for key in vm.dirty_background_bytes vm.dirty_bytes; do
  value="$(sysctl -n "$key")"
  [[ "$value" == 0 ]] || echo "$key = $value" >> "$backup/runtime.conf"
done
sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$scheduler" > "$backup/scheduler"
for path in "$config" "$rule"; do
  if [[ -e "$path" ]]; then cp -p "$path" "$backup/${path##*/}"; fi
done
printf '%s\n' "$device" > "$backup/device"
echo "backup=$backup"

rollback() {
  echo 'Apply failed; restoring the previous I/O policy' >&2
  sysctl -p "$backup/runtime.conf" >/dev/null
  cat "$backup/scheduler" > "$scheduler"
  for path in "$config" "$rule"; do
    if [[ -e "$backup/${path##*/}" ]]; then
      cp -p "$backup/${path##*/}" "$path"
    else
      rm -f -- "$path"
    fi
  done
  udevadm control --reload-rules
}
trap rollback ERR

cat > "$config" <<'CONF'
# CI data and runner control traffic share a disk. Bound dirty writeback in
# bytes: a percentage grows with Hyper-V dynamic RAM and permits GiB bursts.
vm.dirty_background_bytes = 67108864
vm.dirty_bytes = 268435456
CONF
printf '%s\n' "ACTION==\"add|change\", SUBSYSTEM==\"block\", ENV{ID_FS_UUID}==\"$uuid\", ATTR{queue/scheduler}=\"mq-deadline\"" > "$rule"
chmod 644 "$config" "$rule"
udevadm control --reload-rules
sysctl -p "$config"
printf '%s\n' mq-deadline > "$scheduler"
[[ "$(sysctl -n vm.dirty_background_bytes)" == 67108864 ]]
[[ "$(sysctl -n vm.dirty_bytes)" == 268435456 ]]
grep -Fq '[mq-deadline]' "$scheduler"
trap - ERR
echo "Applied runner I/O policy; previous state retained in $backup"
