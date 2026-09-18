"""One provisioning flow, for all six cells.

The nine steps of design section 12.4, written once. What changes from one cell
to another is which runtime adapter and which provider adapter are used, and
nothing else: both are looked up from the runner's (provider, platform) through
the service's tables. The agent and the scheduler are the same for every cell.
A test runs the flow against all six cells and asserts the identical sequence,
and another reads this file and fails on any branch that names a platform or a
forge.

The steps, in the order they run:

    1-2  collected and validated by RunnerService.plan()      (already done)
    3    place          pick a healthy worker of the right kind
    4    create_unit    the runtime creates the unit and its storage
    5    mint_token     the provider mints a short-lived registration token
    6    register       the agent registers the runner with that token
    7-8  verify_online  the agent says ready AND the forge shows it online
    9    on failure, the compensations of 12.5 run in reverse

The reconciler drives this in two of its passes: `provision` is steps 3-4 and
`register` is steps 5-8, so no single pass holds everything up for the minutes
a real registration takes. Each half compensates for its own failures before
returning, so a failure leaves nothing behind - which is NFR-8's "no half
instances", and T-0308 extends it to crashes.

**A crash leaves nothing behind (T-0308).** Every step that creates something
outside records its intent first, as design 12.5 lists: the worker before the
unit is built on it, and the forge's ids the moment the agent confirms them.
The unit's name is derived from the runner_id, so a controller that died after
creating it can find it again. Re-running `provision` after such a crash adopts
the unit rather than failing to create a second one with the same name, and a
runner left mid-creation past its deadline is swept by `abandon`, which runs
the same compensations the flow runs for an ordinary failure.

**The registration token goes nowhere but the agent.** It is not logged, not
put in an operation's trace or result, not stored on the spec, and every error
message is scrubbed of it by value before it leaves this module. A test mints a
sentinel token and searches every table and all captured output for it.
"""
from typing import Any, Callable, Mapping, Optional, Protocol

import providers

from . import retry
from .redact import redact
from .retry import (AGENT_FAST, AGENT_SLOW, FORGE_DELETE,
                    FORGE_REGISTRATION, FORGE_STATUS)

#: The flow's own steps, in order. Asserted by test.
STEPS = ("place", "create_unit", "mint_token", "register", "verify_online")

#: Design 12.5: what undoes the work up to and including a failed step, in the
#: order it must run. Every compensation is safe when there is nothing to undo
#: - that is why 12.5 insists on "safe if absent" - so the failed step is
#: compensated as well as the ones before it: a create that died half-way may
#: have left half a unit.
#:
#:   place          - nothing was made
#:   create_unit    - the unit, possibly partial
#:   mint_token     - a token is short-lived and needs no undoing; the unit does
#:   register       - the forge record, then the unit
#:   verify_online  - "as above": the same two
COMPENSATIONS = {
    "place": (),
    "create_unit": ("remove_unit",),
    "mint_token": ("remove_unit",),
    "register": ("deregister", "remove_unit"),
    "verify_online": ("deregister", "remove_unit"),
}

#: Which worker kind hosts which platform. macOS runs as an appliance inside a
#: Linux worker (16.4): there is no separate worker kind for it, on purpose.
WORKER_KIND = {
    providers.LINUX: "hyperv-linux",
    providers.WINDOWS: "hyperv-windows",
    providers.MACOS: "hyperv-linux",
}


class Agent(Protocol):
    """The platform-neutral channel to a worker. Phase 3 builds the real one."""

    def register(self, host_id: str, ref: Any, plan: Any) -> Mapping: ...
    def deregister(self, host_id: str, ref: Any) -> None: ...
    def drain(self, host_id: str, ref: Any) -> None: ...
    def cancel_drain(self, host_id: str, ref: Any) -> None: ...
    def ready(self, host_id: str, ref: Any) -> bool: ...


class Forges(Protocol):
    """The forge side: what it knows about runners, and deleting a record."""

    def records(self, provider: Any) -> Optional[list]: ...
    def delete(self, provider: Any, registration_id: str) -> bool: ...


class StepFailed(Exception):
    """A step failed and its compensations have run.

    Carries what was undone so the reconciler can clear the references to
    things that no longer exist - a failed runner pointing at a unit that was
    removed would send the next `remove` looking for it.

    Compensation failures are collected, not swallowed. A compensation that
    could not run is the one fact an operator most needs: it is where
    something was left behind.
    """

    def __init__(self, step, message, compensated=(), compensation_errors=()):
        self.step = step
        self.compensated = tuple(compensated)
        self.compensation_errors = tuple(compensation_errors)
        detail = f"{step}: {message}"
        if self.compensation_errors:
            detail += ("; could not undo: "
                       + "; ".join(self.compensation_errors))
        super().__init__(detail)

    @property
    def removed_unit(self):
        return "remove_unit" in self.compensated

    @property
    def deregistered(self):
        return "deregister" in self.compensated


