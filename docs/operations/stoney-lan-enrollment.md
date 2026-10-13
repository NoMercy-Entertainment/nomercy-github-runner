# Stoney: private LAN connection and runner enrollment

Prepared 2026-10-13. Issue #5 remains open. No tunnel, routes, port forwards,
certificates or lending have been activated by this preparation.

## Connection design

Use a routed WireGuard site-to-site tunnel between two dedicated Linux gateway
VMs. Fillz LAN is currently 192.168.178.0/24; Stoney LAN is 192.168.2.0/24.
The controller is 10.77.0.10 on the separate Fillz management network. The
proposed tunnel subnet is 10.203.0.0/30; check it is unused at both sites first.
Neither endpoint advertises 0.0.0.0/0, ::/0 or an internet exit. Each household
keeps its own default gateway, DNS and public internet IP. DHCP and broadcasts
stay separate; Ethernet bridging is unnecessary for SSH, HTTPS or runner events.

The encrypted tunnel uses the two existing internet connections to transport
LAN packets. The fleet endpoints remain private. A geographically separate
connection with no internet carrier would require a separate physical/provider
link. A subnet-only VPN does **not** make either site use the other's internet.

WireGuard requires at least one reachable UDP endpoint. Determine router
models, available port forwarding and public IPv4/IPv6/CGNAT at both sites
tomorrow. If neither endpoint is reachable, use two Linux Tailscale subnet
routers in one jointly administered tailnet instead: approve only the listed
LAN/controller routes, select no exit node and enable no Funnel. This is an
alternative carrier for the same private routed design, not a full tunnel.

