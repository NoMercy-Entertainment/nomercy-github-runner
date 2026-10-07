# Windows 11 ARM64 QEMU guest

## ARM runner provisioning in progress (2026-10-02)

### Targeted installer startup investigation (18:50 UTC)

The manual elevation-service start also failed (`The handle is invalid`),
so that recovery is finished. Read-only inspection of Microsoft's installed
setup assembly confirms `ExistingRpcServerConnector` has a fixed 60,000 ms
connection timeout; the elevation connector kills its child on failure.
The elevated installer log remained empty before this timeout. Slow CLR
startup is therefore the next hypothesis to test, not a confirmed fix.

`Prepare-ArmVsNativeImages.py` prepares native images with the installed
ARM64 .NET Framework NGen for setup.exe and VSInstallerElevationService.exe,
verifies the original binaries' SHA-256 hashes remain unchanged, then makes
one attempt to resume the tool bootstrap. No installer binaries are patched.
Read `arm-vs-native-images-result.json`, `arm-ngen-setup.log`,
`arm-ngen-VSInstallerElevationService.log`, and
`arm-tools-after-ngen.stdout.log` in ProgramData/nomercy. This runs without
model-driven polling and stops on failure. CI verification remains pending.

### Installer service timeout recovery (18:23 UTC)

The shared installer update succeeded, but the resumed C++ setup exited at
18:16 UTC with `0x80131505`: its named-pipe connection to the installer
service timed out. No setup/MSI processes remained at 18:22 UTC.
`Resume-ArmToolsWithInstallerService.ps1` now starts
`VSInstallerElevationService` separately, waits for Running, then invokes
the existing tool bootstrap. Read `arm-installer-service-recovery.txt`
and `arm-tools-service-recovery.stdout.log` under ProgramData/nomercy.
The earlier repair PID 5260 is finished; do not use its log as live status.
No runner listeners have been enrolled yet.

### Build Tools installer recovery started at 17:31 UTC

Bootstrap PID 6376 stopped at 16:51 UTC with Visual Studio exit 5008.
The bootstrapper log records a cancelled latest-installer-feed request,
then installer version 3.14 below the required 4.10 minimum. GitHub CLI
and 7-Zip had completed. No installer processes remained at recovery start.

Native PowerShell PID 5260 runs `Repair-ArmVsInstaller.ps1`: it downloads
Microsoft's stable bootstrapper, validates its Microsoft Authenticode
signature, updates only the shared installer (`--installerOnly`), then
resumes `Run-ArmTools.ps1`. Output is
`C:\ProgramData\nomercy\arm-tools-installer-repair.stdout.log`; repair state
is `arm-installer-repair-result.txt`. Do not start a second bootstrap.
The C++ helper now includes a single installer-update recovery for exit
5008; the previous helper is in `Recovery\cpp-installer-before-20261002`.
Runner enrollment and service-account build verification remain pending.

### LLVM ARM64 completed at 16:35 UTC

The active bootstrap is native PowerShell PID 6376. WinGet selected
`LLVM-22.1.8-woa64.exe`; the installer is PID 5168, parent WinGet 1608.
WinGet reported successful completion at 16:35:59 UTC; both installer
processes have ended and the bootstrap advanced to GitHub CLI. The installed
`C:\Program Files\LLVM\bin\clang-cl.exe` has PE machine `0xaa64` (ARM64).
Actual compilation is still pending the MSVC/SDK installation and CI jobs.
The controller confirmed a fresh healthy worker heartbeat at 16:17:20 UTC.

### Ruby completed and bootstrap resumed at 15:46 UTC

Ruby 3.3.12-1 with DevKit completed successfully. WinGet and both installer
processes exited with code 0; the installer reported no reboot requirement.
The supervisor verified the Ruby and UCRT64 GCC executables. Its detached
PowerShell attempt (PID 6316) exited 0 without producing output or updating
the bootstrap marker; that was not tool completion. PowerShell's version
probe worked. A new launch with CREATE_NO_WINDOW (PID **6376**) successfully
started transcript `arm-tools-resume-20261002-155030.log` and advanced to
LLVM/Clang after recognizing all existing tools, including Ruby.

Current output: `C:\ProgramData\nomercy\arm-tools-resume-1550.stdout.log`.
Current marker: `arm-tool-bootstrap-result.txt` (`20261002-155030` while
running). The supervisor's `arm-tools-after-ruby-result.json` describes the
earlier failed-to-start attempt and is no longer the live bootstrap status.
PIDs 7028 and 6316 have exited and must not be used for liveness.

### Service transition correction at 14:37 UTC

NSSM can return a nonzero exit code with SERVICE_START_PENDING or
SERVICE_STOP_PENDING although Windows is still completing the requested
operation. The runtime now confirms the final state with a bounded wait;
unknown state still fails closed. Targeted tests: **135 passed, 40 skipped**.
Updated runtime SHA-256:
`d1fa99e7961c3403b9779a4660807bc16fbbd502f0245cb96fa3bc930e3c7b9d`.
The prior module is backed up under
`C:\ProgramData\nomercy\Recovery\service-transition-before-20261002`.

The deployment wrapper exited after replacing the module and stopping the
agent, without recording its final result. The interactive monitor and main
tool bootstrap also exited; the Ruby installer remained active. The cause
has not been established. A fresh native ARM interpreter successfully
imported the updated runtime and checked its environment. The agent was
started separately at 14:47 UTC; the controller confirmed a fresh healthy
heartbeat at 14:49:38 UTC. The deployment result marker records that check.
Do not rerun the installer until the still-running Ruby installer finishes.

At 14:52 UTC, detached supervisor PID 6748 was started from
`C:\Users\admin\Resume-ArmToolsAfterRuby.py`. It holds verified handles to
WinGet 6816 and Ruby installer processes 5364/4440, waits for their final
exit codes, and only then resumes `Run-ArmTools.ps1`. The native Rust
completion marker and current bootstrap source hash are checked first.
Progress is in `arm-tools-after-ruby-result.json` and
`arm-tools-after-ruby-supervisor.log`; the resumed tool output goes to
`arm-tools-after-ruby.stdout.log`. Do not launch a competing bootstrap.

Before the planned cold start, `D:\runners\.arm-cold-start-probe` was written;
its SHA-256 is recorded in `C:\ProgramData\nomercy\arm-data-before-cold-start.json`.
Verify this existing file after the data disk moves from USB to NVMe, then
remove only that probe. Free space at 15:28 UTC: C: 85.9 GiB, D: 119.9 GiB.

### Native Rust ready at 14:35 UTC

The official ARM64 rustup installer was downloaded with its published SHA-256
and verified before execution (`01aa49cf9574a8bd0ae52005d7de2590e8f27181ded6748236e702c92aef826d`).
It installed Rust 1.99.0, Cargo, Clippy and rustfmt using the minimal profile,
under `C:\Rust\cargo` and `C:\Rust\rustup`. The native compiler reports
`aarch64-pc-windows-msvc`; its PE architecture was checked. The separate
wrapper exited successfully. A real C++-linked Cargo build is still pending.
The main bootstrap will publish the machine environment when it reaches
its existing Cargo step. Rust installation did not change PATH concurrently
with the running Ruby installer. Logs/result are `arm-rustup-preinstall.log`
and `arm-rustup-preinstall-result.txt` under `C:\ProgramData\nomercy`.

### Runner profile and cache correction at 14:14 UTC

The plain-directory Windows backend previously assigned HOME/USERPROFILE
and APPDATA/LOCALAPPDATA only when per-runner VHD storage was enabled.
The ARM guest uses directories on its data disk. Both backends now put
profiles inside the owned work/cache trees, with per-runner Cargo, .NET,
NuGet, Gradle, npm, pip, Go build and Go module caches. The preinstalled
RUSTUP_HOME remains shared; job Cargo caches no longer use the machine's
installation directory.

The targeted runtime/storage/registration suite passed **129 tests**;
**40 were skipped** because they require Windows or Hyper-V facilities.
A regression test verifies that cleanup removes these caches while retaining
another runner's files, shared installed tools and registration files.
Both real CI workflows now also check writable profile/cache locations.
Health commits: GitHub `262b6be28ec9c22926eecae3404d89d5ea6c1806`, Forgejo
test branch `8718ff14bb865bc6af0d05243571cdddc29cb3e9`.

Deployment helper: `C:\Users\admin\deploy-arm-cache-profiles.py`.
It saves the original source and live module under
`C:\ProgramData\nomercy\Recovery\profile-cache-before-20261002`.
The new module SHA-256 is
`9785aa4243da5915718c7ac40cde0bcd76066976088d55d596c5e621600771aa`.
The native ARM import/environment probe passed and the agent restarted.
The controller confirmed a healthy worker again at 14:16 UTC.
Watch `arm-cache-profile-deploy-final.log` and
`arm-cache-profile-deploy-result.txt` in `C:\ProgramData\nomercy`.

Two guarded deployment attempts stopped before modifying code: the service
enumeration buffer was larger than Windows' 256 KiB limit (error 1783),
then NSSM returned exit 1 while the service was still STOP_PENDING. The
helper now uses a bounded enumeration and explicitly waits for final
states using native SC queries. The original backups are verified on retry.
The interactive Python monitoring process also exited during this interval;
it was reopened. The bootstrap PID 7028 remained active and Ruby continued
installing. The latest Application Error event is from October 1, not this
monitor-session exit. Its cause has not been established.

### Tool installation progress at 13:33 UTC

The bootstrap has completed Go 1.27.0 ARM64, Temurin 21.0.12.101 x64,
the VC++ redistributable dependency and PHP 8.4.25 x64. It is now installing
Ruby 3.3.12 with DevKit. The native Java correction below is deployed but
will execute at the Android SDK preparation stage; the currently installed
Temurin JDK is not yet the intended default for this ARM worker.

### Native Java preparation at 13:28 UTC

WinGet selected Temurin 21.0.12.101 **x64** for its current latest manifest.
Its MSI had already started when checked. The WinGet parent was briefly
suspended to inspect that boundary, then immediately resumed; the MSI and
bootstrap continue intact. Do not terminate their active installation.

Future ARM bootstrap runs select `Microsoft.OpenJDK.21`. The Android SDK
preparation also ensures that a native Microsoft Java 21 is installed before
running SDK Manager, verifies `java.exe` has PE machine `0xaa64`, and places
its bin directory first on machine PATH. It sets both `JAVA_HOME` and
`JAVA_HOME_21_ARM64`. This catches the current bootstrap too: its top-level
tool loop was already loaded, but it loads the Android helper from disk later.
The existing Temurin installation can remain alongside native Java.

