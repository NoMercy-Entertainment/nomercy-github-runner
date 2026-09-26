# Windows 11 ARM64 QEMU guest

The Windows ARM64 guest uses `qemu-system-aarch64` on the existing Ubuntu
`macos-runner` Hyper-V VM. It has its own qcow2 disk, UEFI variables and TPM
state. The macOS QEMU guest and its registration are independent. The physical
host is x86_64, so QEMU uses multithreaded TCG software emulation, not KVM.

## Current deployment (2026-09-26)

The `windows-arm64-runner.service` guest boots the original Windows 11 ARM64
ISO with a separate ARM64-only answer ISO derived from
`D:\Downloads\autounattend.xml`. The source file's Winhance script is kept
byte-for-byte. The answer file selects Windows 11 Pro by name (index 3 in the
original `install.wim`), creates `admin`, partitions the blank NVMe disk and
runs Winhance during the `specialize` pass. The answer ISO uses ISO9660 level 2
so `AUTOUNATTEND.XML` has its full name even without Joliet.

The clean reinstall was started on 2026-09-26. **Installation and GitHub/Forgejo
registration are not yet verified.** `systemctl is-active` proves only that
QEMU is running. Check Setup, the installed edition, Winhance logs and runner
services before calling it a runner. The former guest disk, answer ISO, UEFI
variables and TPM state are archived with `.pre-unattended-20260926` names.

| Item | Location |
| --- | --- |
| Host | `runner@10.77.0.40`, SSH key `~/.ssh/macos_runner` (existing host key alias `172.19.136.46`) |
| Original ISO | `/var/lib/runner-appliances/windows-arm64/Windows11Arm64-original.iso` |
| Answer ISO | `/var/lib/runner-appliances/windows-arm64/windows-arm64-answer.iso` (ARM64-only Setup settings and the user's Winhance choices; root-readable because it contains a password) |
| ARM64 driver ISO | `/var/lib/runner-appliances/windows-arm64/virtio-win.iso` |
| Driver ISO SHA-256 | `303f7ae40dad495d6ae474fdc571df58958a4dbc5c37a522d80f9a203867949d` |
| Guest tool ISO | `/var/lib/runner-appliances/windows-arm64/payload.iso` (ARM runner packages, NSSM and `Prepare-WindowsArm.ps1`) |
| Guest disk | `/var/lib/runner-appliances/windows-arm64/windows-arm64.qcow2`, 120 GiB virtual |
| Previous failed attempt | `/var/lib/runner-appliances/windows-arm64/windows-arm64.failed-pro-20260925.qcow2` (retained for diagnosis) |
| TPM state | `/var/lib/swtpm/windows-arm64-runner/` |
| Service | `/etc/systemd/system/windows-arm64-runner.service` |
| Guest network | QEMU user networking; SSH `127.0.0.1:52222`, RDP `127.0.0.1:53389`, agent `10.77.0.40:8445` on the Ubuntu host |
| Guest console | VNC `127.0.0.1:5903` on the Ubuntu host |

The answer file creates local administrator `admin` with the password entered
through the host's masked prompt. The password is not copied into this
repository or the QEMU launch command. Windows answer files necessarily carry
the credential in recoverable form; keep the answer ISO restricted to root.

`start-qemu.sh` uses the distribution's AAVMF firmware, an NVMe system disk,
USB installer and VirtIO driver media, a VirtIO network card, a private
software TPM, 4 virtual CPUs and 8 GiB RAM. The driver ISO contains
`NetKVM/w11/ARM64/netkvm.inf`. The unattended media bypasses Secure Boot
checks; the AAVMF firmware is the non-Secure-Boot variant. Windows may need
its ARM64 network driver installed before the forwarded SSH/RDP ports work.
The Winhance choices remove `OpenSSH.Server`, so SSH must be installed again
after Setup and the debloat pass finish.
`Prepare-WindowsArm.ps1` on the guest tool ISO locates the attached ARM64
VirtIO network driver, installs it, then reinstalls SSH and enables RDP.
It installs the platform's public SSH key for the `admin` account; the private
key remains on the Windows host at `D:\HyperV\runner-platform\ssh\id_ed25519`.
The tool ISO is attached to the current QEMU guest and future starts.

## Observe and recover

On the Ubuntu host:

```sh
systemctl status windows-arm64-runner.service
sudo journalctl -u windows-arm64-runner.service -n 50 --no-pager
sudo qemu-img info -U /var/lib/runner-appliances/windows-arm64/windows-arm64.qcow2
sudo python3 /home/runner/monitor.py screendump /tmp/windows-arm.ppm
```

The `-U` flag is required for a read-only image inspection while QEMU owns
the disk. The monitor script is in this directory and is staged in
`/home/runner/monitor.py` on the host. QEMU writes a PPM screenshot; copy it
over SSH to view it. For interactive VNC, tunnel from the Windows host:

```powershell
ssh -o HostKeyAlias=172.19.136.46 -i "$env:USERPROFILE\.ssh\macos_runner" -L 5903:127.0.0.1:5903 runner@10.77.0.40
```

Then connect a VNC viewer to `127.0.0.1:5903`. If an unfinished install lands
in the UEFI shell, the installer is `fs0:\efi\boot\bootaa64.efi`; press a key
at the CD boot prompt. After Windows installs, its boot manager must boot from
the NVMe disk without that manual command.

`systemctl stop windows-arm64-runner.service` requests QEMU termination; do
this only after Windows has shut down cleanly. The disk and UEFI/TPM state are
retained. Never remove or replace them as part of a routine restart.

## Runner packages already prepared on the Windows host

* GitHub: `D:\HyperV\runner-platform\artefacts\actions-runner-win-arm64-2.336.0.zip`,
  SHA-256 `b3799e9cf754fe4dfcb3d220c9701c924829737ee815dbeb674f8bd076794504`
  from GitHub's v2.336.0 release notes.
* Forgejo: `D:\HyperV\runner-platform\artefacts\forgejo-runner-v13.1.0\forgejo-runner-v13.1.0-windows-arm64.exe`,
  SHA-256 `cd59d117346c32419ddb62679150ba2f11ff7f6fbbf64b7400830287c4b15b07`,
  built from v13.1.0 with `images/windows/build-forgejo-runner.sh`.

Register two separate listeners, one with each forge, after checking Windows
boot, networking and guest tools. Use `Windows`/`ARM64` for the GitHub runner
and a dedicated `windows-arm64:host` Forgejo label. Do not reuse the existing
Windows x64 runner's registration or `windows-latest` label.

The Windows control agent can use the existing `windows-process` runtime
inside the guest, declaring `capacity.architecture=arm64`. Its advertised
endpoint is `https://10.77.0.40:8445`, forwarded to guest
`10.0.2.15:8443`; the QEMU host forward appears to the guest as `10.0.2.2`.
After the preparation script has enabled SSH, run
`infra/hyperv/Install-WindowsArmGuestWorker.ps1` from the Windows host to
enrol and install the agent with the ARM64 templates. It uses
`Install-WindowsWorker.ps1` without `-WindowsStorage` (the
Hyper-V VHD cmdlets are unavailable inside this QEMU guest). Controller
enrolment, guest agent installation and fleet creation are still pending.
