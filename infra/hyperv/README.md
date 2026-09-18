# Runner platform on Hyper-V (phase 5)

Two VMs on an internal network of their own: the control plane, running the
controller, and the first Linux worker, running the agent and its runner units.
They sit beside the WSL fleet and change nothing about it. The 13 runners and
the dashboard in WSL keep running as they are, and the controller manages only
the runners it creates.

| VM | Role | Memory (static) | vCPU | Disk | Address |
| --- | --- | --- | --- | --- | --- |
| `rnr-control` | controller + receiver | 4 GB | 2 | 64 GB | 10.77.0.10 |
| `rnr-linux-1` | agent + runner units (2 x 6 GB) | 16 GB | 8 | 400 GB | 10.77.0.20 |

The switch is `rnr-internal` (Internal, 10.77.0.0/24), with the host at
10.77.0.1 and a NetNat `rnr-internal-nat` for outbound traffic. Every value is
in `settings.psd1`. Why these values: design section 20, OPEN-5 and OPEN-6.

## Order

```powershell
# 1. Not elevated. Downloads and verifies the Ubuntu 24.04 cloud image,
#    converts it, and writes one seed image per VM. Changes nothing on the host.
.\infra\hyperv\Prepare-RunnerPlatform.ps1

# 2. ELEVATED. The switch, the NAT and the two VMs. -WhatIf first shows the plan.
.\infra\hyperv\New-RunnerPlatformVMs.ps1 -WhatIf
.\infra\hyperv\New-RunnerPlatformVMs.ps1

# 3. Not elevated. Software, the controller's authority, the worker enrolled
#    and its agent running. Ends when the worker reports healthy.
.\infra\hyperv\Initialize-RunnerPlatform.ps1
```

Then the first runner, on the control plane (`ssh -i D:\HyperV\runner-platform\ssh\id_ed25519 rnr-admin@10.77.0.10`):

```sh
sudo docker exec rnr-controller python -m control capacity forgejo-linux-x64 1
sudo docker exec rnr-controller python -m control status
```

## What protects the host and the running CI

- **Commit.** The host has no pagefile, and a static reservation is committed
  at once. `New-RunnerPlatformVMs.ps1` refuses a VM if less than 20 GB of commit
  would be left after it. It also refuses a plan whose VMs add up to more than
  30 GB, the part of the `.wslconfig` margin set aside for them.
- **No External switch.** Creating one would briefly take the host's network
  down, and every running job's network with it.
- **The pilot takes no production job.** The controller registers Forgejo
  runners with the label `rnr-pilot:docker://node:20`. Forgejo matches jobs by
  label name, and no workflow asks for that one. The GitHub cell is not set up.
  A GitHub runner always carries `self-hosted`, `Linux` and `X64`, and a
  production job could ask for exactly those. It waits for a runner group that
  no repository may use (`GITHUB_DRAIN_GROUP`, spec 13.1).
- **Each unit is held to 6 GB.** A runaway job cannot take the worker down, and
  the agent with it.
- **Only two secrets leave the host.** `FORGEJO_INSTANCE_URL` and
  `FORGEJO_API_TOKEN` go to the control plane in a 0600 file. OIDC, `GH_TOKEN`
  and the rest of `.env` stay where they are.
- **Nothing reaches the worker but the controller and the host.** Its firewall
  accepts the agent's port from 10.77.0.10 and SSH from 10.77.0.1, and nothing
  else.

## Rollback

```powershell
# ELEVATED. Refuses while a worker still holds runners: scale their fleets to 0
# first, so their records are deleted at the forge.
.\infra\hyperv\Remove-RunnerPlatform.ps1
```

This removes the VMs, the NAT and the switch, by the names in `settings.psd1`
and nothing else. The WSL fleet was never changed, so nothing needs restoring
there.