Both updated scripts are deployed and SHA-256 verified in the guest source
tree. Prior copies are under
`C:\ProgramData\nomercy\Recovery\java-native-preparation-20261002`.
`Install-RunnerTools.ps1`: `f0dc8b34b04e5ede4eb0b369823fbf8ce2f45210de926abb2909f6d66bd6f732`.
`Install-AndroidSdk.ps1`: `d4898b794e4a50e5e7de4d63709685d41bc96cbe49e7b6c1a0ee8aae6608be1c`.
PowerShell parsing and `git diff --check` passed. Actual native Java install
and CI execution remain pending.
The C++ helper's quiet installer now also starts with `-WindowStyle Hidden`;
that one-line deployment was verified against SHA-256
`45c2c01cfd01ce56a3963b00d0753a6df22b2778406524492854e2999ac57052`,
with its original copy in the same recovery directory.

### Tool installation progress at 12:36 UTC

CMake 4.4.3 ARM64 completed successfully; the bootstrap advanced to the
Go 1.27.0 ARM64 MSI. The long CMake `InstallFinalize` phase continued doing
CPU work and thousands of writes before returning successfully. The guest
bootstrap remains PID 7028 and has not been restarted. Neither ARM fleet
has been created or tested yet.

At 13:05 UTC, the shared CI health script was expanded to build and execute
both a CMake/NMake C++ project and a Clang-CL C++ program, checking each
resulting PE architecture. PowerShell syntax validation passed. Published
health commits: GitHub `82f8db342f5083be85b052d5753bab9205e7c4dd`, Forgejo
test branch `9fca72e35e60f3d240a9c156aa051664f0c8001e`. Neither workflow has
been dispatched. Go 1.27.0 ARM64 completed successfully at 13:18 UTC;
the bootstrap advanced to Java.

The offline checkpoint helper now staged at
`/tmp/arm-checkpoint-installed-tools.py` refuses to run while either ARM
QEMU or its TPM daemon is active. It verifies both disk copies, firmware
variables and TPM state. A clean cold start is still pending to activate
the already configured NVMe data disk attachment.

### Registration timing correction at 12:19 UTC

A read-only Windows PowerShell startup plus ACL query succeeded, but took
**229.97 seconds** under TCG. This exceeds the previous 30-second key setup
and 105-second registration-script deadlines. `agent/windows_timeouts.py`
now supplies ARM64-specific bounds: key setup 600 s, service-side script
900 s, authenticated pipe 915 s, parent client 940 s. Both ARM template
registration children are bounded at 600 s. The outer controller deadline
remains 1790 s. Existing x64 bounds and security boundaries are preserved.
The native `whoami /user /fo csv /nh` identity probe completed successfully
in 2.36 seconds under the same installation load (measured with Windows
process creation/exit timestamps); its existing 10-second bound is retained.
The installed NSSM wrapper returned `SERVICE_RUNNING` for `rnr-agent` in
4.09 seconds (exit 0), within the existing 30-second status-query bound.

Targeted tests: **88 passed, 8 skipped** on the Linux test host. The skipped
tests require a real Windows kernel; actual registration and CI jobs remain
the final checks. Two existing test-only ACL lookups used host `os.path`
instead of Windows `ntpath`; those were corrected so the same isolation
assertions run on Linux. PowerShell templates passed syntax parsing.

The checked patch bundle and `Deploy-ArmRegistration.ps1` are staged in
the ARM admin profile. Deployment completed at 12:21 UTC (exit 0), with prior agent/template
files backed up under
`C:\ProgramData\nomercy\Recovery\registration-before-arm-timeouts-20261002`.
Watch `C:\ProgramData\nomercy\arm-registration-deploy-result.txt` and its
adjacent transcript. Only this ARM agent was updated. Its native Python
import check confirmed the ARM limits and the service restarted successfully.

.NET SDK 10.0.401 finished and the bootstrap advanced to CMake 4.4.3.
Neither ARM fleet has been created yet.

### Storage launch correction at 12:05 UTC

QEMU's device tree showed the hot-added data disk behind the auto-created
USB hub at **12 Mbps** (`runnerdata-usb`, port 4.7). The data disk is empty
apart from its write probe; no ARM listeners have been created yet.
The persistent launcher now attaches that same qcow2 as NVMe with serial
`RNRARM64DATA`, after the existing NIC in device declaration order so its
PCI location does not change. **A clean guest shutdown and QEMU cold start
are still required to activate this.** A guest-only reboot is insufficient.
Do not interrupt an installer to switch storage.

The prior launcher is retained as `start-qemu.before-data-nvme-20261002.sh`.
Updated launcher SHA-256:
`43f4885a1fb19b343e6b4ba2d68a6b0c91ffa31b5da0d3682415ac0f52856ad7`.
`bash -n` passed. Verify D: still resolves to `RNR_ARM_DATA` after the cold
start, before allowing jobs. Its existing filesystem must not be formatted.

The five temporary USB media devices `armprep-usb`, `armdiag-usb`,
`armsshmsi-usb`, `armrunnerpayload-usb`, and `armtools-usb` were detached.
Their ISO files remain available for recovery. Agent templates and active
tool installation sources are all on C:; no installer was stopped. The
original Windows/driver media and both writable disks remain attached.

### Completed setup cleanup at 11:47 UTC

Windows reported `OOBEInProgress`, `SystemSetupInProgress`, `SetupPhase` and
`SetupType` all zero. The four temporary CloudExperienceHost additions
described below were verified against their recorded hashes and backed up
under `C:\ProgramData\nomercy\Recovery\retired-oobe-trials-20261002`.
They were then removed, and the two temporary OOBE timeout values were
removed to restore their original absence. Original Windows package files
and the answer file were not rewritten. The guest cleanup script exited 0;
log: `C:\ProgramData\nomercy\arm-oobe-cleanup.log`. Earlier failed deletion
attempts are preserved in the log. Explicit `SetFileAttributesW` was needed
to clear the ISO read-only attributes; the first temporary file was also
taken into Administrators ownership before its eventual deletion. Package
directory permissions were not changed.

Python 3.12 ARM64 and pip completed successfully at 11:33 UTC; machine PATH
includes `C:\Program Files\Python312-arm64` and its Scripts directory.
.NET SDK 10.0.401 ARM64 is now installing runtime/SDK components. Other
build tools and the real CI checks remain pending. The ARM agent is online.

### Agent online at 11:21 UTC

The guest agent installer completed with result `agent-installed`. The
native ARM64 Python service serves on `10.0.2.15:8443` and reports to
`https://10.77.0.10:8444`. The controller confirmed `windows-arm64-1`
**healthy** at 11:21:26 UTC. Guest installation log:
`C:\ProgramData\nomercy\arm-agent-install.log`.

Only the two ARM fleets were configured: explicit ARM templates and labels,
3 GiB per listener, each able to use all eight shared guest CPUs. Both
desired counts remain zero while tools install. Python's native ARM64
machine installation is still progressing; Node/Git/PowerShell are present.

The temporary worker-certificate tar was imported and removed in the guest.
Cleanup of the transfer copies was rejected by automatic approval review;
restricted copies remain at
`D:\HyperV\runner-platform\stage\arm-enrol-private-20261002\bundle.tar`,
controller-host `/tmp/arm-worker-bundle-20261002.tar`, and the same temporary
path inside `rnr-controller`. They contain only this worker's identity, not
the CA private key. Do not publish these files.

### Build environment progress at 11:10 UTC

Git completed, but the old WinGet 1.11 client returned `E_ABORT` after its
successful installer. App Installer also updated to WinGet 1.29.380 during
this period; the cause of the abort is not proven. The old tools script
retried every nonzero exit without machine scope, unnecessarily installing
Git a second time. That second installer was allowed to finish safely
(10:53:29 UTC), then its old bootstrap parent was stopped before it could
install anything else. No active file replacement was interrupted.

The tools script now retries without scope only for `0x8A150010`
(no applicable installer), and disables interactive prompts. The patched
guest file is SHA-256
`1ed09e2265cdec3ef1b31bc02ce3640d03131264a5ee235bb114103c4ec0198d`.
The new native PowerShell bootstrap started at 10:54:55 UTC. Git/Bash and
PowerShell resolve from machine PATH, Node.js has installed, and Python
3.12 is installing. Progress/result files are
`C:\ProgramData\nomercy\arm-tools-resume.stdout.log` and
`C:\ProgramData\nomercy\arm-tool-bootstrap-result.txt`.
Do not rerun `J:\bootstrap.ps1`: it would extract the older source archive.

Both checked runner binaries successfully executed `--version` in the
guest: Forgejo v13.1.0 and GitHub 2.336.0. Their binaries and NSSM are now
staged in `C:\ProgramData\nomercy\src`, independent of the payload DVD.
The controller enrolled `windows-arm64-1` at 11:10 UTC; guest agent
installation is being prepared. Both ARM fleet desired counts remain zero
until build tools are ready. No ARM CI job has passed yet.

### Build environment progress at 10:30 UTC

Git's installer reported `Installation process succeeded` at 10:29:52 UTC.
The parent WinGet/bootstrap still has to return and continue with the other
tools. A persistent, separate native Python SSH session now reads the guest
logs directly; the read-only NBD export was stopped after that worked.
The agent's native Python has also successfully imported `agent.jobhost`
and `agent.runtimes.windows_process` in the guest.

A missing data disk was found before enrollment: the runtime intentionally
uses `D:\runners`, while the ARM guest's D: was its Windows installation DVD.
A new sparse 120 GiB image, `windows-arm64-runners.qcow2`, was created and
checked, then hot-added as USB storage with serial `RNRARM64DATA`. The
launcher includes it persistently. The prior launcher is preserved as
`start-qemu.before-runner-disk-20261002.sh` beside the guest disk. Windows
confirmed disk 1 is RAW, 120 GiB, and neither boot nor system; disk 0 remains
the existing Windows NVMe disk. The guarded initializer is moving the DVD
to R: and preparing NTFS D: labelled `RNR_ARM_DATA`. At 10:34 UTC it completed: D: is NTFS with 128831188992 bytes total
and 128729665536 bytes free. A write/readback test in D:\runners passed.
Log: `C:\ProgramData\nomercy\arm-runner-disk.log`.

Build health checks are prepared but not dispatched:
- GitHub private repository `NoMercy-Entertainment/nomercy-runner-health-check`,
  master, workflow `.github/workflows/windows-arm64.yml`.
- Forgejo `FiLL/forgejo-runners-heath-check`, branch
  `windows-arm64-health-20261002`, `.forgejo/workflows/windows-arm64.yml`.

Both contain the same service-account build test, including native C++,
Cargo/Rust and Go binaries, Java/.NET builds, Android APK packaging, and
other installed CLI tools. The existing project workflows were not changed.

