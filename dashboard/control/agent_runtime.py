"""The controller's side of a worker: its runtime and its runner, over the agent.

Two adapters over one `AgentClient`, and nothing else between the controller
and a worker:

- `AgentRuntime` is a `RuntimeAdapter` (runtime/base.py). The service table
  names it for a cell whose units live on a worker; the flow and the service
  reads use it exactly as they use the Docker adapter, and every call becomes
  one of the closed verbs of design 13.1, addressed to the runner's own worker.
- `FlowAgent` is the provisioning flow's `Agent`: registering, deregistering,
  draining, and whether a unit is up.

**Where a unit lives is its spec's `host_id`, and nothing else.** A runtime is
bound to one worker when it is made (`for_runner`), from the spec the caller
holds. Nothing here chooses a worker or falls back to another one: a runner
with no worker has nothing out there to act on, and says so.

**What reaches a worker is what its verb takes.** A unit spec is built from
the controller's spec field by field - image, limits, the labels that find it
again - and nothing is passed through whole, because the agent refuses any
field it does not take, and a field it did take by accident would be a way to
ask it for something it was not designed to do. A registration plan goes
without `RegistrationPlan.extra`, for the same reason.

The runner_id a verb names comes from the unit's handle, `rnr-<runner_id>`,
which the agent made from it. A handle that is not one is refused here rather
than sent: the agent would refuse it too, and later.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

_HANDLE = re.compile(
    r"^rnr-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

#: How long an asynchronous verb may take before the controller stops
#: waiting. Longer than the agent's own slowest step: a remove waits out the
#: runner's stop grace, and a create may pull an image.
DEADLINE = 290


#: The controller's runtime table: every cell's units live on a worker and
#: are reached through its agent. `RunnerService.RUNTIMES` stays the table of
#: a dashboard that drives its own engine; this one is the controller's.
TABLE = {(provider, platform): "control.agent_runtime:AgentRuntime"
         for provider in ("github", "forgejo")
         for platform in ("linux", "windows", "macos")}


class NotBound(RuntimeError):
    """A runtime was asked for a runner it cannot reach."""


@dataclass
class AgentWiring:
    """What the controller reaches its workers with, set once at start.

    `images` names what each cell's units are made from - an image on a Linux
    worker, a template directory on a Windows or macOS one - keyed by
    (provider, platform). A cell with no entry uses its fleet's template.
    """

    client: Any
    operations: Any = None
    images: Mapping = field(default_factory=dict)
    #: The memory limit a cell's units get when their spec names none, keyed
    #: like `images`. A unit with no limit can take its whole worker down,
    #: agent and all, so a deployment names one for every cell it runs.
    memory: Mapping = field(default_factory=dict)
    deadline: float = DEADLINE

    def call(self, host_id, verb, body=None, operation_id=None):
        return self.client.call_and_wait(
            host_id, verb, body or {}, operation_id=operation_id,
            operations=self.operations if operation_id else None,
            deadline=self.deadline)


def runner_id_of(ref) -> str:
    """Which runner a unit reference belongs to.

    Carried by the reference when the controller made it. Falling back to
    reading it out of the handle is for references built from a name alone;
    it works only for units this controller named, and a runner adopted as
    it stood is not one of those.
    """
    carried = str(getattr(ref, "runner_id", "") or "")
    if carried:
        return carried
    handle = getattr(ref, "handle", ref)
    m = _HANDLE.match(str(handle or ""))
    if not m:
        raise NotBound(f"{handle!r} is not a unit this controller made and "
                       f"the reference carries no runner_id")
    return m.group(1)


def _wiring(service):
    wiring = getattr(service, "agents", None)
    if wiring is None:
        raise NotBound("this controller has no agent client; workers cannot "
                       "be reached")
    return wiring


class AgentRuntime:
    """A runtime adapter for units on a worker, through that worker's agent."""

    def __init__(self, wiring: AgentWiring, host_id: str):
        self.wiring = wiring
        self.host_id = host_id

    @classmethod
    def for_runner(cls, service, spec):
        """Bound to the worker the spec names. Called by the service with the
        spec it is acting on - the one place a runtime learns where it is."""
        host_id = (spec or {}).get("host_id")
        if not host_id:
            raise NotBound(f"runner {(spec or {}).get('runner_id')} has no "
                           f"worker yet")
        return cls(_wiring(service), host_id)

    def _call(self, verb, ref=None, **body):
        if ref is not None:
            body["runner_id"] = runner_id_of(ref)
        return self.wiring.call(self.host_id, verb, body) or {}

    # ---- lifecycle ---------------------------------------------------------

    def unit_spec(self, spec: Mapping[str, Any]) -> dict:
        """The agent's unit spec, built field by field (module docstring)."""
        if spec.get("adopt_unit"):
            # An adoption names what is already on the worker; there is no
            # image to make it from, and nothing to size (T-0802).
            return {"adopt": dict(spec["adopt_unit"])}
        image = (self.wiring.images.get((spec.get("provider"),
                                         spec.get("platform")))
                 or spec.get("runtime_template"))
        if not image:
            raise NotBound(f"nothing names what {spec.get('provider')}/"
                           f"{spec.get('platform')} units are made from")
        unit = {"image": str(image),
                "labels": {"nomercy.provider": str(spec.get("provider")),
                           "nomercy.fleet": str(spec.get("fleet_id") or "")}}
        if spec.get("cpu_limit") not in (None, "", "0"):
            unit["cpus"] = str(spec["cpu_limit"])
        if spec.get("memory_limit"):
            unit["memory"] = str(int(spec["memory_limit"]))
        else:
            default = self.wiring.memory.get((spec.get("provider"),
                                              spec.get("platform")))
            if default:
                unit["memory"] = str(default)
        return unit

    def create(self, spec: Mapping[str, Any]):
        from runtime.base import ExecUnitKind, ExecUnitRef
        from .service import EXEC_KINDS
        result = self.wiring.call(self.host_id, "exec_unit.create", {
            "runner_id": spec["runner_id"], "spec": self.unit_spec(spec)})
        handle = (result or {}).get("handle")
        if not handle:
            raise RuntimeError("the agent made the unit but did not name it")
        return ExecUnitRef(kind=ExecUnitKind(EXEC_KINDS[spec["platform"]]),
                           handle=handle, runner_id=spec["runner_id"])

    def start(self, ref) -> None:
        self._call("exec_unit.start", ref)

    def stop(self, ref, timeout: int = 60) -> None:
        self._call("exec_unit.stop", ref)

    def remove(self, ref, keep_data: bool = False) -> None:
        self._call("exec_unit.remove", ref, keep_data=bool(keep_data))

    # ---- reads -------------------------------------------------------------

    def status(self, ref):
        from runtime.base import ExecUnitStatus
        got = self._call("exec_unit.status", ref)
        if got.get("exists") is None:
            # Unknown is not absent: the flow would create a second unit.
            raise RuntimeError("the worker could not say whether the unit "
                               "exists")
        return ExecUnitStatus(exists=bool(got["exists"]),
                              running=bool(got.get("running")),
                              exit_code=got.get("exit_code"),
                              started_at=got.get("started_at"),
                              restart_count=int(got.get("restart_count")
                                                or 0),
                              message=str(got.get("state") or ""))

    def telemetry(self, ref):
        from runtime.base import Telemetry
        got = self._call("exec_unit.telemetry", ref)
        return Telemetry(**{k: got.get(k) for k in (
            "cpu_percent", "mem_used_bytes", "mem_limit_bytes")})

    def logs(self, ref, since_seconds: int = 45) -> str:
        got = self._call("exec_unit.logs", ref,
                         since_seconds=int(since_seconds))
        return str(got.get("text") or "")

    def exec_probe(self, ref, probe):
        from runtime.base import Probe, ProbeResult
        if not isinstance(probe, Probe):
            raise TypeError("a probe is a Probe member, never a string")
        got = self._call("exec_unit.probe", ref, probe=probe.value)
        return ProbeResult(probe=probe, ok=bool(got.get("ok")),
                           value=got.get("value"),
                           error=str(got.get("error") or ""))

    def clear_cache(self, ref, policy: Optional[Mapping[str, Any]] = None):
        """What was freed, as the agent measured it. Only the policy fields
        the verb takes are sent."""
        from runtime.base import Freed
        sent = {k: v for k, v in (policy or {}).items()
                if k in ("max_bytes", "scopes", "on_clear", "timeout")}
        got = self._call("exec_unit.clear_cache", ref, policy=sent)
        return Freed(per_scope=dict(got.get("per_scope") or {}),
                     errors=dict(got.get("errors") or {}),
                     total_bytes=int(got.get("total_bytes") or 0),
                     before=got.get("before"), after=got.get("after"),
                     measured=bool(got.get("measured")))

    def capabilities(self):
        from runtime.base import Capabilities, ExecUnitKind
        got = self.wiring.call(self.host_id, "capabilities")
        runtime = dict((got or {}).get("runtime") or {})
        return Capabilities(
            kind=ExecUnitKind(runtime.get("kind")),
            job_containers=bool(runtime.get("job_containers")),
            nested_builds=bool(runtime.get("nested_builds")),
            resettable_os=bool(runtime.get("resettable_os")),
            supports_drain=bool(runtime.get("supports_drain")),
            max_instances=runtime.get("max_instances"),
            notes=str(runtime.get("notes") or ""))


