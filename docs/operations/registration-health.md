# Registration health during maintenance

Online counts require a confirmed serving runner: a fresh running unit, a
reachable worker and fresh online registration evidence. Reachability alone
does not count an unknown, failed or draining runner as online. Totals and fleet
headings use the same card summary. Unknown registration evidence is included
in Attention and the card explains which forge confirmation is missing.

The controller owns a bounded registration-health observer on the existing
CONTROL_INTERVAL. It supplies shared provider snapshots in place of ordinary
per-pass reads, so a long drain/start/storage action cannot prevent other
runners from receiving current registration state and labels. Explicit fresh
lifecycle preflight remains separate. No worker/storage/action calls, job-name
queries or result polling run in that observer; native job events and SSE keep
their existing path.

Snapshots keep their original capture timestamp. API failures become unknown;
reusing a reply never renews freshness. Registration ID/UUID and capture-time
fences prevent old replies affecting a replacement or newer signed event. A
signed event identity also fences replies arriving within the same second.
Only the ordinary lifecycle writer changes actual_state; cards can project
fresh idle/busy registration state while that writer is occupied.

The observer starts and stops with the controller, shares provider read locks
with explicit fresh reads and satisfies the same cadence after such a read.
Shutdown wakes its cadence wait and joins within the existing 20-second forge
read deadline plus five seconds. None of these changes extend job limits or
weaken unknown/busy preflight for destructive operations.