### Build environment progress at 10:08 UTC

The WinGet source catalog finished its first registration at 09:43:52 UTC.
Git 2.55.0.5 ARM64 downloaded successfully, passed WinGet's hash check,
and launched its silent machine installer at 09:47:01 UTC. Its Inno log
shows continuing extraction through 10:08 UTC; this is still in progress,
not a completed Git installation. The main bootstrap transcript is quiet
while WinGet is running because the installer output is captured by the
shared tools script.

The ARM worker bootstrap previously selected the x64 embedded Python even
when `-Architecture arm64` was supplied. The installer now chooses the
WindowsArm Python settings and rejects a non-ARM64 PE executable. The
Python 3.13.14 ARM64 embed archive was checked against SHA-256
`8b5bfc935a24b55c17410aa0b21016ebeee225c96addf008d1d3cd83ff52eb43`,
with a valid Python Software Foundation signature and PE machine 0xaa64.
Updated settings, installer and a VERSION file have been transferred and
extracted into the guest source tree. The agent itself is not installed yet.

`infra/windows/guest/Test-RunnerBuildEnvironment.ps1` now provides real
compile/run checks intended to run under each forge's runner service
account. It is syntax checked and staged in the guest; no ARM CI job has
passed yet. Both ARM fleets currently still have zero runners. The existing
17 other runners were healthy at the last controller check.

### Update at 08:55 UTC

The original Winhance scheduled user pass completed successfully at
08:25:53 UTC, after applying the HKCU choices and marking them applied.
This was the original automatic task, not a manual replacement debloater.
The taskbar now reflects the chosen layout.

The OpenSSH Windows capability download stayed at 0 bytes / 14.6% overall
through 08:52 UTC. Ctrl+C requested normal cancellation; DISM ended at
08:53:27 with error 1223 (cancelled) and `Reboot required=no`. The redirected
batch termination prompt was answered Y, and the administrator prompt returned.

The signed upstream ARM64 MSI is now staged as H: ARMSSHMSI:
`/home/runner/arm-ssh-msi-20261002.iso`, drive armsshmsi, device armsshmsi-usb.
Source: PowerShell/Win32-OpenSSH release `10.0.0.0p2-Preview`, asset
`OpenSSH-ARM64-v10.0.0.0.msi`. SHA-256 verified against the GitHub asset digest:
`7a17d0e22d004fb47ca4bfd8fef926fa305de4ebf70a6f3c7a29c39aabef0023`.
Microsoft Authenticode validation passed on the physical host; the copied
Ubuntu MSI hash matches. `H:\run.cmd` was launched at about 08:55 UTC.
It installs only the server using `ADDLOCAL=Server`, the existing public key,
automatic service startup and the SSH firewall rule. Watch
`C:\windows-arm-ssh-msi.log` and `C:\windows-arm-ssh-msi-install.log`.
Installation and SSH authentication are not yet confirmed.

At 09:02:56 UTC the MSI completed with exit code 0. The follow-up batch
confirmed sshd was already running and configured automatic startup. The
ed25519 host public key was obtained from the guest via the trusted read-only
QEMU disk export before adding it to the physical host's known_hosts:
`AAAAC3NzaC1lZDI1NTE5AAAAIGvhUJW6NFFnYkNOnB81Qnd0IV8eAJBsXmR9MAxTjzTF`.
At 09:04:55 UTC a strict-host-key-checked SSH connection through the Ubuntu
ProxyJump completed `cmd.exe /d /c echo ARM-SSH-READY` with exit code 0.
The platform private key stayed on the physical Windows host. SSH execution
is now verified, but connection/command startup still took roughly two minutes.
Tool inventory and enrollment remain pending.

Direct SSH inventory then confirmed `ARM64`, `admin`, edition
`Professional`, PowerShell 7 on PATH and working WinGet `v1.11.510`.
`HKCU\Software\Winhance\UserCustomizationsApplied=1`. Keyboard preload
0409/0413 entries both substitute to `00020409` (US International), so no
keyboard modification was needed. SFTP archive transfer closed after
authentication; the MSI's documented machine PATH addition for
`C:\Program Files\OpenSSH` still needs service-environment renewal and a
successful transfer test.

The runner source archive and build-tool bootstrap were instead delivered
on J: ARMTOOLS (`/home/runner/arm-tool-bootstrap-20261002.iso`, armtools /
armtools-usb). I: RNR_ARM_TOOLS is the original runner payload, reattached
as armrunnerpayload / armrunnerpayload-usb. At about 09:12 UTC the existing
SSH admin shell launched PowerShell 7 with `J:\bootstrap.ps1`. It adds the
OpenSSH machine PATH, extracts the source into `C:\ProgramData\nomercy\src`,
and invokes the shared `Install-RunnerTools.ps1 -Architecture arm64`.
Logs: `C:\ProgramData\nomercy\arm-tool-bootstrap.log`; final marker:
`arm-tool-bootstrap-result.txt`. Completion and service-account build
verification remain pending; neither forge is enrolled yet.

At 09:18 UTC the bootstrap reached `installing: git (Git.Git), for the
machine`. Its transcript identifies native PowerShell 7.6.6, PID 7104,
admin, with a start time of 09:14:39 UTC. The small SFTP upload then
completed with exit code 0 after the machine PATH addition; no sshd restart
was needed for that new connection. Full tool installation and both forge
registrations remain incomplete. The bootstrap is running in the persistent
SSH command session; do not terminate that session while it installs tools.
The temporary read-only NBD server was stopped after this check.

At 06:37 UTC, after almost nine hours of uptime, the lock screen was still
showing 01:32 and did not reliably open the password field. The prior
service-timeout change has not solved the login freeze. Profiling PID
693290 found CPU activity in kernel synchronization and TCG translation;
this is diagnostic evidence, not proof of a specific QEMU bug.

A separate QEMU 11.1.2 aarch64 build completed under
`/home/runner/arm-qemu-11.1.2`, for `/opt/nomercy-qemu-11.1.2`.
The official release archive SHA-256 is
`731b5681e4bb18be313231579b8efd0296c5b015fa36dc533874b639ba838016`.
Its detached signature verified against release-manager fingerprint
`CEACC9E15534EBABB82D3FA03353C9CEF108B584`, linked from qemu.org/download.
Build dependencies installed 23 new packages, upgraded none; the system
QEMU package is not replaced. The new build has CONFIG_CMPXCHG128 enabled.

Before the test, only the ARM service was stopped, its disk passed
`qemu-img check`, and a checkpoint was created at
`checkpoint-before-qemu11-20261002` (launcher, vars, TPM, service).
The current disk path is now a new qcow2 overlay backed by
`windows-arm64-before-qemu11-20261002.qcow2`, preserving the complete
pre-test Windows installation. Both files are required. Never remove the
backing disk or copy only the overlay as if it were a complete backup.
The earlier full byte-verified 2026-10-01 backup remains available.
No answer file, Windows account, or installed Windows settings were
changed for this emulator compatibility test. Boot testing is in progress.
The emulator binary SHA-256 is
`5e463677444dcf5097b962c7556c8bc7662d5529374328f961283950eafa0549`.
The build used `--target-list=aarch64-softmmu --disable-docs --disable-tools
--disable-guest-agent --disable-werror --enable-slirp --enable-vnc
--enable-tpm --disable-gtk --disable-sdl --disable-opengl
--disable-virglrenderer`, then `ninja -j 3 qemu-system-aarch64`.
Its signed-source `pc-bios` directory is installed alongside the executable
at `/opt/nomercy-qemu-11.1.2/share/qemu`; the launcher must use that `-L`
path. The first launch lacked efi-virtio.rom, exited before boot and was
stopped; this packaging issue was corrected before the actual boot test.
The active process started at 06:52:46 UTC (PID 1006668), with eight CPUs,
`virt-8.2` to retain the previous machine version, and the existing AAVMF
firmware and TPM state. Windows Boot Manager was observed loading from
the installed NVMe disk. At 06:56 UTC the lock screen appeared. Enter
opened the admin password field, confirmed by 06:57 UTC; after its idle
timeout the same transition succeeded again at 07:00 UTC. The screenshot
arm-qemu11-login-responsive-20261002.png records the second check.
No credentials were entered by the agent. The user has been asked to log
in; desktop performance and sustained stability are not yet verified.
The 07:00 UTC event collection (`arm-login-20261002-070018.tar`) still
contains StateRepository.User DCOM 10010 and TPM 15 errors. Neither is
claimed fixed by the QEMU upgrade. The hardware-clock correction now
matches local time. The read-only diagnostic NBD export was stopped.

Native preparation media is now `/home/runner/arm-native-prep-20261002-v3.iso`
on the `armprep` CD. Its `run.cmd` calls the existing native DISM/sc/netsh
SSH preparation. A small native ARM64 `CheckTokenMembership` helper
replaces the pre-log `fltmc` elevation check, so it no longer depends on a
filter-manager query merely to check administrator rights. It does not
modify Windows. The public key still
matches the original platform key. Do not run older K:\run.cmd by assuming
that drive letter persisted; locate the ARMNATIVE3 volume after login.

The user logged in at 07:40 UTC on 2026-10-02. The desktop and taskbar
appeared, and an elevated command prompt opened at approximately 07:49 UTC.
Application startup remains very slow; sustained stability is not yet proven.
`vol f:` verified ARMNATIVE3, then `F:\run.cmd` was started once at 07:50 UTC.
The native administrator check passed, the installed timeout read back as
600000, and the outgoing HTTPS test succeeded. DISM began adding
`OpenSSH.Server~~~~0.0.1.0` at 07:51 UTC. At 07:58 UTC CBS was still advancing
through package applicability checks. A quiet console or unchanged DISM
header alone does not establish a hang. Monitor
`C:\windows-arm-native-preparation.log` and `Windows\Logs\CBS\CBS.log`;
SSH installation, key authentication, build tools and enrollment are not yet
verified. The private, read-only NBD export was temporarily reopened for
these checks, with the reader disconnected between collections.
At 08:05 UTC the preparation still showed 5.9%; CBS had entered the
OpenSSH ARM64 Windows Update download/search stage at 08:00 UTC. Its latest
records at 08:03 UTC also show a separate language-feature servicing query
queued. No completed SSH installation or failure result was available.
Windows Update's collected events did not establish a new download error;
System still contained recurring TPM event 15. The NBD server was stopped
after inspection, leaving the in-guest preparation running undisturbed.

