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
    # And the platform's VMs together stay inside this. The WSL VM the figure
    # above once budgeted around is retired; the live cost is now the VMs
    # themselves. Corrected 2026-09-23 against rnr-linux-1's corrected entry
    # below (80 GB) plus rnr-control (4 GB) plus the new rnr-windows-1
    # (16 GB) = 100 of 110. Measured that day: host commit 169 of 256 GB
    # used, 87 GB free. CommitReserveGB is still enforced per VM on top of
    # this sum.
    VmBudgetGB    = 110

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
        BaseImage = 'ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest'
    }

    GitHub = @{
        BaseImage   = 'ghcr.io/nomercy-entertainment/nomercy-github-runner:latest'
        RunnerMemGB = 32
        DrainGroup  = 'drain'
        # What a GitHub runner on the Windows worker is made from: a template
        # on that worker holding the runner GitHub publishes, checked against
        # the hash from its own release notes (images/windows/manifest.json).
        WindowsTemplate    = 'actions-runner-v2.336.0-windows'
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
            MemoryGB  = 80
            Cpus      = 56
            DiskGB    = 400
            Address   = '10.77.0.20'
            MgmtMac   = '00155D77140A'
            UplinkMac = '00155D77140B'
            # What the agent declares, and placement respects: room for two
            # runners at 6 GB each, leaving the guest and its engine 68 GB.
            MaxRunners   = 2
            RunnerMemGB  = 6
        }
        'rnr-windows-1' = @{
            Role      = 'windows-worker'
            MemoryGB  = 16
            Cpus      = 8
            DiskGB    = 200
            DataDiskGB = 200
            Address   = '10.77.0.30'
            MgmtMac   = '00155D771E0A'
            UplinkMac = '00155D771E0B'
            MaxRunners  = 2
            RunnerMemGB = 8
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
        NssmSource    = 'C:\forgejo-runner\nssm.exe'
        NssmSha256    = 'f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97'
        Template      = 'forgejo-runner-v13.1.0-windows'
        RunnerBinary  = 'D:\HyperV\runner-platform\artefacts\forgejo-runner-v13.1.0\forgejo-runner-v13.1.0-windows-amd64.exe'
        RunnerSha256  = '82ea01bc63c3ba60526576f3d8ac491a1e77db0f8d3d55cc37bd39666a5f04c8'
        MaxRunners    = 2
        RunnerMemGB   = 8
        # What the Windows Forgejo runners register with: the labels the
        # retired NSSM runner carried (C:\forgejo-runner\.runner), which the
        # workflows ask for. The pilot label is gone with the pilot.
        Labels        = 'windows-2022:host,windows-latest:host'
    }
}
