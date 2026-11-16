# GitHub Listener and Worker startup

Every GitHub runtime sets `GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT=300`.
This is GitHub's supported maximum wait for its Listener to send the initial
job message. The official Worker independently waits only 30 seconds to
receive it: the setting alone does not fix slow Worker startup. Neither
startup timeout limits the job, its steps, or an already running process.

The runner loads its private `.env` after inheriting the service environment.
Creation therefore normalizes the same setting in `.env`, preserving other
entries. Existing registrations receive the same preparation without
changing their credential files or stopping a listener. A running Listener
reads the prepared setting on its next normal service start.

On 2026-10-11, run `38157335286` never reached a workflow step: its Listener
cancelled Worker startup after 30 seconds, stopped renewing the job, and
GitHub reported lost communication. This is separate from a workflow job
timeout. The replacement setting extended the Listener's timeout only.

On 2026-11-16, FFmpeg run `43776377514`, attempt 1, exposed the remaining
Worker timeout on Linux runners 4 and 9. Both Workers exited before receiving
their job; the Listener continued renewing the abandoned job. A controlled
IPC test reproduced this with the real upstream Worker, an inherited setting
of 300, and a 35-second delayed message. The corrected Worker received that
message and a 45-second delayed message.

`images/github-worker-startup/` rebuilds only `Runner.Worker.dll` from the
checksum-pinned v2.338.0 source and the exact original distribution's
dependencies. It applies the Listener's environment setting to the Worker's
receive timeout as well: default 30 seconds, clamped to 30..300. The build
refuses other source/binary versions. This is a local derivative, identified
by its image label, assembly informational version and build manifest.

The operator approved the Linux deployment on 2026-11-16. The install utility
replaces the DLL atomically, retaining the original for rollback. Linux keeps
the old inode mapped for existing Workers; only the next Worker uses the new
assembly. Registrations, listeners and current jobs are preserved. This was
verified with a mapped-file test on the Linux worker. Windows and macOS are
not patched by this Linux build.

Disk writeback and read scheduling on `rnr-linux-1` are described in
[the incident record](incidents/2026-11-16-linux-runner-startup.md). Those
changes reduce storage stalls; they do not increase the physical HDD array's
capacity or guarantee that external services cannot fail.

Self-hosted GitHub workflow jobs specify `timeout-minutes: 7200` for the full
five-day service allowance. Omitting this workflow setting selects GitHub's
six-hour default. Worker startup settings cannot change those GitHub limits.

Sources: [runner startup timeout](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/JobDispatcher.cs),
[Worker receive timeout](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Worker/Worker.cs),
[environment loading](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/Program.cs).