class FlowAgent:
    """The provisioning flow's `Agent`, over the same client."""

    #: What a registration plan carries to a worker - `_plan` in
    #: agent/verbs.py takes these and refuses anything else.
    PLAN_FIELDS = ("url", "token", "name", "labels", "runner_group")

    def __init__(self, wiring: AgentWiring):
        self.wiring = wiring

    def _call(self, verb, host_id, ref, **body):
        if not host_id:
            raise NotBound("the runner has no worker")
        body["runner_id"] = runner_id_of(ref)
        return self.wiring.call(host_id, verb, body) or {}

    def register(self, host_id, ref, plan):
        sent = {k: getattr(plan, k) or "" for k in self.PLAN_FIELDS}
        got = self._call("runner.register", host_id, ref, plan=sent)
        return {"registration_id": str(got.get("registration_id") or ""),
                "registration_uuid": got.get("registration_uuid")}

    def deregister(self, host_id, ref):
        self._call("runner.deregister", host_id, ref)

    def drain(self, host_id, ref):
        self._call("exec_unit.drain", host_id, ref)

    def cancel_drain(self, host_id, ref):
        self._call("exec_unit.cancel_drain", host_id, ref)

    def running(self, host_id, ref):
        """True or False as the worker saw it; raises when it could not
        tell, which proves nothing either way."""
        got = self._call("exec_unit.status", host_id, ref)
        if got.get("running") is None:
            raise RuntimeError("the worker could not say whether the unit "
                               "runs")
        return bool(got["running"])

    def ready(self, host_id, ref):
        return self.running(host_id, ref)
