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

from .agent_client import AgentError

_HANDLE = re.compile(
    r"^rnr-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

#: How long an asynchronous verb may take before the controller stops
#: waiting. Longer than the agent's own slowest step: a remove waits out the
#: runner's stop grace and then the engine's own removal, and a create
#: prepares a container's snapshot from an image of some size - measured at
#: 58 seconds on a quiet engine and three minutes on a busy one
#: (2026-09-20). Just inside the operation deadline of 17.2, so the
#: controller hears an answer rather than giving up on one.
DEADLINE = 1790


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
    #: RUNNER_TRUSTED_OWNERS: the accounts besides the org whose
    #: repositories a pull request may come from and still run on a GitHub
    #: runner (its job-started hook reads it). Given to every GitHub unit;
    #: see docs/operations/runner-job-hooks.md, "Outside code".
    trusted_owners: str = ""
    deadline: float = DEADLINE

    def call(self, host_id, verb, body=None, operation_id=None):
        return self.client.call_and_wait(
            host_id, verb, body or {}, operation_id=operation_id,
            operations=self.operations if operation_id else None,
            deadline=self.deadline)


def _names(text) -> str:
    """A list of names, split on commas and whitespace (a login has
    neither), as one comma-separated line: the agent refuses a value of
    more than one line, and the hooks split on commas."""
    return ",".join(name for name in re.split(r"[,\s]+", str(text or "")) if name)


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
        image = (spec.get("runtime_template")
                 or self.wiring.images.get((spec.get("provider"),
                                             spec.get("platform"))))
        if not image:
            raise NotBound(f"nothing names what {spec.get('provider')}/"
                           f"{spec.get('platform')} units are made from")
        unit = {"image": str(image),
                "labels": {"nomercy.provider": str(spec.get("provider")),
                           "nomercy.fleet": str(spec.get("fleet_id") or "")}}
        limit = str(spec.get("cpu_limit") or "").strip()
        if limit not in ("", "0"):
            # 11.1: adapter-interpreted. A window of cores - "0-15", or a
            # list - is a cpuset, which is the only thing that changes what
            # `nproc` reports inside the unit; a plain number is a quota,
            # which does not. This fleet is pinned to 16-core windows so a
            # build sees sixteen (2026-09-20).
            unit["cpuset" if ("-" in limit or "," in limit) else "cpus"] = limit
        if spec.get("memory_limit"):
            unit["memory"] = str(int(spec["memory_limit"]))
        else:
            default = self.wiring.memory.get((spec.get("provider"),
                                              spec.get("platform")))
            if default:
                unit["memory"] = str(default)
        if spec.get("platform") == "linux" and spec.get("memory_swap_limit"):
            unit["memory_swap"] = str(int(spec["memory_swap_limit"]))
        if spec.get("disk_limit"):
            unit["disk_limit"] = int(spec["disk_limit"])
        env = {}
        policy = spec.get("cache_policy") or {}
        if spec.get("platform") == "linux" and isinstance(policy, dict):
            if isinstance(policy.get("max_bytes"), int) and policy["max_bytes"] > 0:
                env["RUNNER_BUILD_CACHE_GC"] = f"{policy['max_bytes']}B"
            if isinstance(policy.get("scopes"), list):
                env["RUNNER_CLEANUP_SCOPES"] = ",".join(policy["scopes"])
            if policy.get("enabled") is False:
                env["RUNNER_CLEANUP_ENABLED"] = "0"
        trusted = _names(getattr(self.wiring, "trusted_owners", ""))
        if spec.get("provider") == "github" and trusted:
            # Read by the job-started hook, which only GitHub units have.
            env["RUNNER_TRUSTED_OWNERS"] = trusted
        if env:
            unit["env"] = env
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
        if not isinstance(got.get("exists"), bool):
            # Unknown is not absent: the flow would create a second unit.
            raise RuntimeError("the worker could not say whether the unit "
                               "exists")
        if got["exists"] and (not isinstance(got.get("running"), bool) or
                (got["running"] is False and got.get("state") not in
                 (None, "", "exited", "stopped", "absent"))):
            raise RuntimeError("the worker could not say whether the unit is running")
        return ExecUnitStatus(exists=got["exists"],
                              running=got.get("running") if got["exists"] else False,
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
    #: agent/verbs.py takes these and refuses anything else. `replace` is
    #: apart from the rest because it is a flag: sent as text it would be
    #: true whatever it said, and the agent refuses anything but a boolean.
    PLAN_FIELDS = ("url", "token", "name", "labels", "runner_group")
    PLAN_FLAGS = ("replace",)

    def __init__(self, wiring: AgentWiring):
        self.wiring = wiring

    def _call(self, verb, host_id, ref, **body):
        if not host_id:
            raise NotBound("the runner has no worker")
        body["runner_id"] = runner_id_of(ref)
        return self.wiring.call(host_id, verb, body) or {}

    def register(self, host_id, ref, plan):
        sent = {k: getattr(plan, k) or "" for k in self.PLAN_FIELDS}
        sent.update({k: bool(getattr(plan, k, False))
                     for k in self.PLAN_FLAGS})
        try:
            got = self._call("runner.register", host_id, ref, plan=sent)
        except AgentError as refused:
            if not self._refused_flags(refused):
                raise
            # A worker that has not been upgraded yet does not know this
            # field and refuses the whole plan for carrying it. The fleet is
            # deployed a worker at a time, so that worker's runner would be
            # unregisterable until somebody got to it - which on 2026-09-20
            # was a Forgejo runner whose unit had already been removed for
            # its rebuild. It registers without the flags instead.
            got = self._call("runner.register", host_id, ref,
                             plan={k: v for k, v in sent.items()
                                   if k not in self.PLAN_FLAGS})
        return {"registration_id": str(got.get("registration_id") or ""),
                "registration_uuid": got.get("registration_uuid")}

    def _refused_flags(self, refused):
        """Whether an agent refused this plan only for the flags it carries:
        it names the fields it does not take, and they are all ours."""
        if getattr(refused, "status", None) != 400:
            return False
        named = re.findall(r"['\"]([A-Za-z_]+)['\"]", str(refused))
        return bool(named) and set(named) <= set(self.PLAN_FLAGS)

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
        if got.get("running") is False and got.get("state") not in (
                "exited", "stopped", "absent"):
            raise RuntimeError("the worker did not confirm a stopped execution unit")
        return bool(got["running"])

    def ready(self, host_id, ref):
        return self.running(host_id, ref)
