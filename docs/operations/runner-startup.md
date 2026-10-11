# GitHub Listener and Worker startup

Every GitHub runtime sets `GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT=300`.
This is GitHub's supported maximum wait for its Listener to exchange the
initial job message with a newly started Worker. It does not limit the job,
its steps, or an already running process.

The runner loads its private `.env` after inheriting the service environment.
Creation therefore normalizes the same setting in `.env`, preserving other
entries. Existing registrations receive the same preparation without
changing their credential files or stopping a listener. A running Listener
reads the prepared setting on its next normal service start.

On 2026-10-11, run `38157335286` never reached a workflow step: its Listener
cancelled Worker startup after 30 seconds, stopped renewing the job, and
GitHub reported lost communication. This is separate from a workflow job
timeout. The replacement setting allows up to 300 seconds for startup;
successful execution still requires a live CI check after service recovery.

Self-hosted GitHub workflow jobs specify `timeout-minutes: 7200` for the full
five-day service allowance. Omitting this workflow setting selects GitHub's
six-hour default. Worker startup settings cannot change those GitHub limits.

Sources: [runner startup timeout](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/JobDispatcher.cs),
[environment loading](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/Program.cs).
