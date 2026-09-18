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

from . import placement, retry
from .redact import redact
from .retry import (AGENT_FAST, AGENT_SLOW, FORGE_DELETE, FORGE_DRAIN,
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

#: Which worker kind hosts which platform; one table, in placement.py.
WORKER_KIND = placement.WORKER_KIND


class Agent(Protocol):
    """The platform-neutral channel to a worker. Phase 3 builds the real one."""

    def register(self, host_id: str, ref: Any, plan: Any) -> Mapping: ...
    def deregister(self, host_id: str, ref: Any) -> None: ...
    def drain(self, host_id: str, ref: Any) -> None: ...
    def cancel_drain(self, host_id: str, ref: Any) -> None: ...
    def ready(self, host_id: str, ref: Any) -> bool: ...
    #: Whether the unit's process is running at all. Raises when the agent
    #: cannot tell: a drain is proven by the process having gone, and an
    #: agent that did not answer has proven nothing.
    def running(self, host_id: str, ref: Any) -> bool: ...


class Forges(Protocol):
    """The forge side: what it knows about runners, and deleting a record."""

    def records(self, provider: Any) -> Optional[list]: ...
    def delete(self, provider: Any, registration_id: str) -> bool: ...
    def drain(self, provider: Any, spec: Mapping) -> None: ...
    def cancel_drain(self, provider: Any, spec: Mapping) -> None: ...


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
        #: runner_id -> what the forge last said of it in `observe`.
        self.forge_words = {}

    # ---- the two adapters, looked up per cell ------------------------------

    def _runtime(self, spec, host_id=None):
        """The cell's runtime, bound to the runner's worker - `host_id` when
        the caller knows it before the spec records it, as the steps of one
        pass do."""
        if host_id and not spec.get("host_id"):
            spec = dict(spec, host_id=host_id)
        return self.service.runtime(spec)

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

    def placement(self, spec):
        """(host_id, None) or (None, why): where this runner would go now.
        The reconciler asks before moving a runner out of `planned`, so one
        with nowhere to go waits there instead of failing (T-1502)."""
        kind = WORKER_KIND[spec["platform"]]
        workers = self.service.inventory.healthy(kind=kind)
        placed = [s for s in self.service.specs.list() if s.get("host_id")]
        return placement.choose(spec, workers, placed)

    def _step_place(self, spec, state):
        """Scheduler.place(): a healthy worker of the right kind and
        architecture with room, the least loaded first. A runner already
        placed keeps its worker, because its storage is there."""
        if state["host_id"]:
            return
        host_id, why = self.placement(spec)
        if host_id is None:
            raise NoWorker(why)
        state["host_id"] = host_id

    def _step_create_unit(self, spec, state):
        """Create the unit - or adopt it, if a previous attempt already did.

        The name comes from the runner_id, so a unit by that name IS this
        runner's. After a crash between creating it and recording it, the
        next attempt finds it here instead of failing on a name already in
        use, and the runner converges rather than being failed for a
        collision with itself.
        """
        from store import storage
        runtime = self._runtime(spec, state["host_id"])
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
        steps = ("mint_token", "register", "verify_online")
        still = self._still_registered(spec)
        if still is None:
            # Nothing was done, so nothing is compensated.
            raise StepFailed(
                "mint_token", "the forge could not be asked whether "
                f"registration {spec.get('registration_id')} still exists; "
                "not registering again blind - that would strand it")
        if still:
            # Only the incomplete steps (T-1303). A repair of a runner that
            # failed after it was registered finds its record still at the
            # forge; registering again would make a second record and strand
            # this one. So it only waits for it to come online.
            state["registration"] = {
                "registration_id": str(spec.get("registration_id") or ""),
                "registration_uuid": spec.get("registration_uuid")}
            steps = ("verify_online",)
        return self._run(spec, steps, state,
                         lambda: dict(state["registration"]), hooks)

    def _still_registered(self, spec):
        """Whether the registration this spec records is still a record at
        the forge: True, False, or None when the forge could not be asked.
        A spec that records no registration has none to find."""
        if not (spec.get("registration_id") or spec.get("registration_uuid")):
            return False
        provider = self._provider(spec)
        records = self._records(provider)
        if records is None:
            return None
        return provider.record_for(spec, records) is not None

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
        # of it by value. `state` is local to one call and dies with it; the
        # redaction registry keeps the value, never the plan, so a record
        # written elsewhere is masked too (T-1801).
        state["secret"] = plan.token
        from .redact import remember
        remember(plan.token)

    def _step_register(self, spec, state):
        result = retry.call(AGENT_SLOW, self.agent.register,
                            state["host_id"], state["ref"],
                            state["plan"]) or {}
        state["registration"] = {
            "registration_id": str(result.get("registration_id") or ""),
            "registration_uuid": result.get("registration_uuid"),
        }
        # What was asked for, kept to compare with what the forge then shows
        # (T-0902). Labels and group only - not the token.
        state["expect"] = {"labels": state["plan"].labels,
                           "runner_group": state["plan"].runner_group}
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
                records = self._records(provider)
                seen = provider.job_state(probe, records)
                if seen in (providers.IDLE, providers.BUSY):
                    # Online. Whether it registered with what the fleet
                    # asked for is a separate question, answered here and
                    # recorded rather than failed on: a runner with a wrong
                    # label still works, it just takes the wrong jobs, and
                    # that has to be visible (T-0902).
                    drift = provider.registration_drift(
                        state.get("expect") or {},
                        provider.record_for(probe, records))
                    if drift:
                        state["registration"]["drift"] = drift
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
        return self._undo(spec, COMPENSATIONS[failed_step], state)

    def _undo(self, spec, actions, state):
        """Run compensations in order. **A unit is never removed after its
        deregistration failed** (T-0903): the unit is where the runner's own
        credentials are, and with the record still at the forge, removing it
        is what strands a registration for good. Both are left, and the error
        says so - a half instance that can still be cleaned up, rather than
        an orphan that cannot."""
        done, errors = [], []
        for action in actions:
            if action == "remove_unit" and any(
                    e.startswith("deregister:") for e in errors):
                self.trail.append("skip:remove_unit")
                errors.append("remove_unit: not run - the forge record is "
                              "still there, and the unit is kept so it can "
                              "still be removed")
                continue
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
        retry.call(AGENT_SLOW, self._runtime(spec, state.get("host_id")).remove,
                   ref, keep_data=False)

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
            try:
                retry.call(AGENT_SLOW, self.agent.deregister,
                           spec.get("host_id"), ref or self._ref(spec))
                return
            except Exception as e:              # noqa: BLE001
                own = e
            self._delete_record_instead(provider, spec, plan, own)

    def _delete_record_instead(self, provider, spec, plan, cause):
        """A runner that could not deregister itself - its unit is gone, or
        its worker is not answering, or it holds no credential to do it with
        - still has a record the forge can delete by id, needing nothing from
        the unit. Done only when the forge does not show it running a job:
        deleting the record from under one would abort it (MIG-9)."""
        if not plan.registration_id:
            raise cause
        if provider.job_state(spec, self._records(provider)) == \
                providers.BUSY:
            raise RuntimeError(
                f"the runner could not deregister itself ({cause}) and the "
                f"forge shows it running a job; its record is not deleted "
                f"from under it")
        if not retry.call(FORGE_DELETE, self.forges.delete, provider,
                          plan.registration_id):
            raise RuntimeError(
                f"the runner could not deregister itself ({cause}) and the "
                f"forge did not delete registration {plan.registration_id}")

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
        done, errors = self._undo(spec, COMPENSATIONS["verify_online"], state)
        if errors:
            raise StepFailed("abandon", "could not finish undoing", done,
                             errors)
        return tuple(done)

    def observe(self, spec):
        """What the forge says, translated into the machine's words.

        Only for runners that are serving, draining or drained; everything
        else is reported by the step that moved it. A draining runner is
        drained once `_drained` can prove it. A drained runner the forge
        shows busy took a job after all - GitHub will still send one that
        asks only for labels a drain cannot take off - and goes back to
        draining, so nothing ends it under that job. Unknown and offline are
        not observations of a serving runner: they say nothing new about the
        machine and must not be read as idle.
        """
        actual = spec["actual_state"]
        if actual not in ("idle", "busy", "draining", "drained",
                          "registering"):
            return None
        provider = self._provider(spec)
        seen = provider.job_state(spec, self._records(provider))
        # What the forge said, kept for the reconciler to record as an
        # observation: readiness needs it, not only the machine (T-1803).
        self.forge_words[spec["runner_id"]] = seen
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
            return "drained" if self._drained(spec, provider, seen) else None
        if actual == "drained":
            return "draining" if seen == providers.BUSY else None
        if seen == providers.BUSY:
            return "busy"
        if seen == providers.IDLE:
            return "idle"
        return None

    def start(self, spec):
        """Started, and in service. The runtime undoes on the worker what a
        drain did there; a runner drained at its forge is put back there as
        well. A restart of a busy runner is drain, stop, start, and without
        this it would come back unable to take a job."""
        retry.call(AGENT_SLOW, self._runtime(spec).start, self._ref(spec))
        provider = self._provider(spec)
        if provider.drain_plan(spec).via_forge:
            retry.call(FORGE_DRAIN, self.forges.cancel_drain, provider, spec)

    def stop(self, spec):
        retry.call(AGENT_SLOW, self._runtime(spec).stop, self._ref(spec))

    # ---- drain (OPEN-7) -----------------------------------------------------

    def drain(self, spec):
        """Stop the runner taking jobs, where its provider says, and report
        whether it is drained already: True only when that is proven, so the
        reconciler moves it to `drained` on evidence and not because the
        machine last saw it idle. Asked again on every pass while it is
        draining, so every layer of it is idempotent.

        GitHub's runner cancels its job on SIGTERM, so it is drained at
        GitHub; forgejo-runner finishes its job on SIGTERM, so it is drained
        on its worker (`DrainPlan`).
        """
        provider = self._provider(spec)
        if provider.drain_plan(spec).via_forge:
            retry.call(FORGE_DRAIN, self.forges.drain, provider, spec)
        else:
            retry.call(AGENT_FAST, self.agent.drain, spec.get("host_id"),
                       self._ref(spec))
        return self._drained(spec, provider)

    def _drained(self, spec, provider, seen=None):
        """Whether the runner has no job and will take none - proven, never
        assumed. The forge must show it without a job: idle, or offline,
        which a runner that has exited is. Unknown proves nothing. A runner
        drained on its worker must also have stopped: its runner exits once
        its job is done, and a process still up may still be finishing one
        the forge has stopped reporting - offline is also what a network
        break mid-job looks like."""
        if seen is None:
            seen = provider.job_state(spec, self._records(provider))
        if seen not in (providers.IDLE, providers.OFFLINE):
            return False
        if provider.drain_plan(spec).via_forge:
            return True
        try:
            running = retry.call(AGENT_FAST, self.agent.running,
                                 spec.get("host_id"), self._ref(spec))
        except Exception:               # noqa: BLE001 - unknown is not down
            return False
        return running is False

    def cancel_drain(self, spec):
        """Back into service from `drained`, where its provider drained it:
        the forge gives it jobs again, or its unit is started again."""
        provider = self._provider(spec)
        if provider.drain_plan(spec).via_forge:
            retry.call(FORGE_DRAIN, self.forges.cancel_drain, provider, spec)
        else:
            retry.call(AGENT_SLOW, self.agent.cancel_drain,
                       spec.get("host_id"), self._ref(spec))

    def remove(self, spec, keep_data=False):
        if not spec.get("exec_unit_ref"):
            return                      # nothing was ever created
        retry.call(AGENT_SLOW, self._runtime(spec).remove, self._ref(spec),
                   keep_data=keep_data)

    def clear_cache(self, spec):
        """One clear, whatever the runtime (T-1602): what each scope freed,
        what each failing scope said, usage before and after, and whether
        any of it was measured. A scope that failed does not hide the ones
        that did not, and "could not measure" is never reported as zero."""
        freed = retry.call(AGENT_SLOW, self._runtime(spec).clear_cache,
                           self._ref(spec), spec.get("cache_policy") or {})
        get = (freed.get if isinstance(freed, dict)
               else lambda k, d=None: getattr(freed, k, d))
        errors = dict(get("errors", {}) or {})
        measured = bool(get("measured", False))
        return {"per_scope": dict(get("per_scope", {}) or {}),
                "errors": errors,
                "total_bytes": get("total_bytes", 0) if measured else None,
                "measured": measured,
                "before": get("before"), "after": get("after"),
                "partial": bool(errors)}
