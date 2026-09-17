"""The contract every runtime adapter implements.

Abstract definitions and dataclasses only. This module imports nothing from
docker_ops, providers, app or any forge module, and a test asserts that: the
whole point of the seam is that a runtime knows nothing about which forge a
runner belongs to, and a provider knows nothing about how it is executed.

The verb set is closed. `exec_probe` takes a member of the `Probe` enum and
never a command string, which is the type-level form of the rule that the
control agent accepts only predefined operations and offers no endpoint for
arbitrary shell, PowerShell or SSH. A reviewer must reject any change that
adds a free-text member to `Probe` or a parameter that reaches a shell.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol


class ExecUnitKind(str, enum.Enum):
    """What a runner instance actually runs inside.

    A QEMU guest is never called a container. The vocabulary is part of the
    contract, because calling a VM a container is how a design starts claiming
    a uniformity it does not have.
    """

    LINUX_CONTAINER = "linux-container"
    WINDOWS_PROCESS = "windows-process"
    MACOS_APPLIANCE = "macos-appliance"


class Probe(enum.Enum):
    """The only questions a runtime may be asked about a running instance.

    Closed deliberately. Each member maps to a fixed, argument-free operation
    inside the adapter. There is no member that carries a command, a path or a
    format string, so the protocol cannot be widened into a remote shell by
    adding data.
    """

    DISK_USAGE = "disk_usage"
    JOB_STATE = "job_state"
    AGENT_VERSION = "agent_version"
    CACHE_SIZE = "cache_size"


@dataclass(frozen=True)
class ExecUnitRef:
    """An opaque handle to one execution unit.

    `handle` is adapter-private: a container id, a service name, a domain name.
    The controller stores it and hands it back, and never parses it. That is
    what keeps `RunnerSpec.exec_unit_ref` from quietly becoming a second
    identity alongside `runner_id`.
    """

    kind: ExecUnitKind
    handle: str


@dataclass(frozen=True)
class ExecUnitStatus:
    """What the runtime can say about an instance without asking the forge.

    `running` is about the execution unit. Whether the runner is usable also
    depends on the forge, and the controller combines the two; neither alone is
    enough. A healthy process with an unreachable forge is `unknown`, never
    `ready`.
    """

    exists: bool
    running: bool
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    restart_count: int = 0
    message: str = ""


@dataclass(frozen=True)
class Telemetry:
    """One sample, in bytes and percent-of-one-core.

    `cpu_percent` follows the convention `docker stats` uses: 100 means one
    core saturated, so 400 on a four-core allowance is full. `cpu_cores` is the
    denominator the instance is actually allowed, which is not always the
    host's core count - a cpuset narrows it and the host count would imply
    headroom the instance cannot reach.

    Every field is optional because "not measured" and "measured as zero" are
    different answers and must not be collapsed.
    """

    cpu_percent: Optional[float] = None
    cpu_cores: Optional[float] = None
    mem_used_bytes: Optional[int] = None
    mem_limit_bytes: Optional[int] = None
    disk_used_bytes: Optional[int] = None
    disk_total_bytes: Optional[int] = None
    cache_bytes: Optional[int] = None
    sampled_at: Optional[str] = None


@dataclass(frozen=True)
class ProbeResult:
    """The answer to one `Probe`, or an explicit failure to answer.

    `ok=False` means the probe could not be run. It is never rendered as a
    zero: a runner whose disk could not be measured is not a runner with an
    empty disk.
    """

    probe: Probe
    ok: bool
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class Freed:
    """What `clear_cache` actually reclaimed, per scope.

    Partial failure is normal and must be reported rather than hidden: a scope
    that could not be cleared appears in `errors` while the others still count
    toward `total_bytes`.

    `measured` is the other half of that honesty. A runtime that could not read
    its usage either side must say so, because "could not measure" and "0B" are
    not the same answer: reading a failed measurement as zero turns an unknown
    after-state into "everything was reclaimed" and reports the entire
    before-figure as freed.
    """

    per_scope: Mapping[str, int] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)
    total_bytes: int = 0

    #: Usage either side, in whatever units the runtime reports, for display.
    #: None when it could not be read.
    before: Optional[Mapping[str, str]] = None
    after: Optional[Mapping[str, str]] = None

    #: Whether `total_bytes` means anything. False and it must not be shown.
    measured: bool = False

    #: The clear was abandoned part-way and the unit may still be reclaiming on
    #: its own. A caller must not retry immediately, must not report a figure,
    #: and should tell the operator to look again shortly. A flag rather than a
    #: phrase in `errors`, so no caller has to match on message text to find
    #: out - that is the coupling this contract exists to remove.
    still_working: bool = False


@dataclass(frozen=True)
class Capabilities:
    """What this runtime can and cannot do, as data.

    The dashboard renders these rather than testing the platform, and the
    conformance suite asserts the correspondence in both directions: a runtime
    that cannot pass a scenario must declare the matching capability false, and
    a runtime that declares one true must pass it. That is what stops "uniform"
    from being true by definition.
    """

    kind: ExecUnitKind
    job_containers: bool = False
    nested_builds: bool = False
    resettable_os: bool = False
    supports_drain: bool = True
    max_instances: Optional[int] = None
    notes: str = ""


class RuntimeAdapter(Protocol):
    """The ten verbs. No more, and no verb that takes a command.

    Every method is idempotent on the instance it names, because the controller
    re-drives operations after a crash or a lost reply and must be able to do so
    safely. `create` on an instance that already exists returns the existing
    ref; `remove` on one that is already gone succeeds.
    """

    def create(self, spec: Mapping[str, Any]) -> ExecUnitRef:
        """Bring the execution unit and its isolated storage into being."""

    def start(self, ref: ExecUnitRef) -> None:
        ...

    def stop(self, ref: ExecUnitRef, timeout: int = 60) -> None:
        ...

    def remove(self, ref: ExecUnitRef, keep_data: bool = False) -> None:
        """Remove the unit. `keep_data` keeps the per-instance storage.

        The split exists because recreate and delete are different intents:
        recreate applies a settings change under the same identity and must not
        throw away the build cache, while a delete must not leave tens of GB
        behind under a name nothing will mount again.
        """

    def status(self, ref: ExecUnitRef) -> ExecUnitStatus:
        ...

    def telemetry(self, ref: ExecUnitRef) -> Telemetry:
        ...

    def logs(self, ref: ExecUnitRef, since_seconds: int = 45) -> str:
        ...

    def exec_probe(self, ref: ExecUnitRef, probe: Probe) -> ProbeResult:
        """Answer one closed question. Not a command channel."""

    def clear_cache(self, ref: ExecUnitRef,
                    policy: Mapping[str, Any]) -> Freed:
        """Reclaim only storage this instance provably owns."""

    def capabilities(self) -> Capabilities:
        ...


#: The verbs a runtime adapter exposes. Tests assert the Protocol carries
#: exactly these, so widening the contract is a deliberate, visible act.
RUNTIME_VERBS = (
    "create", "start", "stop", "remove", "status",
    "telemetry", "logs", "exec_probe", "clear_cache", "capabilities",
)
