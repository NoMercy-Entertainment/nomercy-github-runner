# Forgejo native job event smoke

`.forgejo/workflows/job-event-health.yml` runs only on manual dispatch. Each
dispatch schedules one chosen platform and outcome; the other platform jobs
are skipped. Check Windows x64 and macOS first, then Linux, and ARM last.
There is no checkout, build, action download or runner configuration change.
Every job retains the fleet's 7200-minute ceiling.

The labels were checked against fresh Forgejo registration records through
the controller's SecretStore and provider client on 2026-10-12:

| Platform input | Forgejo label | Shell |
| --- | --- | --- |
| `windows-x64` | `windows-2022` | `pwsh` |
| `macos-x64` | `macos-latest` | `bash` |
| `linux-x64` | `ubuntu-latest` | `bash` |
| `windows-arm64` | `windows-arm64` | `pwsh` |

Success echoes a fixed diagnostic message. Failure additionally exits with
code 1. Cancellation echoes and sleeps for at most 600 seconds: cancel only
this owned diagnostic run while its hold step is running. If it is not
cancelled, the sleep completes successfully; that does not prove cancellation.
Linux's existing `ubuntu-latest` label uses its configured Docker job executor;
the smoke adds no separate image or container configuration.

The operator can use the Forgejo Actions UI or the authenticated API. The
workflow must already exist on the selected ref. Dispatch endpoint:

```text
POST https://forgejo.phillippepelzer.me/api/v1/repos/FiLL/nomercy-runners/actions/workflows/job-event-health.yml/dispatches
Authorization: token <operator's existing token>
Content-Type: application/json
```

```json
{"ref":"master","return_run_info":true,"inputs":{"platform":"windows-x64","outcome":"success"}}
```

Use `failure` and `cancellation` for the other outcomes, and a table value
for each later platform. Never put the token into workflow inputs or logs.
The installed Forgejo 16.0.5 OpenAPI schema confirms `return_run_info`: dispatch
can return HTTP 201 with this owned run's `id`, `jobs` and `run_number`, instead
of a bodyless HTTP 204. Retain the returned ID rather than searching for the
latest run. For the cancellation case, after its hold step starts, cancel only
that returned run:

```text
POST https://forgejo.phillippepelzer.me/api/v1/repos/FiLL/nomercy-runners/actions/runs/{returned_id}/cancel
```

Successful YAML/API preparation is not runtime evidence: verify native
start/finish identity and exact result in the durable controller ledger and
dashboard history for each actual run. Wait on events instead of repeatedly
requesting status, and do not cancel unrelated runs.

The dispatch inputs and endpoint follow the official
[Forgejo workflow reference](https://forgejo.org/docs/latest/user/actions/reference/#onworkflow_dispatch).