class NoWorker(Exception):
    pass


class ProvisioningFlow:
    """The real executor behind the reconciler.

    `sleep` and `verify_timeout` are injectable so the wait for a runner to
    come online can be tested without waiting.
    """

    def __init__(self, service, agent: Agent, forges: Forges, env=None,
                 verify_timeout=120, verify_interval=5,
                 sleep: Optional[Callable[[float], None]] = None):
        self.service = service
        self.agent = agent
        self.forges = forges
        self.env = env or {}
        self.verify_timeout = verify_timeout
        self.verify_interval = verify_interval
        if sleep is None:
            import time
            sleep = time.sleep
        self.sleep = sleep
        #: What the flow did, in order, for the tests that assert the order.
        self.trail = []

    # ---- the two adapters, looked up per cell ------------------------------

    def _runtime(self, spec):
        return self.service.runtime_for(spec["provider"], spec["platform"])()

    def _provider(self, spec):
        return providers.by_key(spec["provider"])

    def _ref(self, spec, handle=None):
        from runtime.base import ExecUnitKind, ExecUnitRef
        from .service import EXEC_KINDS
        return ExecUnitRef(kind=ExecUnitKind(EXEC_KINDS[spec["platform"]]),
                           handle=handle or spec["exec_unit_ref"])

    # ---- steps 3-4 ----------------------------------------------------------

    def provision(self, spec, on_placed=None):
        """Place the runner and create its unit. Returns what the reconciler
        records: the unit's handle and the worker it is on.

        `on_placed` is called with the chosen worker before the unit is built
        on it - 12.5's "recorded before" for placement. Without it, a crash
        after the unit was created would leave the next attempt free to place
        the runner somewhere else, orphaning the unit on the first worker.
        """
        state = {"host_id": spec.get("host_id"), "ref": None}
        hooks = {"place": (lambda: on_placed(state["host_id"]))
                 if on_placed else None}
        return self._run(spec, ("place", "create_unit"), state,
                         lambda: {"exec_unit_ref": state["ref"].handle,
                                  "host_id": state["host_id"]}, hooks)

    def _step_place(self, spec, state):
        """Scheduler.place(): a healthy worker of the right kind, the least
        loaded first. A runner already placed keeps its worker, because its
        storage is there."""
        if state["host_id"]:
            return
        kind = WORKER_KIND[spec["platform"]]
        workers = self.service.inventory.healthy(kind=kind)
        if not workers:
            raise NoWorker(f"no healthy {kind} worker to place this runner on")
        load = {}
        for other in self.service.specs.list():
            if other.get("host_id"):
                load[other["host_id"]] = load.get(other["host_id"], 0) + 1
        state["host_id"] = min(
            workers, key=lambda w: (load.get(w["host_id"], 0),
                                    w["host_id"]))["host_id"]

    def _step_create_unit(self, spec, state):
        """Create the unit - or adopt it, if a previous attempt already did.

        The name comes from the runner_id, so a unit by that name IS this
        runner's. After a crash between creating it and recording it, the
        next attempt finds it here instead of failing on a name already in
        use, and the runner converges rather than being failed for a
        collision with itself.
        """
        from store import storage
        runtime = self._runtime(spec)
        name = storage.unit_name(spec["runner_id"])
        ref = self._ref(spec, handle=name)
        try:
            existing = retry.call(AGENT_FAST, runtime.status, ref)
        except Exception:               # noqa: BLE001 - unknown is "create"
            existing = None
        if existing is not None and getattr(existing, "exists", False):
            state["ref"] = ref
            return
        unit = dict(spec)
        unit["name"] = name
        unit["storage"] = storage.names(spec["runner_id"], spec["platform"])
        unit["host_id"] = state["host_id"]
        state["ref"] = retry.call(AGENT_SLOW, runtime.create, unit)

    # ---- steps 5-8 ----------------------------------------------------------

    def register(self, spec, on_registered=None):
        """Mint a token, register with it, and wait until the runner is up
        and the forge agrees. Returns the forge's identifiers for it.

        `on_registered` is called with those identifiers as soon as the agent
        confirms them, before the wait for the runner to come online - 12.5's
        "registration_id written with the agent's confirmation". A crash during
        the wait then leaves a runner whose forge record can be found and
        deleted by id, rather than one nobody can attribute.
        """
        state = {"host_id": spec.get("host_id"), "ref": self._ref(spec),
                 "plan": None, "registration": {}}
        hooks = {"register": (lambda: on_registered(
            dict(state["registration"]))) if on_registered else None}
        return self._run(spec, ("mint_token", "register", "verify_online"),
                         state, lambda: dict(state["registration"]), hooks)

    def _step_mint_token(self, spec, state):
        provider = self._provider(spec)

        def mint():
            # Raising inside the retried call is what lets a transient failure
            # be retried at all: `registration` reports failure as (None,
            # reason), which the retry loop would otherwise take as an answer.
            plan, error = provider.registration(spec, self.env)
            if plan is None:
                raise RuntimeError(error or "no registration plan")
            return plan

        plan = retry.call(FORGE_REGISTRATION, mint)
        state["plan"] = plan
        # Kept only so every later error message in this call can be scrubbed
        # of it by value. `state` is local to one call and dies with it.
        state["secret"] = plan.token

    def _step_register(self, spec, state):
        result = retry.call(AGENT_SLOW, self.agent.register,
                            state["host_id"], state["ref"],
                            state["plan"]) or {}
        state["registration"] = {
            "registration_id": str(result.get("registration_id") or ""),
            "registration_uuid": result.get("registration_uuid"),
        }
        # The plan has done its one job; nothing after this step needs it.
        state["plan"] = None

    def _step_verify_online(self, spec, state):
        """Ready means both: the agent says the process is up, AND the forge
        shows the runner online. Either alone has already been wrong on this
        fleet - the macOS runner's process was healthy for hours while the
        forge showed it offline."""
        provider = self._provider(spec)
        probe = dict(spec, **state["registration"])
        waited = 0
        while True:
            if self._ready(state["host_id"], state["ref"]):
                seen = provider.job_state(probe, self._records(provider))
                if seen in (providers.IDLE, providers.BUSY):
                    return
            if waited >= self.verify_timeout:
                raise TimeoutError(
                    f"not online within {self.verify_timeout}s: the agent "
                    f"and the forge did not both report it ready")
            self.sleep(self.verify_interval)
            waited += self.verify_interval

    def _ready(self, host_id, ref):
        """The agent's answer, or False when it could not give one. An agent
        that did not answer has not said the runner is ready."""
        try:
            return bool(retry.call(AGENT_FAST, self.agent.ready, host_id,
                                   ref))
        except Exception:               # noqa: BLE001
            return False

    def _records(self, provider):
        """The forge's runner list, or None - unknown - when it could not be
        read. Never the last list that did arrive: 17.2."""
        try:
            return retry.call(FORGE_STATUS, self.forges.records, provider)
        except Exception:               # noqa: BLE001
            return None

    # ---- running steps, and undoing them ------------------------------------

    def _run(self, spec, steps, state, result, hooks=None):
        hooks = hooks or {}
        for step in steps:
            self.trail.append(step)
            try:
                getattr(self, f"_step_{step}")(spec, state)
                if hooks.get(step):
                    # The record of what this step made, written before the
                    # next step starts. If this write fails, the step is
                    # treated as failed and undone: something made that
                    # nothing has recorded is the thing to avoid.
                    hooks[step]()
            except Exception as error:          # noqa: BLE001
                token = state.get("secret")
                compensated, errors = self._compensate(spec, step, state)
                raise StepFailed(step, redact(str(error), token),
                                 compensated,
                                 [redact(e, token) for e in errors]) \
                    from None
        return result()

    def _compensate(self, spec, failed_step, state):
        done, errors = [], []
        for action in COMPENSATIONS[failed_step]:
            self.trail.append(f"undo:{action}")
            try:
                getattr(self, f"_undo_{action}")(spec, state)
                done.append(action)
            except Exception as e:              # noqa: BLE001
                errors.append(f"{action}: {e}")
        return done, errors

    def _undo_remove_unit(self, spec, state):
        """Safe if there is no unit: that is the case 12.5 designs for.

        Falls back to the name derived from the runner_id, so a unit created
        by a call that failed or was cut off before returning its handle is
        still found and removed.
        """
        from store import storage
        ref = state.get("ref")
        if ref is None and spec.get("exec_unit_ref"):
            ref = self._ref(spec)
        if ref is None:
            ref = self._ref(spec, handle=storage.unit_name(spec["runner_id"]))
        retry.call(AGENT_SLOW, self._runtime(spec).remove, ref,
                   keep_data=False)

    def _undo_deregister(self, spec, state):
        """Safe if nothing was registered. A registration whose reply was lost
        cannot be found by id from here; the reconciler reports forge records
        with no spec rather than guessing (17.3)."""
        registration = state.get("registration") or {}
        if not registration.get("registration_id"):
            return
        self._deregister(dict(spec, **registration), state.get("ref"))

    # ---- the rest of the executor protocol ----------------------------------

    def _deregister(self, spec, ref=None):
        provider = self._provider(spec)
        plan = provider.deregistration(spec)
        if plan.via_api:
            # Forgejo: only the API can delete the record, and a removal that
            # cannot reach it must fail rather than strand the registration.
            if not retry.call(FORGE_DELETE, self.forges.delete, provider,
                              plan.registration_id):
                raise RuntimeError(
                    f"the forge did not delete registration "
                    f"{plan.registration_id}; refusing to continue rather "
                    f"than strand it")
        else:
            # GitHub: the runner deregisters itself when told to.
            retry.call(AGENT_SLOW, self.agent.deregister,
                       spec.get("host_id"), ref or self._ref(spec))

    def deregister(self, spec):
        self._deregister(spec)

    def abandon(self, spec):
        """Undo a creation that was interrupted and not finished in time.

        The sweep's half of 12.5: the same compensations the flow runs for an
        ordinary failure - deregister what was registered, then remove the
        unit - driven from what the spec recorded rather than from the state
        of a call that no longer exists. Returns what was done, and raises if
        any of it could not be, because a compensation that failed is where
        something was left behind.
        """
        state = {"ref": None,
                 "registration": {
                     "registration_id": spec.get("registration_id"),
                     "registration_uuid": spec.get("registration_uuid")}}
        if spec.get("exec_unit_ref"):
            state["ref"] = self._ref(spec)
        done, errors = [], []
        for action in COMPENSATIONS["verify_online"]:
            try:
                getattr(self, f"_undo_{action}")(spec, state)
                done.append(action)
            except Exception as e:          # noqa: BLE001
                errors.append(f"{action}: {e}")
        if errors:
            raise StepFailed("abandon", "could not finish undoing", done,
                             errors)
        return tuple(done)

    def observe(self, spec):
        """What the forge says, translated into the machine's words.

        Only for runners that are serving or draining; everything else is
        reported by the step that moved it. A draining runner the forge shows
        idle has finished its job, which is the observed `draining -> drained`
        edge. Unknown and offline are not observations: they say nothing new
        about the machine and must not be read as idle.
        """
        actual = spec["actual_state"]
        if actual not in ("idle", "busy", "draining", "registering"):
            return None
        provider = self._provider(spec)
        seen = provider.job_state(spec, self._records(provider))
        if actual == "registering":
            # A registration interrupted during the wait to come online. If
            # the forge now shows the runner and the agent says it is up, the
            # wait is over: that is the observed `registering -> idle` edge,
            # and the runner converges to healthy instead of being swept.
            if not spec.get("registration_id"):
                return None
            if seen not in (providers.IDLE, providers.BUSY):
                return None
            ready = self._ready(spec.get("host_id"), self._ref(spec))
            return "idle" if ready else None
        if actual == "draining":
            return "drained" if seen == providers.IDLE else None
        if seen == providers.BUSY:
            return "busy"
        if seen == providers.IDLE:
            return "idle"
        return None

    def start(self, spec):
        retry.call(AGENT_SLOW, self._runtime(spec).start, self._ref(spec))

    def stop(self, spec):
        retry.call(AGENT_SLOW, self._runtime(spec).stop, self._ref(spec))

    def drain(self, spec):
        retry.call(AGENT_FAST, self.agent.drain, spec.get("host_id"),
                   self._ref(spec))

    def cancel_drain(self, spec):
        retry.call(AGENT_FAST, self.agent.cancel_drain, spec.get("host_id"),
                   self._ref(spec))

    def remove(self, spec, keep_data=False):
        if not spec.get("exec_unit_ref"):
            return                      # nothing was ever created
        retry.call(AGENT_SLOW, self._runtime(spec).remove, self._ref(spec),
                   keep_data=keep_data)

    def clear_cache(self, spec):
        freed = retry.call(AGENT_SLOW, self._runtime(spec).clear_cache,
                           self._ref(spec), spec.get("cache_policy") or {})
        return {"total_bytes": getattr(freed, "total_bytes", 0),
                "measured": getattr(freed, "measured", False),
                "errors": dict(getattr(freed, "errors", {}) or {})}
