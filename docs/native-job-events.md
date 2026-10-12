Job changes use a common bounded JSON schema and a private durable outbox.
The heartbeat reads an in-memory job cache while the event service is active;
it never tails a job log or opens a periodic guest SSH connection for job names.
Linux uses inotify, Windows uses native change-notification handles, and macOS
uses a persistent host-key-pinned SSH stream backed by guest kqueue. Watches
are registered before the bounded initial replay. Delivery retries after a
failed mTLS send use backoff; they do not query the job or forge for its result.
The dashboard projects the same durable ledger into history with a committed
cursor. Workers advertise job_events=1 only while their native event service
is active. History then uses received samples and database-change notifications
instead of requesting runner logs every five seconds; new native runs do not
enter the old result-enrichment API loop. Older workers retain their fallback
until upgraded, and preexisting historical rows can still be enriched.

GitHub's installed start/completion hooks emit observations with a null result.
Each pair has a bounded opaque hook identity in its own .active-job file, so
sequential matrix jobs with the same GITHUB_JOB slug remain distinct. History
binds that identity to a signed numeric job ID using separate ID aliases;
different known IDs are never merged merely because their names match.
Its signed workflow_job webhook supplies the authoritative result. The minimal
Forgejo v13.1.0 patch emits from NewReporter and Close with the reporter's exact
result. These remain worker observations and never grant themselves the right
to update authoritative forge state or release a drain. Payloads, secrets,
outputs, job logs and arbitrary environment variables are excluded.
The Forgejo finish is emitted immediately after its reporter establishes the
local result, before final network upload retries. A server cancellation can
race that upload, so the local result is explicitly worker evidence; only a
signed forge event can replace authoritative forge state or a signed result.

Rollout, owned by the operator:

1. Build `images/windows/build-forgejo-runner.sh v13.1.0` in the pinned
   golang:1.26.7 container. Mount images/windows at /build, images/forgejo-events
   at /events, both read-only, and a private output directory at /out. Allow
   executable temporary storage for Go tests. Preserve PROVENANCE and SHA256SUMS.
2. Before an idle runner is resumed, install its patched Forgejo binary or its
   GitHub hook set, and set NOMERCY_RUNNER_ID to that owned runner UUID and
   NOMERCY_JOB_EVENT_DIR to its own logs/job-events directory. Preserve all
   registration files. Existing services require their definitions refreshed;
   a management-agent restart alone does not update inherited environments.
   For storage-backed Windows services use `_unit(..., proven_disk)` to put
   the validated Volume{GUID} root in that environment. The legitimate NTFS
   mountpoint at D:\runners\UUID is never a generic privileged reparse exemption.
3. On each macOS guest, install agent/job_event_stream.py at
   /Library/NoMercy/job-events/job_event_stream.py, root-owned0644 in protected
   root-owned0755 directories. Create that runner's owned spool. Provision its
   hypervisor-verified host-key pin at pool-instance/guest_known_hosts, root0600,
   containing the exact [127.0.0.1]:owned-port host-key mapping. A missing or
   changed pin is a failed source, never an automatic rotation or insecure SSH.
4. Install the agent modules and restart only its management service. Preserve
   the outbox across deployment: it lives beside the TLS certificate under
   job-events/events.sqlite. A failed native source remains unknown; it does not
   fall back to log polling. Source handles detach before storage maintenance.
   Install the dashboard/control modules together with native_history.py so
   the job_events capability disables the legacy history-log collector too.
5. Run real short jobs for both providers on Linux, Windows x64/ARM64 and macOS.
   Verify start/finish, exact Forgejo success/failure/cancellation, signed GitHub
   results, durable replay after sender restart and no per-heartbeat log reads.
   Keep provider/registration IDs and resource profiles unchanged.

The producer spool bounds 256 files of at most4096bytes. If its consumer is down
long enough to fill it, the producer warns instead of blocking the workflow.
This is observable degraded operation, not a guarantee of unlimited replay.
The private outbox bounds10000 pending events and retains10000 delivered event
identities. A conflicting reuse of an event ID is refused. Worker timestamps
more than30seconds ahead of receive time remain evidence but cannot freeze
the display cache; signed events are ranked independently for forge authority.

This change does not claim to remove resource-sampling heartbeats, VM boot
readiness checks or required fresh provider preflight before destructive actions.