At 08:12 UTC OpenSSH reached 14.6%; Windows Update reported download
progress 0/100 through 08:17 UTC. A read-only native process/service
diagnostic from the verified G: ARMDIAG volume found approximately 6.36 GB
guest RAM available and CPU activity in Windows Update (PID 4824), Delivery
Optimization (2808), and PowerShell (5808). The original Winhance log now
records a successful interactive-user launch at 08:15:16 UTC with PID 5808,
following the elevated-token selection at 08:15:04. This is progress past
the previous Start-ProcessAsUser failure; completion of the HKCU pass is
not yet verified. No replacement debloat or manual user-pass rerun was
started. Diagnostic media is `/home/runner/arm-progress-diagnostic-20261002.iso`
on drive armdiag / device armdiag-usb. Its batch completed and its separate
console was closed; the original F: preparation remains active.
At 08:19 UTC the OpenSSH download still reported 0/100 (14.6% overall).
The 08:18:57 UTC event collection contains Windows Update event 31 for
a separate .NET update KB5126052, error 0x800F8011 at 08:07:01 UTC. This
is not an OpenSSH installation result and its relationship to the current
download is unproven. The temporary NBD server was stopped after inspection.

## Latest verified state (2026-10-01, 21:58 UTC)

Update on 2026-10-01: the desktop survived overnight, but this is not yet a
healthy runner. StartMenuExperienceHost logged a hang at 19:05:52 UTC. The
original Winhance user-customization task found `admin`, then failed in
`Start-ProcessAsUser` with a null-valued-expression error; its log says it
will retry next logon. Its user-customization pass must not be described as
complete. No answer file or embedded script was edited to hide that failure.

The checked original tool payload is now F: (`ooberepair` drive), recovery
media G: is `arm-oobe-page-timeout-20261001-v11.iso`, and new native tools
media is attached as `nativetools` / `nativetools-usb` (expected H:):
`/home/runner/arm-native-tools-20261001-v2.iso`. The first native-tools ISO
was incomplete because Ubuntu had no `unzip`; Python zipfile extraction
fixed v2 before any bootstrap was executed. Windows PowerShell preparation
from `G:\prepare.cmd` had no transcript visible to the external reader;
its explicitly identified console was closed before trying PowerShell 7.

PowerShell 7.6.6 ARM64 ZIP SHA-256:
`bbde9dda31d148415eccb5fbe1638e6400a144187b006e5b3fd8ec2f39d781be`.
Microsoft Authenticode signature on pwsh.exe was valid on the physical host;
its executable SHA-256 matches the staged Ubuntu copy:
`abb215e4d87889c85a18333a310b1dbdae33e78a25e13dc7cf129fbdaea848e0`.
`bootstrap.cmd` copies it to Program Files, then uses it to execute the
unchanged F:\Prepare-WindowsArm.ps1 (network, SSH and RDP only). Execution
and remote access are not yet fully verified. Robocopy completed at 20:32:05
UTC: 657 files, 269.38 MiB, zero failed files. PowerShell 7 reached the original
preparation script at 20:46:26 UTC (PID 9616, `admin`, ARM64); it is locating
the network driver. PnPUtil confirmed the existing ARM64 NetKVM driver
`oem0.inf` is current. The next management-interface query did not complete;
the preparation pipeline was stopped at 21:16:32 UTC and its batch terminated.
Its SSH, firewall and RDP steps did not complete. The very slow program
startup remains unresolved.
The payload public key matches
`D:\HyperV\runner-platform\ssh\id_ed25519.pub` byte-for-byte.

For live diagnostics, use the running QEMU's read-only NBD export rather
than opening its qcow2 independently with `--force-share`: cached block
metadata caused inconsistent visibility of newly created guest files in
the latter reader. The temporary export is `system` on the private Unix
socket `/var/lib/runner-appliances/windows-arm64/inspect.sock`; no TCP port
or write-enabled export was created. The filesystem is mounted read-only
with `norecover`, and unmounted/disconnected after every collection. This
is still a live filesystem view, not a transactional snapshot. Stop the
temporary NBD server when diagnostics finish.

Windows also delays flushing new directory metadata: the live read-only
reader temporarily reported missing files and an index I/O error for
`ProgramData/NoMercy/ArmSetup`, then read the files successfully again.
Do not interpret a missing live-view file as proof that a command never ran,
or that the installed filesystem is corrupt. Verify guest-native results.

The native diagnostic samples found 142 processes, 21 WerFault processes,
and about 48% memory load with 4.3 GB available. Service enumeration maps
PID 66584 to WerSvc and reports WMI/Appinfo/RPC as running; a running service
state alone does not prove its requests complete. The recurring DCOM AppID
`{6A695947-B2C3-457C-9B12-800EE815E4BF}` belongs to the out-of-process context
menu host (`OOPContextMenuHost.dll`), not a network-driver registration.

System events 7000/7009 repeatedly record a 30,000 ms startup timeout for
AppXSVC, ClipSVC, WaaSMedicSvc and Software Protection. The upstream answer
file explicitly sets `ServicesPipeTimeout=30000`. A narrowly scoped native
repair verified that original value, wrote a restoration `.reg` file, and
read back `ServicesPipeTimeout=600000`. Evidence is in the guest's
`C:\ProgramData\NoMercy\ArmSetup\service-timeout-repair.txt` and
`service-timeout-before.reg`. This installed-OS change requires a reboot;
the source answer file was not edited. Reboot validation is still pending.

At 21:18 UTC the existing elevated command prompt started `K:\run.cmd` from
`/home/runner/arm-native-prep-20261001-v2.iso` (`armprep` drive). This checks
elevation, confirms/applies the same timeout repair idempotently, then runs
the native Windows DISM/sc/netsh SSH preparation instead of the stalled
PowerShell management query. Its public key hash matches the original payload.
Watch `C:\windows-arm-native-preparation.log`; successful SSH connectivity
has not yet been verified. Temporary diagnostics media also occupy I: and J:.

At approximately 21:25 UTC a normal QEMU ACPI power-button request was sent
to prepare for a clean backup/restart. It does not initiate shutdown with
this guest's power policy: the active scheme
`4fa0a3d5-072c-46d5-8f81-f8d4e48be9e6` has physical power-button AC/DC action
0 (do nothing), matching the upstream script. This was verified in the
guest SYSTEM hive. Use Windows' own Shut down command or secure-screen
power menu; an ignored ACPI request is not proof of a frozen guest.
The secure-screen menu opened following Ctrl+Alt+Delete. Shutdown and the
new desktop backup have not yet completed. A guarded offline backup helper
is staged as `/tmp/arm-backup-desktop-20261001.sh`; it refuses to copy while
the ARM service, QEMU, TPM or disk reader is active.

At 21:38 UTC Windows shutdown attempts had returned to the lock screen
without powering off. Using the previously authorized forced-stop recovery,
only `windows-arm64-runner.service` was stopped (inactive, MainPID 0).
`qemu-img check` found no qcow2 errors. A full offline disk/UEFI/TPM/launcher
backup was byte-compared successfully at
`backup-desktop-service-timeout-20261001-213839` (helper exit 0).
The same guest was restarted at 21:48:12 UTC with eight vCPUs, PID 693290.
Only the native SSH preparation media was reattached as armprep; old
diagnostic CDs were not reattached. Boot/login validation is pending.

Offline SYSTEM alone still contained 30000 because its transactions had
not yet been merged. Replaying copied SYSTEM.LOG2 (1768-1791) followed by
SYSTEM.LOG1 (1792-1824) into a separate local analysis copy confirms
`ControlSet001\Control\ServicesPipeTimeout=600000`. The guest hive and logs
were read only and were not replaced by this analysis copy. Windows will
perform its own normal registry recovery on boot. There was no native SSH
preparation log in the offline filesystem; SSH remains unverified.
By 21:54 UTC the reboot reached the Windows lock screen, without returning
to Setup or OOBE. The live SYSTEM hive now directly reports 600000, without
requiring transaction-log replay. GPClient startup completed; no desktop
login has occurred during this boot. The user was asked to sign in as admin
via noVNC. SSH has no registered service yet; build tools and runner
registration remain pending.

