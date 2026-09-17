# External runner telemetry

## The problem

Two Forgejo runners are not containers on this engine, so the dashboard
shows them in the Elsewhere section with only what the forge knows. The
forge's runner endpoint carries `name`, `labels`, `version`, `status`,
`id`, `description` and `ephemeral`. There is no CPU, no memory, no disk,
no active job and no last-seen timestamp. Those cards are therefore blank
next to every other runner on the page, and the one failure mode this
fleet has actually hit - the macOS runner going offline because its VM
root disk filled - is invisible from here.

## What the two runners actually are

Measured on 2026-09-17, not assumed:

- `beaststack-windows-runner` is the `forgejo-runner` Windows **service**
  on BEAST-UNIT itself, started through NSSM from `C:\forgejo-runner`.
  It is not a separate machine.
- `beaststack-macos-sequoia` runs two hops away. BEAST-UNIT hosts a
  Hyper-V VM `macos-runner` (Ubuntu 24.04, 8 cores, 24 GB, root LV 292 GB)
  at 172.19.136.46. That VM runs the container `macos-sequoia`, which is
  QEMU running macOS, plus a `novnc` container. noVNC is published on
  8899 and the macOS guest's SSH on 50922.

## Routing, which decides the shape

| From | To | Result |
| --- | --- | --- |
| dashboard container (WSL) | BEAST-UNIT 172.28.192.1 | reachable |
| dashboard container (WSL) | macOS VM 172.19.136.46 | route exists, traffic blocked |
| BEAST-UNIT | macOS VM port 22 | open, and the `macos_runner` key works |

An exporter on the macOS VM could not be scraped by the dashboard without
a `netsh` portproxy on BEAST-UNIT. That portproxy is an established part
of this deployment and an established failure point: the dashboard has
already gone dark once because its listener silently stopped being bound.
Adding a second dependency on it to make a status page work would be
trading a blank card for an unreliable one.

So: one exporter, on BEAST-UNIT, reachable by the dashboard directly. It
measures the Windows runner locally and reaches the macOS VM over SSH with
the key that already exists.

## Components

### `exporters/runner_exporter.py`

One file, standard library only, no third-party dependencies, so it runs
on the host's Python 3.13 unchanged.

Serves `GET /metrics` and returns JSON: a `runners` object keyed by the
runner name as Forgejo reports it, plus a `generated` timestamp. Any other
path is 404. It binds to a configured address and port and nothing else -
no write endpoints, no shell passthrough.

Two probes, selected by config, each returning the same shape or `None`:

- **windows_service** reads the `forgejo-runner` service locally: process
  CPU, working set, uptime, the service state, and free space on its
  drive. The active job comes from the runner's own log, matched with the
  pattern `docker_ops.RE_FORGEJO_TASK` already uses, so the two fleets
  report a job the same way.
- **macos_vm** runs one SSH command against the VM and parses the result:
  `docker stats` for the `macos-sequoia` container, `df` for the root LV,
  and the runner log for the active job. One connection per sweep, with a
  hard timeout, so a hung VM costs one timeout and not a wedged exporter.

A probe that fails returns `None`. It never returns zeros and never
returns the previous sweep's numbers. This mirrors `_forge_records`,
where a failed call stores `None` rather than replaying a stale answer.

### `exporters/windows/install.ps1` and `exporters/macos-vm/README.md`

The Windows install script registers the exporter through the same NSSM
binary that already runs the runner service, and adds the one firewall
rule the dashboard needs. Nothing is installed on the macOS VM: it is read
over SSH, so there is no service to keep alive there.

### `dashboard/external_telemetry.py`

Fetches the exporter and caches it, following `_forge_records` exactly:
a TTL for successes, a longer backoff for failures, the deadline read
AFTER the call finishes, and `None` as the explicit "could not ask"
sentinel. A dead exporter must never cost the collector sweep more than
its timeout, because that sweep also serves the GitHub fleet's telemetry.

Configured by one `.env` key, `EXTERNAL_EXPORTER_URL`. Absent or empty
disables the whole feature and the Elsewhere cards render exactly as they
do today.

### `docker_ops._elsewhere()`

Takes the telemetry map and merges it onto each record by name. A runner
with no telemetry keeps today's fields and gains nothing, so the section
degrades to its current behaviour rather than breaking.

### `templates/index.html`

The Elsewhere card grows the same three meters the other cards have. When
a value is `None` the meter renders as "unknown" and draws empty, the way
the grid already handles an unknown core count, rather than drawing a
confident zero.

## Failure behaviour

Every one of these must leave the page working:

- Exporter unreachable, refusing, slow, or returning malformed JSON.
- SSH to the VM failing while the Windows probe succeeds, and the reverse.
- `EXTERNAL_EXPORTER_URL` unset, malformed, or pointing somewhere else.
- The forge being unreachable at the same time as the exporter.

In each case the section renders, the other fleets are unaffected, and the
missing numbers read "unknown".

## Testing

Tests live beside the existing suite and follow its style: a docstring
that says why the test exists, then the assertion.

- The probe parsers, against captured real output from both machines.
- A failed probe yields `None`, never zeros and never the last good value.
- The cache does not replay a stale answer after a failure, and a failure
  is rate-limited by the backoff rather than the success TTL.
- `_elsewhere()` merges by name and is unchanged when telemetry is absent.
- No exporter configured leaves the payload byte-identical to today's.

## Explicitly not in scope

- Metrics from inside the macOS guest. The container and the VM disk are
  what this fleet's failures have actually turned on.
- Any control action on an external runner. These cards stay read-only,
  as their existing comment in `_elsewhere()` requires.
