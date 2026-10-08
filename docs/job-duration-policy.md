# Job duration

Jobs may run for multiple days. Runner maintenance must drain them to completion.

## Forgejo

Linux, macOS, Windows x64 and Windows ARM64 use `2562047h47m16s` for both
`runner.timeout` and `runner.shutdown_timeout`. This is the largest whole-second
duration supported by Go, about 292 years, not a literal infinity setting.
Forgejo Runner v13.1.0 does not support disabling these deadlines: a zero job
timeout retains its three-hour default, and a zero shutdown timeout immediately
cancels running jobs during a drain.

The Forgejo server also needs `actions.ENDLESS_TASK_TIMEOUT=2562047h47m16s`.
Forgejo workflows should omit job and step `timeout-minutes` when no shorter
deadline is intended. Cancelling a workflow remains possible.

## GitHub

GitHub imposes a five-day execution limit on self-hosted jobs. It cannot be
disabled by changing a runner. Set `jobs.<job>.timeout-minutes: 7200` in each
workflow that should use that full allowance; omitting it defaults to six hours.
Remove shorter step deadlines as appropriate. A runner cannot override a shorter
deadline defined by a repository's workflow.

`GITHUB_TOKEN` expires after at most 24 hours. A multi-day build can keep running,
but later API or authenticated Git operations need suitable separate credentials.

References:

- https://docs.github.com/en/actions/reference/limits
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- https://code.forgejo.org/forgejo/runner/src/tag/v13.1.0/internal/pkg/config/config.go
- https://code.forgejo.org/forgejo/runner/src/tag/v13.1.0/internal/app/run/runner.go
- https://code.forgejo.org/forgejo/runner/src/tag/v13.1.0/internal/app/cmd/daemon.go
