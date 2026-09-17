# Runner exporter

Telemetry for the two Forgejo runners that are not containers on the engine
the dashboard talks to, so their cards stop being blank next to every other
runner on the page.

- `beaststack-windows-runner` is the `forgejo-runner` Windows service on
  BEAST-UNIT, read locally.
- `beaststack-macos-sequoia` is a QEMU container inside a Hyper-V VM, read
  over SSH from BEAST-UNIT.

One exporter serves both, because BEAST-UNIT is the only host that can reach
both: the dashboard's WSL network has a route to the VM but no traffic
crosses it. The alternative, an exporter on the VM behind a `netsh`
portproxy, would add a second dependency on the mechanism that has already
taken this dashboard dark once.

## Install

Elevated, on BEAST-UNIT:

```powershell
.\exporters\windows\install.ps1
```

Then point the dashboard at it and restart it:

```
EXTERNAL_EXPORTER_URL=http://172.28.192.1:9101/metrics
```

Leave that key empty to turn the feature off. The Elsewhere cards then render
exactly as they did before it existed.

## What it serves

`GET /metrics` only. No writes, no shell passthrough, every external command a
fixed argument list with a timeout. The port is opened to the WSL subnet
alone, not to the LAN.

A probe that cannot answer returns `null`, never zeros. An unreachable machine
must not be able to render as an idle, healthy one.
