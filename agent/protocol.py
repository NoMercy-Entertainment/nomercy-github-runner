"""The words the controller and the agent share, and nothing else.

Both sides need the same verb names, the same probe names and the same idea of
which protocol they speak. Each deployable carries its own copy of those words
- the agent cannot import the dashboard, and the dashboard image does not ship
the agent - so the controller keeps a copy in `control/agent_client.py`, and a
dashboard test parses this file and fails on any difference. Two copies with a
check are the price of two deployables; two copies without one would be how
they drift.
"""

#: Major version only, carried as X-Protocol-Version on every request. A
#: minor change must stay compatible; anything that is not is a new major, and
#: a side that meets a major it does not implement says so rather than
#: guessing (design 13.5, T-0406).
PROTOCOL_MAJOR = 1

#: Every verb the controller may ask an agent for, design 13.1. Closed: a new
#: capability is a new name here, reviewed, never a parameter on an old one.
#: `heartbeat` and `event` are not here - they go the other way, agent to
#: controller, and the agent does not answer them.
VERB_NAMES = frozenset({
    "hello",
    "capabilities",
    "exec_unit.create",
    "exec_unit.start",
    "exec_unit.stop",
    "exec_unit.restart",
    "exec_unit.remove",
    "exec_unit.status",
    "exec_unit.telemetry",
    "exec_unit.logs",
    "exec_unit.probe",
    "exec_unit.clear_cache",
    "runner.register",
    "runner.deregister",
})

#: The questions `exec_unit.probe` can ask. Mirrors `runtime.base.Probe` in the
#: dashboard, checked by test. A string on the wire, a closed set on arrival:
#: a probe that is not one of these is refused before anything runs.
PROBES = frozenset({"disk_usage", "job_state", "agent_version", "cache_size"})

#: The scopes `exec_unit.clear_cache` may name, design 15.2. Each is data a
#: runner provably owns; there is no scope for a shared host location.
CACHE_SCOPES = frozenset({"engine-build-cache", "engine-images-unused",
                          "workspace", "toolcache", "temp"})

#: Paths. The verb is in the path, not the body, so an unknown verb is refused
#: before a byte of the body is read (T-0401).
OP_PATH = "/v1/op/"
