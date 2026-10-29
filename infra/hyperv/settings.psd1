# The runner platform's Hyper-V layout: one place for every name, address and
# size, read by every script here. Decisions OPEN-5 and OPEN-6 of the design
# (section 20) are the source of these values.
@{
    # Everything this platform creates lives under here, apart from the
    # existing macOS appliance (D:\HyperV\macos-runner), which is not touched.
    Root          = 'D:\HyperV\runner-platform'

    # OPEN-6: an Internal switch for management, never an External one - that
    # would briefly take the host's network, and every running job's, down.
    # Controller, agents and the host's SSH talk over it, on static addresses.
    Switch        = 'rnr-internal'
    Prefix        = '10.77.0.0/24'
    HostAddress   = '10.77.0.1'
    PrefixLength  = 24
    # Outbound traffic goes through a second adapter on the Default Switch,
    # the host's own NAT that the macOS appliance already uses - not through
    # a NetNat of our own. WinNAT beside the NAT networks WSL and Docker
    # Desktop keep is a known source of conflict, and WSL's network is every
    # running job's network.
    UplinkSwitch  = 'Default Switch'
    # The resolvers the WSL distro is pinned to, for the same reason: a DNS
    # proxy that dies takes every runner with it.
    Dns           = @('1.1.1.1', '8.8.8.8')

    # Ubuntu 24.04 cloud image, verified against Canonical's SHA256SUMS.
    ImageUrl      = 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'
    ImageSums     = 'https://cloud-images.ubuntu.com/noble/current/SHA256SUMS'

    # The WSL distro whose engine does the file conversions (qemu-img,
    # xorriso) in throwaway containers, so nothing is installed on the host.
    Distro        = 'github-runners'

    # A VM is created only when, after its static memory is reserved, at
    # least this much commit is still free. Static memory is a reservation;
    # commit exhaustion has killed the WSL VM twice (R-2).
    CommitReserveGB = 20
    # Current managed VMs: Linux 80, controller 4, Windows x64 36 GiB.
    # The existing macOS host is outside this collection. Every allocation
    # also requires the actual
    # host commit reserve above; this budget is not evidence of free memory.
    # 2026-10-23: Linux 32 at startup (dynamic to 184), controller 4,
    # Windows x64 36. The Linux ceiling is a cap, not a reservation.
    VmBudgetGB    = 132

    # Operator-selected target (2026-10-10). Individual VM sizes below describe
    # the current deployment until its prepared maintenance cutover completes.
    UniformRunnerProfile = @{
        Cores = 8
        MemoryGiB = 16
        StorageGiB = 100
        CacheGiB = 40
    }

    # The GitHub cells. The image is what the fleet on the WSL worker is
    # already made from; the drain group is a runner group with no repository
    # in it, which is where a runner waits while it finishes its last job.
    # What a unit is built FROM, per forge: the image that fleet's runners
    # already run. The unit image itself is built on each Linux worker from
    # these plus the three entry points the agent drives
    # (images/linux/unit), and is what `RUNNER_UNIT_IMAGE_*` names. A runner
    # rebuilt from the base alone has no /runner/register - which is what a
    # rebuild discovered the hard way (2026-09-20).
    Forgejo = @{
        # Built on this host (2026-10-23): forgejo-runner/Dockerfile plus the
        # guarded binary from images/windows/build-forgejo-runner.sh v13.1.0
        # guarded, verified against images/forgejo-admission/PROVENANCE.json.
        # No registry publishes it; Initialize-RunnerPlatform.ps1 saves it
        # from Docker Desktop for the worker.
        BaseImage = 'nomercy/forgejo-runner:13.1.0-nomercy-admission.1'
        # What Forgejo Linux jobs run in (FORGEJO_RUNNER_LABELS names them as
        # 172.17.0.1:5000/<image>): saved from this host and pushed into each
        # worker's job-image registry (guest\job-image-registry.sh). Both
        # carry the hosted tool cache (images/linux/unit/toolcache).
        # docker build -f images/linux/unit/Dockerfile.jammy \
        #   -t nomercy/job-ubuntu-22.04:20261027 images/linux/unit
        JobImages = @(
            'nomercy/github-runner-base:tools-20261027.2'
            'nomercy/job-ubuntu-22.04:20261027'
        )
    }

    GitHub = @{
        # images/linux/unit/Dockerfile.tools on the base built from the root
        # dockerfile on this host (PHP 8.5 only, Node 26; 2026-10-27), plus
        # the hosted tool cache (Python 3.10-3.14, Node 20/22/24/26; .2):
        # docker build -f dockerfile -t nomercy/github-runner-base:20261027 .
        # docker build -f images/linux/unit/Dockerfile.tools \
        #   --build-arg BASE=nomercy/github-runner-base:20261027 \
        #   -t nomercy/github-runner-base:tools-20261027.2 images/linux/unit
        BaseImage   = 'nomercy/github-runner-base:tools-20261027.2'
        RunnerMemGB = 16
        DrainGroup  = 'drain'
        RunnerGroup = 'Fillz'
        TrustedOwners = 'NoMercy-Entertainment,Fill84'
        # What a GitHub runner on the Windows worker is made from: a template
        # on that worker holding the runner GitHub publishes, checked against
        # the hash from its own release notes (images/windows/manifest.json).
        WindowsTemplate    = 'actions-runner-v2.338.0-windows'
        WindowsRunnerMemGB = 8
    }

    # OPEN-5: static memory, inside the measured margin.
    VMs = @{
        'rnr-control' = @{
            Role      = 'control-plane'
            MemoryGB  = 4
            Cpus      = 2
            DiskGB    = 64
            Address   = '10.77.0.10'
            # Fixed, in Hyper-V's own range, so the seed image can tell the
            # two adapters apart before the VM exists.
            MgmtMac   = '00155D770A0A'
            UplinkMac = '00155D770A0B'
        }
        'rnr-linux-1' = @{
            Role      = 'linux-worker'
            # Live since it got its own window of cores: 80 GB static,
            # 56 vCPU (corrected 2026-09-23; this entry used to read 16/8,
            # which is what the VM was provisioned with before -
            # settings.psd1 is the platform's one source of sizes, so a stale
            # row here would rebuild the worker at a fifth of its memory).
            # Rebuilt 2026-10-23 on dynamic memory, the owner's choice: the
            # VM holds only what its runners use and grows to the ceiling
            # under load - ten GitHub runners at 16 GiB plus three Forgejo
            # runners at 8 GiB is 184 GB. MemoryGB is the startup amount and
            # what the budget check counts.
            MemoryGB  = 32
            DynamicMemory = @{ MinGB = 16; MaxGB = 184; BufferPct = 100 }
            Cpus      = 56
            DiskGB    = 400
            # The runners' own disk (one ext4 image each, and the swap file),
            # a dynamic VHDX of its own next to the OS disk.
            DataDiskGB = 2304
            Address   = '10.77.0.20'
            MgmtMac   = '00155D77140A'
            UplinkMac = '00155D77140B'
            # What the agent declares, and placement respects: thirteen
            # runners, each held to 16 GiB; Forgejo's fleet setting lowers
            # its three to 8 GiB.
            MaxRunners   = 13
            RunnerMemGB  = 16
        }
        'rnr-windows-1' = @{
            Role      = 'windows-worker'
            # Live 2026-10-11: two 16-GiB runner ceilings plus 4 GiB for
            # Windows. Each runner is limited to eight of these processors.
            MemoryGB  = 36
            Cpus      = 16
            DiskGB    = 200
            # 320, not 200 (corrected 2026-09-24): this guest hosts two
            # runners and each takes a fixed 100 GB disk of its own, so 200
            # holds one and refuses the second - "Insufficient physical NTFS
            # space for fixed disk and host reserve", which is the storage
            # helper correctly declining to overcommit. Two disks, their
            # manifests and the host reserve need room to spare.
            DataDiskGB = 320
            Address   = '10.77.0.30'
            MgmtMac   = '00155D771E0A'
            UplinkMac = '00155D771E0B'
            # Its agent capacity lives in WindowsGuests below, not here - unlike
            # rnr-linux-1 above, nothing reads a MaxRunners/RunnerMemGB pair off
            # this entry for a windows-worker (Initialize-RunnerPlatform.ps1's
            # and Install-ControlPlaneDashboard.ps1's own reads of .RunnerMemGB
            # off VMs entries are both filtered to Role -eq 'linux-worker').
        }
    }

    # Ports. The agent listens on its worker's address; the controller's
    # receiver and the dashboard on the control plane's.
    AgentPort     = 8443
    ReceiverPort  = 8444
    DashboardPort = 9200

    # The admin account: cloud-init makes it in every Linux guest, and it is
    # also the name the operator gives the Windows guest's manually created
    # local administrator (W10b) - reached by SSH with a key made for this
    # platform only.
    AdminUser     = 'rnr-admin'

    # OPEN-2 and OPEN-3: the Windows worker is this host, its runners process
    # trees under their own virtual accounts. Everything it installs is under
    # Root, and every binary is checked against the hash pinned here.
    Windows = @{
        HostId        = 'beast-unit'
        Root          = 'C:\ProgramData\nomercy'
        # A Python of its own: the one on this host is a per-user Store app,
        # which a service's virtual account cannot run.
        PythonUrl     = 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip'
        PythonSha256  = '90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907'
        # A copy of the NSSM the Windows runner already runs under, 2.24,
        # identified by hash - never by running it (`nssm version` opens a
        # window and blocks).
        # Kept under the platform root since the retired NSSM Forgejo
        # runner's folder was removed (2026-10-23).
        NssmSource    = 'D:\HyperV\runner-platform\artefacts\nssm.exe'
        NssmSha256    = 'f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97'
        Template      = 'forgejo-runner-v13.1.0-windows'
        # The guarded build with native job events and owner admission
        # (PROVENANCE job_events=1, admission_version=1), the same one the
        # Linux and macOS Forgejo runners run. The plain v13.1.0 build emitted
        # no job events, so a busy Windows Forgejo card could not name its
        # job (2026-10-27).
        RunnerBinary  = 'D:\HyperV\runner-platform\artefacts\forgejo-runner-v13.1.0-admission\forgejo-runner-v13.1.0-nomercy-admission.1-windows-amd64.exe'
        RunnerSha256  = '2550f5cf69307916422cc96ba9c73edff7867a8b13bfe5ddbed15b88e54c3b82'
        MaxRunners    = 2
        RunnerMemGB   = 8
        # What the Windows Forgejo runners register with: the labels the
        # retired NSSM runner carried (C:\forgejo-runner\.runner), which the
        # workflows ask for. The pilot label is gone with the pilot.
        Labels        = 'windows-2022:host,windows-latest:host'
    }

    # T-23 (W10b-2): rnr-windows-1's own worker block - its governing capacity,
    # not a second unused copy of BEAST-UNIT's numbers. Its host id is the
    # VM's own name (VMs above, which already carries its address, memory and
    # disk); this carries only what is not already there - the agent's
    # capacity and the storage root on its own second disk, the one
    # New-WindowsGuest.ps1 attached as DataDiskGB and Initialize-WindowsGuest.ps1
    # brings online (repartitioning a disk is not Install-WindowsWorker.ps1's
    # job) - kept apart from the Windows block above, which keeps describing
    # BEAST-UNIT until it is retired (task step 9). Install-WindowsGuestWorker.ps1
    # passes MaxRunners/RunnerMemGB straight through to
    # Install-WindowsWorker.ps1's own -MaxRunners/-RunnerMemGB, which is what
    # actually reaches the agent's capacity block - not the shared Windows
    # block, and not VMs.'rnr-windows-1' (which does not carry these at all;
    # see the comment there). Everything an install does not need a
    # per-worker value for - the pinned Python, NSSM and runner-binary
    # hashes, the template name, the labels - stays shared, read from Windows
    # above.
    WindowsGuests = @{
        'rnr-windows-1' = @{
            MaxRunners  = 2
            RunnerMemGB = 8
            StorageRoot = 'D:\runner-disks'
        }
    }

    # macOS template defaults; capacity is managed by the controller.
    MacOS = @{
        GitHubTemplate = 'actions-runner-v2.338.0-macos-r20261008'
        ForgejoTemplate = 'forgejo-runner-v13.1.0-macos-r20260921'
        ForgejoBinary = '/Users/runner/templates/forgejo-runner-v13.1.0-macos-r20260921/forgejo-runner'
        ForgejoSha256 = 'f9f9ed421d6d1e71436b92e9b8891fb03b44c713e87e545c6e86423be2edb07a'
        ForgejoLabels = 'macos-latest:host,macos-15:host'
    }
}
