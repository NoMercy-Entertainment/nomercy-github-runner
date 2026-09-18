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
    # And the platform's VMs together stay inside this. The live figure above
    # does not see that the WSL VM may still grow to its 120 GB cap:
    # .wslconfig budgets WSL 120 + about 90 outside it = about 210 of 256 GB,
    # so about 45 GB is left, and this takes 30 of it (OPEN-5).
    VmBudgetGB    = 30

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
            MemoryGB  = 16
            Cpus      = 8
            DiskGB    = 400
            Address   = '10.77.0.20'
            MgmtMac   = '00155D77140A'
            UplinkMac = '00155D77140B'
            # What the agent declares, and placement respects: room for two
            # runners at 6 GB each, leaving the guest and its engine 4 GB.
            MaxRunners   = 2
            RunnerMemGB  = 6
        }
    }

    # Ports. The agent listens on its worker's address; the controller's
    # receiver and the dashboard on the control plane's.
    AgentPort     = 8443
    ReceiverPort  = 8444
    DashboardPort = 9200

    # The admin account cloud-init makes in every guest, reached by SSH with
    # a key made for this platform only.
    AdminUser     = 'rnr-admin'
}