The reboot exposed a clock configuration mismatch: the Ubuntu host is UTC,
while Windows expects its hardware clock in W. Europe local time. Only
this guest's launcher now sets `TZ=Europe/Amsterdam` and
`-rtc base=localtime,clock=host`, following the
[QEMU RTC documentation](https://www.qemu.org/docs/master/system/qemu-manpage.html).
The deployed launcher passed `bash -n`; a separate pre-change copy is
`start-qemu.before-rtc-20261001.sh`. This launch correction takes effect
at the next service start; the currently running guest has not been
restarted a second time. Current-boot Windows timestamps are two hours
behind UTC until guest time synchronization is corrected.

Remaining errors after boot include DCOM 10010 for
Windows.Internal.StateRepository.User and TPM 15 with status 0xc0000034.
The TPM error also exists in the pre-reboot log; it was not introduced by
this restart. The swtpm process remains running and no TPM state was reset.
Their causes still require guest-side diagnosis. Evidence is in
`arm-login-20261001-215607.tar`. Reaching the lock screen does not establish
that the runner is healthy or that desktop responsiveness is fixed.
Performance investigation: both installed Windows PowerShell and FrameworkArm64
CLR are native ARM64. The running QEMU's `cpu_atomic_cmpxchgo_le_mmu` and
`helper_atomic_cmpxchgo_le` contain `lock cmpxchg16b`; the known missing
128-bit atomic build regression is therefore not an established explanation.

The current installation has passed OOBE and reached the `admin` desktop.
The taskbar is visible and Win+R launched a normal command prompt. Native
checks report `Architecture=ARM64`, `CPUs=8`, `EditionID=Professional`,
`OOBEInProgress=0`, and `SystemSetupInProgress=0` on build 26200.8037.
Evidence: `arm-first-desktop-20260930.png`,
`C:\Users\admin\AppData\Local\Temp\arm-startup-check.txt`, and
`arm-login-20260930-212932.tar` (collector output includes the native check).
The host-side native output is saved as `arm-startup-check-20260930.txt`.
The optional final `tasklist` command was cancelled after the installation
and architecture checks had completed; do not describe that process-list
check as passing. The diagnostic command prompt and Task Manager were
closed after verification. Windows' own first-logon tasks were left running.
The first logon remains very slow under TCG; a subsequent reboot has not
yet been verified. GitHub/Forgejo agent and toolchain provisioning remain
pending. The temporary OOBE recovery modules, navigation overlay and timeout
registry values described below are still installed; preserve their backups
and remove only these additions after the first-logon work has settled.

The Windows ARM64 guest uses `qemu-system-aarch64` on the existing Ubuntu
`macos-runner` Hyper-V VM. It has its own qcow2 disk, UEFI variables and TPM
state. The macOS QEMU guest and its registration are independent. The physical
host is x86_64, so QEMU uses multithreaded TCG software emulation, not KVM.

## GitHub answer-file reinstall (2026-09-29)

The new installer uses the original Windows ARM64 ISO and the answer file from
[UnattendedWinstall](https://github.com/memstechtips/UnattendedWinstall/blob/0db35390de4ec4745bad2aeb769e169f618330fe/autounattend.xml),
pinned to commit `0db35390de4ec4745bad2aeb769e169f618330fe`.
Its source SHA-256 is
`1906a0bb362b8f6b33a181a6b3405afbc2dea30cf65671d80ca4a06e6565b25d`.
For this build, `filter-autounattend.py` removed the six x86/amd64 component
blocks; the resulting XML is therefore not byte-identical to upstream.
The filtered SHA-256 is
`6664bd2c33c6ed8211c18a44d2243f64b92e93d154fc20f66a7724ca99c9b051`.
ARM64 settings and embedded scripts remain unchanged. Manual Setup choices
remain available; no edition, account, password or partition choices are added.

The launcher now assigns **8 virtual CPUs and 8 GiB RAM**. This is software
emulation on an x86_64 Ubuntu host that also runs the macOS guest. Eight guest
CPUs do not mean eight dedicated physical cores.

New media: `Windows11Arm64-github-0db35390-arm64-20260929.iso`.
The media verification compares the mounted XML byte-for-byte, inventories all
original files and sizes, and byte-compares `install.wim`, `boot.wim`, the ARM64
EFI loader and EFI boot image. Evidence is stored in
`github-install-media-20260929.json` on the QEMU host.
The preceding installation and its UEFI/TPM state are retained under
`backup-before-github-20260929/`; the earlier full verified backup remains at
`backup-logon-crash-20260928-111407/`.

**Installation, debloat results and GitHub/Forgejo registration still require
verification.** The previous attempt ran Winhance during specialize, but .NET
and capability operations failed and Windows stalled during first logon with
Explorer, ShellHost and Ngen crashes. The exact cause is not established.
The upstream XML still includes the same .NET 3.5 command. Neither a successful
ISO build nor Winhance's generic SUCCESS messages prove a healthy installation.
Check actual child logs, Windows edition/architecture, repeated boot/login,
guest tooling and real jobs on both forges before declaring this runner ready.

### OOBE failure observed on 2026-09-29

Winhance finished its system script, and Setup reported `IMAGE_STATE_COMPLETE`,
but the interactive first-run configuration subsequently displayed
"Why did my PC restart?" twice. Event 1074 records two `taskhostw.exe`-initiated
reconfiguration restarts. The collected logs contain no bugcheck event or
kernel crash dump; this should not be described as a confirmed kernel BSOD.

Application events record crashes in `Ngen.exe` (`c0000409`),
`LinqWebConfig.exe` and `WFServicesReg.exe` (`c0000005`), and `AppReadiness`.
Panther records `.NET 3.5` installer failures, `0x800f0922`, and a servicing
rollback. The causal link between these failures and the OOBE restart screen
is not yet established. Do not clear completion flags or force another reboot
to hide this state.

The console also confirmed the VirtIO Ethernet Controller
(`PCI\VEN_1AF4&DEV_1000`) had problem code 28 (missing driver).
The attached driver media provides `E:\NetKVM\w11\ARM64\netkvm.inf`.
Installed this ARM64 driver through the guest recovery console using
`pnputil /add-driver e:\netkvm\w11\arm64\netkvm.inf /install`.
PnPUtil confirmed publication as `oem0.inf` and installation on the Ethernet
device. `ipconfig` subsequently showed `10.0.2.15` and gateway `10.0.2.2`.
A guest HTTPS request to `www.microsoft.com` received HTTP 200 headers; curl
later reached its 30-second time limit, so this is evidence of connectivity,
not a clean end-to-end transfer test. Successful OOBE completion remains unverified.
Diagnostic logs and event records were saved on the Windows host under
`D:\HyperV\runner-platform\stage\arm-oobe-failure-20260929\`.

On 2026-09-30 the recovery screen recurred with the network driver present.
OOBE itself logged `Internet detected`; QEMU showed established outbound HTTPS
connections. Its latest ZDP update search timed out after 240 seconds rather
than the earlier COM startup failure. `sc start wuauserv` followed by
`sc query wuauserv` confirmed the update service could reach `RUNNING`.
`DISM /Online /Cleanup-Image /CheckHealth` reported no component-store
corruption; this is a quick status check, not a full integrity scan.

Application event 4065 records another AppReadiness crash (`e06d7363`), and
OOBE's QueueSystemTasks call returned `0x800706BE`. AppX deployment events
also reject `Microsoft.VCLibs.140.00_14.0.33519.0_arm__8wekyb3d8bbwe` with
`0x80073D10` because it targets ARM32 rather than ARM64. These are diagnostic
findings, not proof that removing that package would fix OOBE. No package was
removed, no answer-file setting was changed, and no reboot was forced during
this check. Starting AppReadiness from the recovery console also reached
`RUNNING` with exit code zero; neither service start proves that the next OOBE
attempt will succeed. The diagnostic console was then closed.
Current evidence is preserved in the host stage directories
`arm-oobe-recurrence-20260930` and `arm-app-events-20260930`.

### First-sign-in watchdog (2026-09-30)

The retry after starting services returned to the recovery screen. The new
log identifies the immediate restart trigger: OOBE Monitor received event 101
at guest-local 20:33:12, called `UserOOBEController::Exit()` at 20:35:12,
resealed to OOBE, and restarted through `taskhostw.exe`. No new Application
1000 crash was recorded in this attempt. This distinguishes the current
watchdog restart from the earlier AppReadiness and .NET failures.

Read-only analysis of the installed `UserOOBE.dll` (SHA-256
`46095dc75b7e8c07e95380220956798c279eea639556e43bb982cc333b53793a`)
identified the built-in timeout table at RVA `0xb9540`: event 101 is
`EventTimeoutUserSignIn`, default 120000 milliseconds. Code at RVA `0x32fb4`
reads DWORD overrides with `RegGetValueW` from
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE`, multiplies the value
by 1000, and accepts values at least as large as the default. This override
therefore uses **seconds**, not milliseconds. It is present in this binary;
it is not claimed to be a documented Microsoft deployment setting.

The value was absent before the diagnostic change (console capture
`D:\HyperV\runner-platform\stage\arm-timeout-before.png`). The targeted trial
sets only `EventTimeoutUserSignIn` to DWORD 1800, allowing 30 minutes for
initial sign-in under TCG. The live command succeeded and a subsequent query
confirmed `REG_DWORD 0x708` (1800); capture `arm-timeout-verified.png` is in
the host stage directory. It does not mark OOBE complete, disable its monitor,
create an account, alter the answer file, or bypass the user's setup choices.
Remove this single value after successful setup to restore the original state.
The diagnosis archive is `arm-oobe-loop-0800-20260930` in the host stage
directory. Longer timeout and successful OOBE still require a live retry;
the reason sign-in exceeded two minutes is not yet proven.

Live retry was started on 2026-09-30 at 08:12:18 UTC. Event 101 arrived at
08:15:12 UTC; the former two-minute restart did not occur. Winlogon records
GPClient's logon callback from 08:15:17 to 08:18:27 (190 seconds), and Group
Policy event 8001 confirms completion after 189 seconds. The local session
then logged on, and Shell-Core completed `LaunchUserOobePrep` and began
`ShellInitTasks` at 08:20:09 UTC. These records demonstrate progress past the
old deadline; they do not yet establish completed interactive OOBE. Evidence:
host stage folders `arm-login-0819-events` and `arm-signin-0821-events`.

The same retry progressed to event 102 at 08:19:54 UTC and event 103 at
08:24:31 UTC. Shell-Core completed `ShellInitTasks` and began `LaunchUserOobe`.
It then hit the separate event-103 deadline: `UserOOBEController::Exit()` began
at 08:27:56, resealing was logged at 08:30:26, and `OOBE Monitor timer expired
for event: 103` at 08:33:33. The binary's separate `EventTimeoutShellReady`
default is 180000 ms (three minutes). Increasing only the event-101 limit was
insufficient. The GUI had not reached user choices. An ACPI power-down request
was sent at 08:32:31 UTC while the failed phase was exiting. Windows did not
shut down. Using the user's earlier authorization for a forced stop with a
backup, only the ARM service was stopped at approximately 08:37 UTC. Evidence:
`arm-login-20260930-083406` archive in the host stage directory.

Before restarting, the current disk, UEFI variables, TPM state, launcher and
service definition were copied to `backup-oobe-watchdog-20260930/` under the
ARM appliance directory. `cmp` verified the complete disk and UEFI copies.
The same installation restarted at 08:48:20 UTC; VNC capture and the host's
noVNC HTTP endpoint responded successfully. Windows returned to the recovery
screen. In its administrator console, `EventTimeoutShellReady` was confirmed
absent and then set to DWORD 1800. A subsequent filtered registry query verified
both `EventTimeoutUserSignIn` and `EventTimeoutShellReady` as `0x708`. Evidence:
`arm-shell-timeout-before.png` and `arm-shell-timeout-verified.png` in the host
stage directory. Neither override marks OOBE complete; actual interactive
setup still needs to be verified. Remove both trial values after successful
setup to restore the original watchdog limits.
The diagnostic consoles were closed and Next was clicked at 09:04:03 UTC
for a controlled retry with both overrides present. The guest still has eight
vCPUs; the host service reports no CPU quota, all eight host vCPUs allowed,
and no memory cap. This is a retry in progress, not a completed repair.
This retry reached event 101 at 09:05:13 UTC, completed temporary-user logon
at 09:06:06, reached event 102 at 09:07:17, and reached event 103 at 09:09:57.
Shell-Core completed `ShellInitTasks` and began `LaunchUserOobe` at 09:09:56.
No new Application 1000/1002 event was present in the 09:10:51 snapshot.
Interactive OOBE is not yet confirmed. The previous attempt also contains a
Shell-Core JavaScript error for `sample/CloudExperienceHostAPI.Speech.SpeechRecognition`
at 08:30:31, after the watchdog had already started exiting at 08:27:56;
its role in the failure is therefore not established.

The second override prevented the three-minute watchdog exit, but did not
complete OOBE. At 09:15:22 UTC, Shell-Core recorded a new unhandled RequireJS
`scripterror` for `sample/CloudExperienceHostAPI.Speech.SpeechRecognitionController`.
Application event 4153 is a `WWAJSE` report for `lib/require.js`; TWinUI event
5961 records activation failure `0x80040904` at 09:16:51. The broker then
exited with `[0]`, resealed to OOBE, and Windows restarted itself. This is a
separate application failure, not the previous watchdog timeout.

Every original file in the installed CloudExperienceHost package matched the
corresponding file extracted from Pro index 3 of the original ISO; the only
extra installed directory was `microsoft.system.package.metadata`. The two
reported `samples/` modules are absent in that ISO too. The original
`lib/optional.js` fallback defines a missing module as an empty string and
retries its import, but these imports still generated unhandled errors here.

A reversible trial supplies two AMD modules returning that same empty string:
`samples/CloudExperienceHostAPI.Speech.SpeechRecognition.js` and
`samples/CloudExperienceHostAPI.Speech.SpeechRecognitionController.js` inside
the installed CloudExperienceHost directory. It does not replace native speech
APIs or modify the original Windows JavaScript, answer file, or setup choices.
Both added files have SHA-256
`b1e2526137ac7bc7a2fc233e03979e720999c201e248244f2df824ac85c11db8`.
The original `optional.js` SHA-256 remains
`be20f2034e02a0d58a1eca7c7cb63b8aec085373f34dd4937b8da33d13b4ff22`;
it has two NTFS hard links and must not be patched in place.
The trial script is on a temporary, hot-attached read-only ISO
`/home/runner/arm-oobe-sample-fallback-20260930.iso`, guest drive F:.
The prepared PowerShell script verifies hashes before and after copying and logs to
`C:\ProgramData\NoMercy\Recovery\oobe-sample-fallback-20260930\repair.log`.
PowerShell startup took several minutes; its invocation was interrupted before
copying, confirmed by the transcript's stopped pipeline. The equivalent native
`robocopy F:\samples <CloudExperienceHost>\samples /B /COPY:DAT /R:0 /W:0`
completed at 09:37:59 UTC: two files copied, zero failures. A subsequent
read-only inspection of the guest disk verified both added files against the
hash above and confirmed the original loader hash is unchanged. No existing
Windows file was overwritten. A successful OOBE retry remains pending.
The consoles were closed and Next was clicked at 09:39:39 UTC for the first
retry with the two fallback modules present.
This retry completed Group Policy user logon at 09:47:17 UTC after 318
seconds, completed ShellInitTasks at 09:53:13, and reached the configuration
app's intro initialization at 09:57:13. The screen still showed the loading
indicator. A separate ClipSVC startup timeout (30,000 ms, System events
7009/7000) occurred at 09:50:50; its causal role is not established and no
service-timeout registry change has been made. These intermediate milestones
do not establish that the fallback resolves OOBE.
At 09:59:52, `ClearTemporaryWebDataAsyncSucceeded` was followed by the FRX
experience start. `StartSelector` completed successfully at 10:00:30, and
`AutoPilotPrefetch` started at 10:00:33 with Internet connectivity reported.
This retry has therefore passed the earlier fatal optional-module import;
interactive setup and completion are still pending. The most recent WWAJSE
report in the 10:01:23 collection remains the previous 09:15 failure.
Rollback consists of removing only the two added files after matching their
hashes; retain all pre-existing package files and setup state.

### OOBE page visibility timeout (2026-09-30)

The next failure was distinct: `OobeRegion` navigated at 10:05:23 UTC and
reported `VisibilityTimeout` exactly 60 seconds later. The error page also
hit its own 15-second visibility timeout before finally becoming visible.
The log records a `SkippingWebapp` input at 10:12:36 and then `OobeKeyboard`;
the agent did not click Skip. A question about this console interaction was
sent to the user. No account, region, keyboard, or privacy choice was entered
by the agent.

The stock `js/appManager.js` reads the current node's `timeout` property.
`js/discovery.js` loads every file in `data/prod` whose display name contains
`navigation`, merging experience definitions in file enumeration order.
A temporary **additional** file `data/prod/zz-navigation-tcg-recovery.json`
contains an exact copy of `FRXINCLUSIVE` with the timeout of its 71 URL nodes
raised to at least 600,000 ms. All other fields, links, choices and original
files are unchanged; a structural comparison verifies this. The additional
file must be read after `navigation.json`; effective loading still needs to
be confirmed in the next retry's telemetry.

The payload was built with the local stage helper
`build-arm-oobe-timeout-overlay.py` and copied in the running Windows guest
with `G:\install.cmd` at 10:21:41 UTC. Robocopy reported one new file, zero
failures, and `fc /b` reported no differences. A separate read-only disk
inspection verified:

* Original `navigation.json`: SHA-256
  `e8f42941b82b5ce6b4d24dfbd758697da2f5ac6b986b40197b945e57ea58a8e7`.
* Added file: SHA-256
  `bbf618437dd37c59a105019ec299b242ea3269ccb49916ecf8c6e2dc9d5b3117`.

The read-only temporary ISO is
`/home/runner/arm-oobe-page-timeout-20260930-v2.iso`, HMP drive `oobetimeout`,
device `oobetimeout-usb`; it is not in the persistent launcher. The earlier
sample-module repair ISO is still attached as drive `ooberepair` (F:).
The stock Try again handler restarts the configuration app and reloads its
navigation data. A successful page retry remains unverified. Remove only
the added timeout file after OOBE is completed to restore stock page limits;
it duplicates this specific Windows build's flow and must not override a
future Windows update's navigation definitions.

The agent clicked **Try again** on OOBEKEYBOARD at 10:23:42 UTC. Navigation
completed at 10:24:39 and the previous 60-second visibility timeout did not
recur. However, at 10:28:19 a different `WebViewUnhandledException` reported
a RequireJS load timeout for
`lib/text!inclusiveOobeViewTemplates/oobe-toggle-template.html`. That 616-byte
template exists in the original package. The stock `RequirePathConfig`
sets a separate `waitSeconds = 30`; extending page visibility alone is
therefore insufficient.

A third optional module was prepared as
`samples/Sample.CloudExperienceHostAPI.Speech.SpeechSynthesis.js`. The stock
`core/js/knockouthelpers.js` requires this otherwise absent optional sample
before registering page components; its third module result is unused.
The added module sets `requirejs.config({waitSeconds: 600})` and returns the
same empty-string value as the stock optional-module fallback. This gives
component template imports more time without editing the original loader,
HTML, native speech APIs, or answer file. SHA-256:
`89a48ac04b7a8cfa0bf963ef9fc39377b9c5ed7c0c8cec3fd64e7e5f2c8556e6`.
It is on the updated temporary
`arm-oobe-page-timeout-20260930-v3.iso`, guest G:, with `loader.cmd`. Copying
completed at 10:36:37 UTC (one new file, zero failures); both guest `fc /b`
and a separate read-only disk hash inspection verified the installed file.
The subsequent **Try again** was clicked at 10:37:53 UTC. The keyboard page
navigated at 10:38:50, reported `Visible | true` at 10:42:43, and the actual
keyboard-layout selection page was confirmed in VNC at 10:43:43. Evidence:
`arm-login-20260930-104343.tar` and the host screenshot
`D:\HyperV\runner-platform\stage\arm-oobe-keyboard-visible-20260930.png`.
Neither of the previous page/module timeout errors occurred before this
successful render. The agent left the layout selection and Yes button to
the user. **This verifies the keyboard page, not completion of OOBE or runner
registration.** No reboot or answer-file change was used for these page
corrections. Both temporary recovery ISOs remain attached; their devices
can be removed after setup when the command consoles are closed.
Remove this additional module together with the page-timeout overlay after
successful OOBE, and restore the two temporary OOBE watchdog registry limits
as described above. Keep the preserved disk/firmware/TPM backup.

### Keyboard commit watchdog (2026-09-30)

After the user submitted the keyboard choice, Shell-Core recorded
`showProgressWhenPageIsBusy` at 10:44:14 UTC and
`ShowProgressWhenPageIsBusyTimeout | OobeKeyboard` at 10:45:14, followed by
`Done | error`. Evidence: `arm-login-20260930-104852.tar`. This is a separate
60-second limit in stock `appManager.js`, reached while
`OobeKeyboardManagerStaticsCore.commitKeyboardsAsync` was pending; the page
visibility and RequireJS corrections do not affect it.

The additional sample module was extended to depend on `legacy/bridge` and
wrap only `CloudExperienceHost.showProgressWhenPageIsBusy`, passing a minimum
600,000 ms caller timeout. Explicit longer limits, all other events, return
values and the receiver are preserved. No completion result is synthesized,
no user choice is supplied, and the original Windows files remain unchanged.
Local execution checks covered those forwarding rules. Updated module SHA-256:
`e0b82dda63c269f5622b0e2ec00fbfe0209fcd7a72706c4d2d2a2ca521ada6bc`.
Temporary G: media is now `arm-oobe-page-timeout-20260930-v4.iso`.
`busy.cmd` verified the previous added module against its preserved copy before
replacing it. Guest copying and byte comparison succeeded at 10:52:06 UTC;
a separate read-only disk hash check also matched. **Try again** was clicked
at 10:53:14. The keyboard page navigated at 10:54:15 (Shell-Core record 474
in `arm-login-20260930-105541.tar`). The same cleanup requirement applies
after OOBE succeeds. At approximately 10:59 UTC, VNC again showed the actual
keyboard choice with US selected; the agent left Yes to the user. Screenshot:
`D:\HyperV\runner-platform\stage\arm-oobe-keyboard-after-busy-repair-20260930.png`.
Full keyboard commit and remaining OOBE steps are not yet verified.

The next user attempt at 18:20 UTC still failed. Shell-Core record 489
confirms `SetCustomShowProgressTimeout | 600000`, but record 490 reports
`KeyboardCommitAsyncWorkerError` with HRESULT `0x800705b4` at 18:22:25,
approximately 126 seconds after submission. Evidence:
`arm-login-20260930-182606.tar`. Thus the extended UI timer is active; the
underlying native keyboard operation times out independently. Do not describe
the keyboard step as fixed or extend the UI timer again to address this.
Read-only diagnostics are under guest
`C:\ProgramData\NoMercy\Recovery\keyboard-20260930` and in timestamped
`arm-keyboard-20260930-*.tar` archives on the hosts. The existing keyboard
registry values still include substitutions to `00020409`; that alone does
not prove the latest submitted choice was committed. The broker process and
Task Scheduler are present; investigation of their responsiveness is ongoing.

The broker task query showed `CreateObjectTask` running as SYSTEM; its
`CloudExperienceHostBroker.exe` process was PID 6320, with only three seconds
of CPU time at observation. At 18:42:45 UTC, a targeted `schtasks /end` and
`schtasks /run` of `\Microsoft\Windows\CloudExperienceHost\CreateObjectTask`
was performed. Both commands reported success; the task's new last-run time
was 18:42:56. Evidence: `broker-restart.txt` in
`arm-keyboard-20260930-184328.tar`. This confirms task restart, not successful
keyboard commit. The optional PowerShell/CIM diagnostic did not produce its
output and was cancelled; the native task/registry/service diagnostics did
complete. No Windows reinstall, VM reboot, or answer-file edit was performed.
G: is currently the diagnostic media `arm-oobe-page-timeout-20260930-v8.iso`.
The agent closed its diagnostics consoles and clicked **Try again** at
18:45:48 UTC. Shell-Core subsequently recorded keyboard navigation success
at 18:47:01 (record 521, `arm-login-20260930-184828.tar`). The user's exact
keyboard selection was requested before replaying the commit; that reply
is pending. No keyboard choice has been changed by this recovery attempt.
At approximately 18:52 UTC the actual keyboard selection page became visible
again, with US selected. Screenshot:
`D:\HyperV\runner-platform\stage\arm-keyboard-after-broker-restart-20260930.png`.
The agent did not press Yes; verification of commit remains pending the
user's keyboard choice. Returning to this page is not proof of a repaired
native commit operation.

### US-International keyboard commit succeeded (2026-09-30)

The user explicitly selected **United States - International**. The agent
selected the visible `United States-International` entry, preserved screenshot
`arm-keyboard-us-international-selected-20260930.png`, and clicked Yes at
19:43:25 UTC. Panther subsequently recorded primary keyboard
`0409:00020409` at 19:44:49 and locale keyboard `0413:00020409` at 19:45:57
(Panther's displayed local times are seven hours behind UTC).
Shell-Core record 528 reports **`Done | success` at 19:46:30 UTC**, followed
by `OobeWireless` at 19:46:53, network success, and `OobeNetworkLogging`.
Evidence: `arm-login-20260930-194715.tar`. This verifies that the selected
keyboard step completed through Windows' native commit operation, with no
Skip button or synthesized success. Full OOBE and runner provisioning still
remain to be completed. The task restart plus existing temporary timeouts
were in place for this successful attempt; this does not isolate which
change was necessary. The answer file remains unchanged.

### Background OOBE launcher timeout (2026-09-30)

After local-account setup succeeded at 20:20:28 UTC, Windows reached
`OobeSettingsSelector` at 20:27:07. Its 15-second visibility watchdog fired
at 20:27:23. At 20:28:13, `LauncherLateNavigationIgnored` confirmed that
the selector eventually returned after the error page was already active.
Evidence: `arm-login-20260930-204558.tar`, records 687, 707, 710, 720.
The original recovery overlay covered URL pages only and therefore missed
this launcher. Its generator now covers URL pages plus launchers whose
`visibility` is false, preserving all non-timeout fields and original longer
limits. The prepared overlay adjusts 99 nodes (previously 71), SHA-256:
`b71847a83e7365709c708dd13da18442359a5205266ab25c3d8a34c1b1eab5ba`.
The previous overlay is retained in the payload's `previous` directory.
Temporary G: media is `arm-oobe-page-timeout-20260930-v9.iso`; `launchers.cmd`
checks the previous overlay before replacement and verifies the result.
Guest copying and byte comparison succeeded at 20:49:20 UTC. A separate
read-only disk check verified the new overlay hash, all 99 timeout values,
unchanged original navigation.json, and equality of every other field.
No answer-file change or new installation was started. On retry at 20:50:26
UTC, the selector returned normally at 20:51:46 (action1), followed by
successful settings, OEM registration and telemetry steps. OobeNDUP loaded
from Microsoft's service at 20:56:14. Evidence:
`arm-login-20260930-205648.tar`, records 735-814. Live completion of OOBE
and arrival at Windows sign-in or the desktop remain to be verified.

At 21:01:11 UTC, `OobeExit` completed with `Done | success`. Panther then
recorded `UserOOBEController::Exit()` and successful first-user autologon
configuration at 21:01:15-18. The console changed to "Just a moment".
The update step itself did not succeed: CloudNDUP returned cancel, and
Panther logged `ExpeditedUpdateUSOTask` timeout `0x80070102`. Windows followed
its existing cancel transition and completed OOBE; no success was injected.
Evidence: `arm-login-20260930-210155.tar`, records 867-880. The desktop and
subsequent boot still need verification.

The `admin` profile loaded at 21:03:11 UTC. First logon waited about five
minutes for GPClient, then LocalSessionManager events 21 and 22 confirmed
successful logon at 21:08:14 and shell-start notification at 21:08:31.
Evidence: `arm-login-20260930-210947.tar`. The first-logon animation was
still visible at collection time; this is not yet visual desktop validation.

At 21:18:07 UTC, Shell-Core recorded `ShellInitTasks` finished. AppReadiness
reported `ART:UserFirstLogon` succeeded at 21:18:18. The first-logon animation
then disappeared and the automatic `tzsync.exe` console was visible. The
remaining 21 per-user app-install tasks were deferred by Windows; that is not
proof all installed applications are ready. Evidence:
`arm-login-20260930-211928.tar`.

## Previous deployment (2026-09-27)

The user is controlling the new installation. The installer is rebuilt from
`D:\bullshit\windows11arm\Windows11Arm64.iso` with a copy of
`D:\Downloads\autounattend.xml` at its root. At the user's explicit request,
only the six x86/amd64 component blocks were removed (two each from windowsPE,
specialize and oobeSystem). **Every other source byte is preserved**, including
the ARM64 settings, embedded Winhance scripts and all manual installer choices.
No edition, partition, account or password settings were added.
The resulting XML's SHA-256 is
`059122d814c67426a9981d851f98b5c19b8ea9e07e057019a7b30a5e88a693fb`;
the unchanged source hash is
`c200b07a2a32046f5c7d7c839d72a9281db2cc60cd6e09091f87a491d2086cfa`.
The previous separate answer ISO and preparation payload are not attached.

The preceding attempt with the completely unchanged source XML failed during
specialize: Setup tried to load an absent x86 Microsoft-Windows-Deployment
manifest and exited with 0x80070002 / 0x8007001F before Winhance ran. This was
confirmed from Panther logs through a temporary read-only disk connection,
without stopping that guest. The user then authorized stopping it and removing
only the incompatible architecture blocks. Its disk, UEFI variables, TPM and
launch script are preserved in `backup-specialize-x86-failure-20260927/` on the
QEMU host. The new attempt uses a blank 120 GiB disk; the user makes Setup's choices.

The NVMe disk has `bootindex=1`; the installation ISO has `bootindex=2`.
The same priorities were applied live through QEMU's `bootindex` properties
while Setup was running, without resetting or stopping the VM. They are read
by firmware at the next reboot. An actual reboot has not been tested, to leave
the user's installation uninterrupted. Keep the UEFI variables file persistent.

The clean reinstall was started on 2026-09-26. **Installation and GitHub/Forgejo
registration are not yet verified.** `systemctl is-active` proves only that
QEMU is running. Check Setup, the installed edition, Winhance logs and runner
services before calling it a runner. The former guest disk, answer ISO, UEFI
variables and TPM state are archived with `.pre-unattended-20260926` names.
The later failed installation is preserved in
`/var/lib/runner-appliances/windows-arm64/backup-unattend-failure-20260926/`.

| Item | Location |
| --- | --- |
| Host | `runner@10.77.0.40`, SSH key `~/.ssh/macos_runner` (existing host key alias `172.19.136.46`) |
| Original ISO | `/var/lib/runner-appliances/windows-arm64/Windows11Arm64-original.iso` |
| Active installer | `/var/lib/runner-appliances/windows-arm64/Windows11Arm64-github-0db35390-arm64-20260929.iso` (original installer files plus pinned GitHub XML with only x86/amd64 components removed) |
| ARM64 driver ISO | `/var/lib/runner-appliances/windows-arm64/virtio-win.iso` |
| Driver ISO SHA-256 | `303f7ae40dad495d6ae474fdc571df58958a4dbc5c37a522d80f9a203867949d` |
| Guest tool ISO | `/var/lib/runner-appliances/windows-arm64/payload.iso` (ARM runner packages, NSSM and `Prepare-WindowsArm.ps1`) |
| Guest disk | `/var/lib/runner-appliances/windows-arm64/windows-arm64.qcow2`, 120 GiB virtual |
| Previous failed attempt | `/var/lib/runner-appliances/windows-arm64/windows-arm64.failed-pro-20260925.qcow2` (retained for diagnosis) |
| TPM state | `/var/lib/swtpm/windows-arm64-runner/` |
| Service | `/etc/systemd/system/windows-arm64-runner.service` |
| Guest network | QEMU user networking; SSH `127.0.0.1:52222`, RDP `127.0.0.1:53389`, agent `10.77.0.40:8445` on the Ubuntu host |
| Guest console | VNC `127.0.0.1:5903` on the Ubuntu host |

The **previous, no longer attached** answer file created local administrator `admin` with the password entered
through the host's masked prompt. The password is not copied into this
repository or the QEMU launch command. Windows answer files necessarily carry
the credential in recoverable form; keep the answer ISO restricted to root.

`start-qemu.sh` uses the distribution's AAVMF firmware, an NVMe system disk,
USB installer and VirtIO driver media, a VirtIO network card, a private
software TPM, 8 virtual CPUs and 8 GiB RAM. The driver ISO contains
`NetKVM/w11/ARM64/netkvm.inf`. The unattended media bypasses Secure Boot
checks; the AAVMF firmware is the non-Secure-Boot variant. Windows may need
its ARM64 network driver installed before the forwarded SSH/RDP ports work.
The new upstream Winhance script no longer removes `OpenSSH.Server`; its
installation and availability must still be checked after Setup finishes.
`Prepare-WindowsArm.ps1` on the guest tool ISO locates the attached ARM64
VirtIO network driver, installs it, then reinstalls SSH and enables RDP.
It installs the platform's public SSH key for the `admin` account; the private
key remains on the Windows host at `D:\HyperV\runner-platform\ssh\id_ed25519`.
The tool ISO is currently detached. Do not run preparation steps while the user
is controlling Windows Setup.

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
enrolment, guest agent installation and fleet creation are described above;
see the dated handoff status below for the current state.

## 2026-10-02: M4 completion and return handoff

The ARM guest was cleanly shut down for a portable export. Other VMs were
left running. `portable/Start.command` starts the exported guest on Apple
Silicon using HVF, eight vCPUs and 8 GB RAM. The Windows startup task finishes
C++/Windows SDK, native Java and Android SDK installation, then runs real
build checks under separate GitHub and Forgejo test service identities.
No live forge jobs are accepted at Stoney's Mac. Successful checks trigger
a clean shutdown and a verified standalone return archive.

Source export: `/var/lib/runner-appliances/windows-arm64/handoff-20261002-212922`
on the Ubuntu QEMU host. Automatic local receipt targets
`D:\Downloads\NoMercy-Windows-ARM64-M4`; `transfer-status.json` must say
`ready` before sharing its `.tar` and `.tar.sha256`. The receiver verifies
the downloaded archive against the source SHA-256. The export checks both
disks against their originals and removes backing-file dependencies.

The guest preparation task was installed successfully, and script syntax
and targeted shutdown/transfer checks passed. **Actual M4 HVF boot and
guest build tests have not yet been verified.** See `portable/LEESMIJ.txt`.

The previously enrolled `rnr-agent` is disabled for transfer. Its controller
private key was removed from the guest's live filesystem after a verified
backup to the restricted host folder
`D:\HyperV\runner-platform\stage\arm-portable-private-20261002\agent.key`.
This backup must not be included in the portable package. The package has
its own local SSH access key, so share it privately. After return, restore
or rotate the worker identity, enable the agent, and register/test the two
real forge listeners. Never start both copies with the same worker identity.

## 2026-10-07: returned M4 build environment

Stoney returned `NoMercy-Windows-ARM64-retour-20261007-014458.tar`
(22,169,917,440 bytes), expected SHA-256
`41236765df6558f585526de8bd2741fae93b0f1dbb200f8382f3d04a8e7e45a1`.
The included reports record successful HVF boot and all 15 build-environment
checks passing under each of the GitHub and Forgejo test service accounts.
They cover native ARM64 MSVC, Rust, .NET, Go, Java, LLVM/CMake, Android SDK,
and the other installed command-line tools. These were local service-account
tests; real forge registration and jobs remain a separate verification step.

The first automatic return failed because the guest's power-button action
was set to do nothing. Stoney shut Windows down from within the guest before
making the checked return copy. `portable.py stop` now requests shutdown
over SSH first and uses ACPI only as a fallback. `Set-ArmShutdownPolicy.ps1`
sets the active scheme's AC/DC power-button action to shutdown (index 3),
and preparation applies it before accepting an existing ready status.
The returned guest must receive this setting during restoration as well.

The returned disks used Zstandard compression. Ubuntu's packaged `qemu-img`
could verify them, but the custom QEMU 11.1.2 runtime was built without Zstandard
and rejected them with `qcow2: unknown compression type: 1`. Restore therefore
converts to standalone, uncompressed qcow2 with the compatible zlib header,
checks the new images and compares their guest-visible bytes before activation.
The original returned disks are retained in `returned-zstd-20261007`; the prior
local VM disks, UEFI variables and TPM are retained in `before-return-20261007`.
Future portable exports use zlib compression for compatibility with both hosts.

The returned archive checksum and complete extracted manifest have been verified.
Both converted disks passed `qemu-img check` and guest-byte comparison. QEMU is
running again with the converted disks. On its first local boot Windows showed
"You're 100% there. Please keep your computer on"; later disk counters continued
to increase. Guest SSH, worker restoration, the ACPI shutdown/restart test and
real forge health jobs have not completed yet.

The host runs `D:\HyperV\runner-platform\stage\arm-return-20261007\Activate-Return.ps1`
in the background, resuming after import and waiting for SSH without stopping
Windows. It restores the saved worker identity, applies the shutdown policy,
disables the completed portable task, enables the worker at startup, tests
actual clean ACPI shutdown and restart, then creates one runner for each forge
and dispatches the two health jobs sequentially. Failures are retained in
`activation.stderr.log`; full output is in `activation.log` and
`activation.stdout.log` beside the script. Once controller-side completion
starts, `/data/arm-return-20261007-status.json` inside `rnr-controller` records
its phase and actual forge outcomes. Do not report the ARM runners as ready
until this report says `ready` with both conclusions `success`.

The user signed into the returned guest on 2026-10-07. The native console
confirmed `AutoAdminLogon=0` and no `AutoLogonCount` value. Persistent
automatic sign-in is not enabled. Runner startup must work as services
without requiring an interactive sign-in.

SSH was still unreachable after sign-in. `sc start sshd` and `sc query sshd`
both returned error 1060 (service absent), although
`C:\Program Files\OpenSSH\sshd.exe` remained present. A native console
repair was launched using the previously verified signed ARM64 MSI (SHA-256
`7a17d0e22d004fb47ca4bfd8fef926fa305de4ebf70a6f3c7a29c39aabef0023`).
The guest fetched the repair script, submitted its initial diagnostic report,
and downloaded the MSI. Installation completion, restored SSH, and live
forge registration are still pending. The existing background activation
continues waiting for SSH; no other VM was stopped or restarted.

The first MSI repair actually ended with error 1603 at 09:55:38 guest time.
Its SecureRepair step tried to read `C:\openssh.msi`; the downloaded source
had a different filename. The signed MSI was copied with the correct name
by the repair script on the read-only `ARMSHREPAIR` media (guest F:).
Launching `F:\run.cmd` with plain Enter started the operation at 10:49:38;
the MSI completed with exit code 0 at 10:52:42. Its final report confirms
automatic startup, `sshd` RUNNING, and an enabled SSH firewall rule.
A platform-key SSH connection subsequently returned `ARM-SSH-READY` with
exit code 0. Earlier malformed interactive command lines were caused by
console input problems; they are not evidence of a Windows installer fault.

The first activation attempt then stopped because Windows blocked
`Restore-ReturnedArm.ps1` under its execution policy. Activation was corrected
to use `-ExecutionPolicy Bypass` only for the known restoration process,
suppress startup progress records, and allow a 60-second SSH connection
timeout. The resumed activation writes to `activation-resume.stdout.log`
and `activation-resume.stderr.log`; it reuses the imported disks and identity
backup. Worker restoration, reboot verification, registration, and real forge
jobs still require successful completion before marking the runner ready.

The resumed restoration started `rnr-agent` with Automatic startup. The actual
ACPI shutdown test passed, and the worker supplied a fresh heartbeat after
the subsequent boot. Its interactive desktop took several minutes to appear.
At the user's request, persistent autologon was then enabled for local `admin`
using a verified credential and the Windows LSA `DefaultPassword` secret;
no plaintext registry password or `AutoLogonCount` is used. Local credential
loading explicitly uses the Windows PowerShell security module and trims
the protected file's trailing newline. The verified result is recorded in
`stage/arm-return-20261007/autologon-status.json` on the Windows host.
No reboot was performed by the autologon configuration; automatic interactive
sign-in at the next boot still needs verification. The two ARM specs have
been created, but provisioning is still held on unanswered worker API reads.
Do not confuse their presence in the dashboard with successful forge registration.

The initial provisioning failures were caused by the guest agent firewall:
the listener was present on `10.0.2.15:8443`, but the rule allowed only
`10.0.2.2`. Direct controller connections through the QEMU forward also need
the controller source address. Adding `10.77.0.10` to that same restricted rule
changed a 75-second connection reset into a successful authenticated `hello`
response in 0.8 seconds. The ARM installer now permits both sources, while
retaining mutual TLS and its existing port and local address restrictions.
No controller timeout increase or guest reboot was needed for that fix.

After the network fix, an authenticated service-status read took 17.5 seconds.
Windows ARM reads now use a 120-second outer deadline, with no overlapping
retries, and a 115-second socket timeout. Other platform profiles retain their
existing deadlines. Provisioning and dashboard reads select this profile from
the spec or the worker's declared architecture. The existing provisioning and
adapter tests passed after installing their missing local test dependencies.

The controller and dashboard correction is deployed in separate images based
on their previous images, preserving the dashboard UI. The prior compose file
is backed up as `/etc/runner-platform/backups/arm-agent-reads-20261007-compose.yml`.
Only those two control-plane containers were recreated; runner VMs remained
running. This intentionally ended the original activation monitor. Completion
now runs from `stage/arm-return-20261007/Complete-Return.ps1`, logging to
`completion.stdout.log` and `completion.stderr.log`, and executes the persisted
`/data/arm-return-20261007/complete-return.py` in the controller. It reuses the
existing runner IDs and idempotency keys. The latest confirmed Forgejo state
is `provisioned`; GitHub preparation and both real health jobs are still pending.

The completion monitor also handles the actual forge API differences: GitHub
reports a completed run with `status=completed` and a separate `conclusion`,
whereas the verified Forgejo API reports `status=success` without a conclusion.
It now recognizes Forgejo's terminal statuses and records each selected run ID
and URL while the job runs. The monitor was replaced while still waiting for
the runners, before dispatching health jobs, so this change did not restart a
job or create extra runners. The GitHub ARM health workflow is confirmed active.

### October 7: guest crash and post-crash sign-in delay

Windows crashed at 10:56:58 UTC and restarted internally at 10:58:14 UTC.
The QEMU process remained alive; its PID cannot establish guest boot continuity.
The preserved kernel dump and WinDbg analysis identify bugcheck 0x135,
`REGISTRY_FILTER_DRIVER_EXCEPTION`, callback in `WdFilter.sys`, with the
Windows Update process `MoUsoCoreWorker.exe` on the failing thread. The
exception stack is `WdFilter -> ExFreeHeapPool -> ViFreeTrackedPool`.
Driver Verifier flags are zero. This identifies the failing driver path,
but does not establish the underlying driver defect or an emulator defect.
Do not disable Defender or repeat installation based only on that attribution.
The dump and analysis are kept in the protected local return stage.

After this restart, System.evtx recorded State Repository Service hanging
at startup and its AppX/capability dependencies failing. The user saw a black
sign-in screen before the desktop background/icons returned. A new worker
heartbeat was confirmed healthy at 11:21 UTC. Forgejo reported its ARM runner
idle; GitHub was still offline even though its Windows service reported
running. Neither a forced reboot nor an OS reinstall was performed for this
incident, and the shared Linux/macOS VMs remained running.

The completion helper had incorrectly required `actual_state=running`;
registered idle runners use `idle`. It now accepts running/idle/busy only
while the worker is healthy. Existing creation idempotency keys are retained.
The first real GitHub ARM health check is run 37613749684, dispatched at
11:23 UTC; success and the subsequent Forgejo health check remain pending.
GitHub's service read returned running, but its latest Runner.Listener
log was still from 10:41 UTC, before the Windows crash; no post-reboot
listener log existed at 11:30 UTC. A scoped service restart was queued only
after GitHub freshly confirmed offline/not busy and the worker was healthy.
Operation: f57cee70-58c3-4d1b-a3ec-640dd76bd86f. The VM and Forgejo service
were left running. A preceding API lookup encountered temporary DNS failure;
that attempt was held because the forge state was unknown.