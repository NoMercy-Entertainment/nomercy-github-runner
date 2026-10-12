#!/usr/bin/env bash
# Run only after the network-fenced source QEMU has stopped: either after
# Windows shutdown or after a verified full RAM/CPU/TPM save. Restore a saved
# execution state before allowing a saved guest to run normally.
set -euo pipefail
[[ $(hostname) == rnr-windows-arm64 ]] || exit 1
mountpoint -q /var/lib/runner-appliances || exit 1
[[ $(findmnt -no LABEL /var/lib/runner-appliances) == arm64-appliances ]] || exit 1
[[ $(systemctl is-active windows-arm64-runner.service || true) == inactive ]] || exit 1
transport='ssh -i /home/rnr-admin/.ssh/arm-migration -o BatchMode=yes -o StrictHostKeyChecking=yes'
$transport runner@10.77.0.40 'test "$(systemctl is-active windows-arm64-runner.service || true)" = inactive && test "$(systemctl show windows-arm64-runner.service -p MainPID --value)" = 0'
# Force content checks even when a live precopy left a matching timestamp/size.
# Delta transfer and in-place writes keep the outage to the changed blocks.
rsync -aSr --checksum --checksum-choice=xxh128 --inplace --no-whole-file \
    --bwlimit=120000 --itemize-changes --stats \
    --files-from=/home/rnr-admin/arm-migration-files --rsync-path='sudo -n rsync' \
    -e "$transport" runner@10.77.0.40:/ /
for disk in windows-arm64.qcow2 windows-arm64-runners.qcow2; do
    qemu-img check "/var/lib/runner-appliances/windows-arm64/$disk"
done
# Firmware and TPM state must match the now-stopped source exactly.
critical='var/lib/runner-appliances/windows-arm64/AAVMF_VARS.fd var/lib/swtpm/windows-arm64-runner/tpm2-00.permall usr/share/AAVMF/AAVMF_CODE.no-secboot.fd'
$transport runner@10.77.0.40 "cd / && sudo sha256sum $critical" > /home/rnr-admin/arm-critical-source.sha256
(cd / && sha256sum -c /home/rnr-admin/arm-critical-source.sha256)
bash -n /var/lib/runner-appliances/windows-arm64/start-qemu.sh
bash -n /var/lib/runner-appliances/windows-arm64/stop-qemu.sh
systemd-analyze verify /etc/systemd/system/windows-arm64-runner.service /etc/systemd/system/windows-arm64-novnc.service
touch /home/rnr-admin/arm-final-sync.ready
echo 'FINAL-SYNC-VERIFIED'