Sources: [WireGuard routing](https://www.wireguard.com/),
[WireGuard setup](https://www.wireguard.com/quickstart/),
[Tailscale site-to-site](https://tailscale.com/docs/features/site-to-site).

## Fill in and install on the gateway VMs

1. Allocate and reserve gateway LAN addresses after checking DHCP and conflicts.
   The Fillz gateway needs access to the LAN and 10.77.0.10; use a VM, with no
   physical Windows host restart, pagefile change or VPN installation.
2. Generate WireGuard private keys independently on each gateway. Exchange
   public keys only. Copy `infra/network/wireguard/*.conf.example` to the
   respective gateway as a root-only `wg-lan.conf`; replace placeholders.
3. Confirm each `AllowedIPs` contains only the opposite LAN, opposite tunnel
   address and the explicitly listed controller /32. Never add a default route.
   Bring up both gateway interfaces and enable forwarding **inside these VMs**.
4. Configure the return routes on the site routers, or narrowly on participating
   VMs. Fillz controller routes 192.168.2.0/24 through its new gateway; Stoney
   needs a route back to 10.77.0.10/32. Household LAN clients need the reciprocal
   LAN route through their site gateway. Preserve all existing default routes.
   Avoid SNAT when reciprocal routes are available so access rules and audit
   records retain the original LAN source addresses.
5. Initially allow TCP 22 to the listed enrollment machines and TCP 443 between
   the two fleet controllers. Open agent mTLS ports 8443/8444 only between the
   enrolled workers and their controller, if cross-site control is chosen.
   Permit established replies. Keep Docker sockets/APIs private. Add explicit
   gateway firewall drops for traffic leaving `wg-lan` toward other destinations,
   including WAN interfaces, even if a client manually sets a default route.

For administration from the Fillz Windows machine, use an SSH `ProxyJump`
through the Fillz gateway/controller VM. This keeps the private SSH key on
Windows without changing that host's route table or enabling agent forwarding.

## Verify before registering anything

Record `ip -4 route show default`, `ip -6 route show default` and the result of
`curl -fsS https://api.ipify.org` at **both** sites before and after activation.
Defaults and each site's observed public IP must remain unchanged. Check
`ip route get 192.168.2.32` from Fillz and `ip route get 10.77.0.10` from Stoney:
only these private destinations should select the new gateway/tunnel.

Verify WireGuard handshake/counters (`wg show`), SSH in both directions and
verified private HTTPS. Disconnect the tunnel once and prove local runners,
local dashboard and ordinary internet continue working; reconnect and confirm
event delivery recovers. Route tests alone are not a live fleet-sharing proof.

## Private fleet HTTPS

Use exact HTTPS names for the two controllers, for example
`fleet-fillz.internal` and `fleet-stoney.internal`, with independently verified
certificates whose SANs contain those names. Mount the peer's public CA
certificate read-only into both the local dashboard and controller containers.
Do not exchange CA private keys or disable certificate verification.

Both containers need the same deployment-owned allowlist:

```dotenv
# Example on Fillz: replace the address with Stoney's actual controller IP.
FEDERATION_DIRECT_PRIVATE_PEERS={"https://fleet-stoney.internal":{"address":"192.168.2.60","ca_file":"/data/federation/stoney-ca.pem"}}
FEDERATION_DIRECT_CALLBACK_URL=https://fleet-fillz.internal
```

192.168.2.60 currently identifies the Docker VM; confirm the controller is
actually installed there before using it. On Stoney configure the inverse
origin/address/CA and callback. The allowlist pins the connection to one exact
RFC1918 address while verifying the original TLS hostname. It needs no public
DNS record. Configure private DNS or local browser name resolution for UI
access separately. The existing `DASH_PUBLIC_URL` remains the login callback
setting; the direct transport uses its separate private callback setting.

The public dashboard login endpoint does not have to change. Its proxy must
deny `/api/v2/federation/peer/*` on the public listener if peer ingress is to be
LAN-only. Expose those paths only on the tunnel/private HTTPS listener. Test
that denial from outside the tunnel before exchanging any peer credentials.
Do not activate NoMercy courier delivery over public callbacks for this setup.

The private transport source change is reviewed and deployed in the existing
controller/dashboard image (2026-10-13). Production still has lending disabled.
Adding a tunnel does not enable lending, grant hardware control or complete #5.

## Machines and SSH

| Machine | Address | User | Role |
|---|---|---|---|
| Mac mini | 192.168.2.32 | stoney | macOS worker |
| EAGLE | 192.168.2.201 | patri | Windows worker |
| Proxmox host | 192.168.2.100 | root | Hypervisor, not an extra runner |
| Proxmox Docker VM | 192.168.2.60 | root | Linux worker / candidate controller |

All use SSH port 22. Existing Fillz key:
`C:\Users\phill\.ssh\nomercy_stoney_ed25519`; only its `.pub` is shared.
Stoney reports the key installed on the Mac, Proxmox host and Docker VM.
EAGLE still needs the administrator authorized-keys step and verification.
Read SSH host-key fingerprints locally on the target machines before pinning
them; do not automatically trust keys first seen across the new tunnel.

EAGLE administrator PowerShell: append the verified `.pub` once to
`C:\ProgramData\ssh\administrators_authorized_keys`, preserving existing keys,
and verify ACLs permit SYSTEM and Administrators only. Verify actual logins on
all four targets. Mac-to-EAGLE's previously saved host key was unverified;
compare it locally rather than silently replacing it.

## Enrollment and remaining acceptance

Keep Stoney's controller separate from Fillz's controller for capacity lending.
Sharing uses restricted intent/result APIs; peers never receive Docker/root
access through federation. Cross-administration of normal runners is a separate
owner-granted dashboard role, not a capacity-peer permission.

Enroll Stoney's three workers through the existing platform installers only
after local OS/virtualization/storage prerequisites are checked. Every runner
uses 8 cores, 16 GiB RAM, 100 GiB disk and 40 GiB cache. Verify real enforced
limits, dependencies and a native job start/completion event on each runtime.
For macOS, verify a working Xcode installation and selected developer directory.
Do not register an unsupported profile to make its dashboard card look green.

Arcanum already has a scoped machine-reader contract (`fleet:read`, `jobs:read`)
and `/api/v2/events/jobs` SSE. Deliver its token privately; never paste it into
this document, source control or chat. Verify one real job's repository, commit,
job identifier and terminal result reach the consumer without status polling.

Issue #9 remains open until real SSH/enrollment and Arcanum consumption pass.
Issue #5 remains open until two actual fleets prove request, approval, scoped
registration, real job execution, pushed result, drain, expiry/revoke and absence.
Keep lending disabled until guarded templates and the private selected-repository
group are proven. Current macOS/mutable-root Linux lending eligibility gates
remain; ordinary runners are independent of those lending gates.
