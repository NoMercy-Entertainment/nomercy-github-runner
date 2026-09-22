# macOS runner resource settings

CPU and RAM defaults are editable only while a healthy, current worker of the
fleet's runtime and architecture advertises all three capabilities as boolean
true: `appliance_per_runner`, `cpu_enforcement`, and `memory_enforcement`.
The same predicate governs settings validation and worker selection. Flags
from separate workers are never combined. Stale, unknown or degraded workers
and workers with invalid measured capacity cannot enable the controls.

The current appliance pool accepts 1–64 whole CPU cores and 4–128 whole GiB of
guest RAM. CPU defaults are stored as integer text, matching the runtime's
input contract. Placement additionally reserves the advertised per-runner
appliance overhead (currently 2 GiB). New defaults apply to newly created or
recreated runners; they do not change existing guests in place.

Legacy shared guests leave CPU/RAM inputs disabled. Disabled fields are
omitted when other settings are saved; existing values are not silently
cleared. The API checks current worker evidence again when a save arrives.

The current pool reports a fixed 256 GiB guest disk and `disk_quota=false`.
Settings show that size read-only and reject configurable `disk_limit`
requests. A fixed appliance disk is not advertised as a configurable quota.

Runner detail confirms current enforcement only while the assigned worker is
healthy and its latest per-instance observation proves it. The pool inspects
the actual owned appliance configuration (CPU, guest RAM and outer memory
limits), includes `resource_enforcement` in status and heartbeat, and inventory
stores that observation outside spec intent/versioning. Missing or invalid
proof clears earlier proof; observations expire after 30 seconds even while
minimal worker heartbeats continue. Another worker's report cannot update the
runner. Saved spec capabilities and fleet defaults are never instance proof.
Detail displays observed CPU/RAM values, including for inspected stopped
appliances, and does not infer current guest disk size from worker defaults.
The fleet dashboard does not display a workers/memory-budget information block.
