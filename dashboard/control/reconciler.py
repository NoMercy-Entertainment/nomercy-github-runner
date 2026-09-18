"""The reconciler: the only thing that makes intent true.

Each pass reads what should be (fleet capacities, desired states, open
operations), compares it with what is, and takes at most one step per runner
towards closing the gap. Then it stops. The next pass takes the next step.

**One step per runner per pass** keeps a pass short and makes it re-entrant. A
pass that tried to drive a runner all the way from planned to idle would hold
everything else up for minutes, and one interrupted half-way would leave no
record of how far it got. A single step is recorded before it is taken, so an
interruption leaves the runner in a transitional state the sweeper recognises.

**It is the only writer of `actual_state`.** The service records what is
wanted; this records what is. Nothing else may claim to know.

**It never overshoots.** When deciding whether a fleet needs another runner it
counts every runner that exists or is on its way - planned, provisioning,
registering - and every runner on its way out as well. Counting only healthy
ones would plan a replacement for each runner still booting, and the fleet
would grow by exactly the number that happened to be starting when the pass
ran. The cost is that a replacement waits until its predecessor is gone, which
dips capacity by one; the design accepts that dip and forbids the overshoot.

**It never aborts a job.** A runner that must go and is busy is drained first.
Stop, deregister and remove are never taken from `busy`, and the state machine
would refuse them if this module ever tried.

**It never acts destructively against a degraded worker.** Unreachable and
absent look the same from here, so a removal on that evidence would delete
capacity that was only out of touch. The step is held and reported instead.

**Two passes cannot run at once.** A lease in the database makes the second one
return having done nothing, rather than both concluding a fleet is short and
both planning the same missing runner.

The work itself is done by an executor passed in (T-0305 provides the real one,
the tests a fake). This module decides; the executor acts.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol

from store import schema
from store.specs import StaleSpec

from . import states
from .operations import OPEN
from .service import RunnerService

LEASE_NAME = "reconciler"
LEASE_SECONDS = 120

#: When scaling down, which runners go first. Cheapest to lose first: a
#: planned runner exists only on paper, a failed one is not serving anyway.
#: `busy` is last because it has to be drained before it can go at all.
VICTIM_ORDER = ("planned", "failed", "stopped", "drained", "idle",
                "provisioning", "provisioned", "registering", "starting",
                "stopping", "draining", "busy")

#: What "done" looks like for each verb's operation.
SERVING = frozenset({"idle", "busy"})
FULFILLED = {
    "start": SERVING, "cancel_drain": SERVING, "provision": SERVING,
    "register": SERVING, "repair": SERVING,
    "stop": frozenset({"stopped"}),
    "drain": frozenset({"drained"}),
    "remove": frozenset({"absent"}), "deregister": frozenset({"absent"}),
}

#: Steps that take capacity away or cannot be undone. Held for a degraded
#: worker.
DESTRUCTIVE = frozenset({"stop", "deregister", "remove"})


class Executor(Protocol):
    """What the reconciler needs done in the world. See T-0305."""

    def observe(self, spec: Mapping[str, Any]) -> Optional[str]: ...
    def provision(self, spec: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def register(self, spec: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def start(self, spec: Mapping[str, Any]) -> None: ...
    def stop(self, spec: Mapping[str, Any]) -> None: ...
    def drain(self, spec: Mapping[str, Any]) -> None: ...
    def cancel_drain(self, spec: Mapping[str, Any]) -> None: ...
    def deregister(self, spec: Mapping[str, Any]) -> None: ...
    def remove(self, spec: Mapping[str, Any], keep_data: bool) -> None: ...
    def clear_cache(self, spec: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass
class Report:
    """What one pass did. Empty `actions` is what converged looks like."""

    skipped: bool = False
    actions: list = field(default_factory=list)
    held: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def did(self, *what):
        self.actions.append(what)


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Reconciler:
    def __init__(self, service: RunnerService, executor: Executor,
                 holder: Optional[str] = None):
        self.service = service
        self.executor = executor
        self.holder = holder or f"reconciler-{uuid.uuid4()}"
        self.path = service.specs.path

    # ---- the lease ---------------------------------------------------------

    def _acquire(self, seconds=LEASE_SECONDS):
        """Take the pass lease, or report that someone else holds it.

        One transaction: insert if absent, then take it if it is ours already
        or has expired. An expired lease is taken over rather than waited on,
        because a pass that crashed must not stop every later one.
        """
        now = _now()
        with schema.connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO leases (name, holder, expires_at)"
                      " VALUES (?, ?, ?)",
                      (LEASE_NAME, self.holder,
                       _iso(now + timedelta(seconds=seconds))))
            c.execute("UPDATE leases SET holder = ?, expires_at = ?"
                      " WHERE name = ? AND (holder = ? OR expires_at < ?)",
                      (self.holder, _iso(now + timedelta(seconds=seconds)),
                       LEASE_NAME, self.holder, _iso(now)))
            row = c.execute("SELECT holder FROM leases WHERE name = ?",
                            (LEASE_NAME,)).fetchone()
        return row is not None and row["holder"] == self.holder

    def _release(self):
        with schema.connect(self.path) as c:
            c.execute("DELETE FROM leases WHERE name = ? AND holder = ?",
                      (LEASE_NAME, self.holder))

    # ---- a pass ------------------------------------------------------------

    def pass_once(self):
        report = Report()
        if not self._acquire():
            report.skipped = True
            return report
        try:
            for fleet in self.service.fleets.list():
                self._converge_capacity(fleet, report)
            for spec in self.service.specs.list():
                try:
                    self._step(spec, report)
                except StaleSpec:
                    # The service changed this runner while the pass was
                    # looking at it. Nothing is lost: the next pass reads the
                    # new intent. Optimistic concurrency paying for itself.
                    report.held.append((spec["runner_id"], "changed mid-pass"))
            return report
        finally:
            self._release()

    # ---- capacity ----------------------------------------------------------

    def _fleet_specs(self, fleet_id):
        return [s for s in self.service.specs.list(fleet_id=fleet_id)
                if s["actual_state"] != states.TERMINAL]

    def _converge_capacity(self, fleet, report):
        desired = fleet["desired_capacity"]
        present = self._fleet_specs(fleet["fleet_id"])

        # Everything present counts when deciding whether to ADD, including
        # runners on their way out. That is the no-overshoot rule.
        if len(present) < desired and fleet["available"]:
            missing = desired - len(present)
            operation_id = self.service.plan(fleet["fleet_id"], missing,
                                             requested_by="reconciler")
            report.did("plan", fleet["fleet_id"], missing, operation_id)
            return

        # Only runners still meant to stay count when deciding whether to
        # REMOVE, or every pass would mark another victim until the fleet
        # was empty.
        keepers = [s for s in present if s["desired_state"] != "absent"]
        excess = len(keepers) - desired
        if excess > 0:
            rank = {state: i for i, state in enumerate(VICTIM_ORDER)}
            victims = sorted(keepers,
                             key=lambda s: (rank.get(s["actual_state"], 99),
                                            s["created_at"]))[:excess]
            for victim in victims:
                self.service.specs.update(
                    victim["runner_id"], victim["spec_version"],
                    desired_state="absent")
                report.did("scale_down", fleet["fleet_id"],
                           victim["runner_id"])
            return

        self._close_capacity_operations(fleet, keepers, report)

    def _close_capacity_operations(self, fleet, keepers, report):
        """A capacity change is done when every runner meant to stay is
        serving, not when the reconciler first noticed it. A fleet with no
        worker to run on therefore keeps its operation open, which is true."""
        if not all(s["actual_state"] in states.LIVE for s in keepers):
            return
        for operation in self.service.operations.list(
                fleet_id=fleet["fleet_id"]):
            if operation["verb"] == "set_capacity" and \
                    operation["state"] in OPEN:
                self.service.operations.succeed(
                    operation["operation_id"],
                    {"capacity": fleet["desired_capacity"]})
                report.did("converged", fleet["fleet_id"])

    # ---- one runner --------------------------------------------------------

    def _operation(self, spec):
        if not spec["current_operation"]:
            return None
        operation = self.service.operations.get(spec["current_operation"])
        if operation and operation["state"] in OPEN:
            return operation
        return None

    def _step(self, spec, report):
        operation = self._operation(spec)

        # Close a finished operation before deciding anything. A runner that
        # has reached what its operation asked for is usually still observed
        # on every pass, so waiting for "no action" to close it would leave a
        # started runner's `start` open for ever.
        if operation and self._close_if_fulfilled(spec, operation, report):
            spec = self.service.specs.get(spec["runner_id"])
            operation = None

        progress = (self.service.operations.progress(operation["operation_id"])
                    if operation else {})
        action = decide(spec, operation, progress)
        if action is None:
            return

        if action in DESTRUCTIVE and not self._worker_accepts(spec):
            report.held.append((spec["runner_id"],
                                f"{action} held: worker "
                                f"{spec['host_id']} is not healthy"))
            return

        getattr(self, f"_do_{action}")(spec, operation, report)

    def _worker_accepts(self, spec):
        """Whether a destructive step may go to this runner's worker.

        A runner with no worker was never placed, so there is nothing out
        there to damage. One whose worker is not in the inventory at all is
        treated as unreachable: the conservative reading of "I cannot tell".
        """
        if not spec["host_id"]:
            return True
        try:
            return self.service.inventory.accepts_destructive_verbs(
                spec["host_id"])
        except Exception:
            return False

    # ---- writing what is ---------------------------------------------------

    def _move(self, spec, to, **extra):
        """Record a transition. The machine is consulted every time, so a bug
        here raises rather than writing a state the design does not have."""
        states.check(spec["actual_state"], to)
        self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                  actual_state=to, **extra)
        return self.service.specs.get(spec["runner_id"])

    def _fail(self, spec, operation, error, report):
        """Record a failed step on the runner and on its operation.

        A step failing from a state with no edge into `failed` - stopping,
        say - leaves the runner where it is with `last_error` set. It is then a
        transitional state past its deadline, which is exactly what the
        sweeper exists to find; forcing it into `failed` would write a
        transition the design does not have.
        """
        message = f"{_iso(_now())} {str(error)[:500]}"
        spec = self.service.specs.get(spec["runner_id"])
        extra = {"last_error": message}
        if operation:
            extra["current_operation"] = None
        if states.can(spec["actual_state"], "failed"):
            spec = self._move(spec, "failed", **extra)
        else:
            self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                      **extra)
        if operation:
            self.service.operations.fail(operation["operation_id"], error)
        report.errors.append((spec["runner_id"], str(error)))

    def _attempt(self, operation, note):
        if operation:
            self.service.operations.attempt(operation["operation_id"], note)

    # ---- the steps ---------------------------------------------------------
    #
    # Each writes the transitional state BEFORE calling the executor, so an
    # interruption between the two leaves a record the sweeper can find
    # (design 12.5). Each then writes the resting state the call produced.

    def _do_provision(self, spec, operation, report):
        self._attempt(operation, "provisioning")
        if spec["actual_state"] != "provisioning":
            spec = self._move(spec, "provisioning")
        try:
            result = self.executor.provision(spec) or {}
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"provision: {e}", report)
            return
        self._move(spec, "provisioned",
                   exec_unit_ref=result.get("exec_unit_ref"),
                   host_id=result.get("host_id") or spec["host_id"])
        report.did("provision", spec["runner_id"])

    def _do_register(self, spec, operation, report):
        self._attempt(operation, "registering")
        spec = self._move(spec, "registering")
        try:
            result = self.executor.register(spec) or {}
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"register: {e}", report)
            return
        self._move(spec, "idle",
                   registration_id=result.get("registration_id"),
                   registration_uuid=result.get("registration_uuid"))
        report.did("register", spec["runner_id"])

    def _do_start(self, spec, operation, report):
        self._attempt(operation, "starting")
        spec = self._move(spec, "starting")
        try:
            self.executor.start(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"start: {e}", report)
            return
        self._move(spec, "idle")
        self._advance(operation, "started")
        report.did("start", spec["runner_id"])

    def _do_stop(self, spec, operation, report):
        self._attempt(operation, "stopping")
        spec = self._move(spec, "stopping")
        try:
            self.executor.stop(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"stop: {e}", report)
            return
        self._move(spec, "stopped")
        self._advance(operation, "stopped")
        report.did("stop", spec["runner_id"])

    def _do_drain(self, spec, operation, report):
        """Draining ends when the job does, which is observed later. An idle
        runner has no job, so it is drained as soon as it stops accepting."""
        self._attempt(operation, "draining")
        was_idle = spec["actual_state"] == "idle"
        spec = self._move(spec, "draining")
        try:
            self.executor.drain(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"drain: {e}", report)
            return
        if was_idle:
            self._move(spec, "drained")
        report.did("drain", spec["runner_id"])

    def _do_cancel_drain(self, spec, operation, report):
        self._attempt(operation, "cancelling drain")
        try:
            self.executor.cancel_drain(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"cancel drain: {e}", report)
            return
        self._move(spec, "idle")
        report.did("cancel_drain", spec["runner_id"])

    def _do_withdraw(self, spec, operation, report):
        """A planned runner exists only on paper. Nothing to deregister,
        nothing to delete - so nothing to hold for a degraded worker either."""
        self._move(spec, "absent")
        self._finish(spec, operation, report)
        report.did("withdraw", spec["runner_id"])

    def _finish(self, spec, operation, report):
        """A runner has reached the end. Soft-delete it and close whatever
        asked for this.

        Done here rather than left to the next pass, because there is no next
        pass for this runner: a soft-deleted spec drops out of the listing the
        reconciler walks. The first version waited, and a `remove` an operator
        asked for stayed `running` for ever on a runner that was long gone.
        """
        self.service.specs.soft_delete(spec["runner_id"])
        if operation:
            self.service.operations.succeed(operation["operation_id"])
            report.did("closed", operation["operation_id"], operation["verb"])

    def _do_deregister(self, spec, operation, report):
        self._attempt(operation, "deregistering")
        spec = self._move(spec, "deregistering")
        try:
            self.executor.deregister(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"deregister: {e}", report)
            return
        self._move(spec, "removing")
        report.did("deregister", spec["runner_id"])

    def _do_remove(self, spec, operation, report):
        """The last step, and where recreate turns back.

        A recreate in flight keeps the storage and returns to provisioning
        under the same runner_id, which is what lets it find its storage by
        name. Anything else removes the storage too and ends at absent.
        """
        if spec["actual_state"] == "failed":
            spec = self._move(spec, "removing")
        recreating = bool(operation and operation["verb"] == "recreate")
        self._attempt(operation, "removing"
                      + (" (keeping storage)" if recreating else ""))
        try:
            self.executor.remove(spec, keep_data=recreating)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, f"remove: {e}", report)
            return
        if recreating:
            self._move(spec, "provisioning", exec_unit_ref=None,
                       registration_id=None, registration_uuid=None)
            self._advance(operation, "rebuilding")
        else:
            spec = self._move(spec, "absent")
            self._finish(spec, operation, report)
        report.did("remove", spec["runner_id"])

    def _do_repair(self, spec, operation, report):
        self._attempt(operation, "repairing")
        self._move(spec, "provisioning", last_error=None)
        report.did("repair", spec["runner_id"])

    def _do_clear_cache(self, spec, operation, report):
        """Guarded: only idle or drained. A runner that picked up a job since
        the request was made is not cleared - the request fails with a reason
        rather than waiting indefinitely or taking the cache out from under a
        build."""
        if spec["actual_state"] not in states.GUARDED["clear_cache"]:
            self.service.operations.fail(
                operation["operation_id"],
                f"the runner became {spec['actual_state']} before its cache "
                f"could be cleared")
            report.errors.append((spec["runner_id"], "busy at clear time"))
            return
        self._attempt(operation, "clearing cache")
        try:
            freed = self.executor.clear_cache(spec) or {}
        except Exception as e:                  # noqa: BLE001
            self.service.operations.fail(operation["operation_id"],
                                         f"clear cache: {e}")
            report.errors.append((spec["runner_id"], str(e)))
            return
        self.service.operations.succeed(operation["operation_id"], freed)
        self._clear_operation(spec)
        report.did("clear_cache", spec["runner_id"])

    def _do_observe(self, spec, operation, report):
        """Ask what the world looks like, and record it if it is news.

        Only observed edges are taken here. An executor reporting something the
        machine cannot reach from the current state is ignored and noted,
        because believing it would write a state the runner never passed
        through to get there.
        """
        try:
            seen = self.executor.observe(spec)
        except Exception:                       # noqa: BLE001
            return
        if not seen or seen == spec["actual_state"]:
            return
        edge = (spec["actual_state"], seen)
        if edge not in states.OBSERVED:
            report.held.append((spec["runner_id"],
                                f"ignored observation {edge}"))
            return
        self._move(spec, seen)
        report.did("observed", spec["runner_id"], seen)

    # ---- operations --------------------------------------------------------

    def _advance(self, operation, phase):
        if not operation:
            return
        progress = self.service.operations.progress(operation["operation_id"])
        done = list(progress.get("done", []))
        done.append(phase)
        self.service.operations.set_progress(operation["operation_id"],
                                             {"done": done})

    def _clear_operation(self, spec):
        spec = self.service.specs.get(spec["runner_id"])
        self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                  current_operation=None)

    def _close_if_fulfilled(self, spec, operation, report):
        """Close the operation if the runner has become what it asked for.
        Returns True when it closed one."""
        if not operation:
            return False
        verb = operation["verb"]
        actual = spec["actual_state"]
        done = self.service.operations.progress(
            operation["operation_id"]).get("done", [])

        # A runner that is `failed` has failed the operation in flight - unless
        # that operation is one that starts from `failed`. `repair`, and a
        # `remove` or `recreate` of a broken runner, would otherwise be failed
        # by the very state they were asked to fix, before taking a step.
        if actual == "failed" and not states.allows(verb, "failed"):
            self.service.operations.fail(operation["operation_id"],
                                         spec["last_error"] or "failed")
        elif verb == "restart" and "started" in done and actual in SERVING:
            self.service.operations.succeed(operation["operation_id"])
        elif verb == "recreate" and "rebuilding" in done \
                and actual in SERVING:
            self.service.operations.succeed(operation["operation_id"])
        elif verb in FULFILLED and actual in FULFILLED[verb]:
            self.service.operations.succeed(operation["operation_id"])
        else:
            return False
        if actual != states.TERMINAL:
            self._clear_operation(spec)
        report.did("closed", operation["operation_id"], verb)
        return True


def decide(spec, operation=None, progress=None):
    """The next step for one runner, or None when nothing needs doing.

    Pure: it reads a spec and an operation and returns a word. Everything that
    touches the world is in the Reconciler, so this - the part that decides
    what is safe - can be tested exhaustively without a database or a worker.
    """
    actual = spec["actual_state"]
    desired = spec["desired_state"]
    verb = operation["verb"] if operation else None
    done = (progress or {}).get("done", [])

    if actual == states.TERMINAL:
        return None

    # -- operations that are more than a desired state ----------------------
    if verb == "clear_cache":
        return "clear_cache"

    if verb == "restart" and "started" not in done:
        if actual == "busy":
            return "drain"                  # never abort the job
        if actual in ("idle", "drained") and "stopped" not in done:
            return "stop"
        if actual == "stopped":
            return "start"
        if actual in ("draining", "stopping", "starting"):
            return "observe"

    if verb == "recreate" and "rebuilding" not in done:
        if actual in ("idle", "busy"):
            return "drain"
        if actual in ("drained", "stopped"):
            return "deregister"
        if actual in ("removing", "failed"):
            return "remove"
        if actual in ("draining", "deregistering"):
            return "observe"

    if verb == "repair" and actual == "failed":
        return "repair"

    # -- a runner half-way through being created is finished first -----------
    # The machine has no way out of `provisioning` or `provisioned` except
    # forwards or into `failed`. So whatever the runner is now meant to be -
    # even absent - it has to become a runner first. Without this, a scale-down
    # that arrived between provisioning and registering left the runner at
    # `provisioned` for ever, observed on every pass and moved by none.
    if actual == "provisioning":
        return "provision"
    if actual == "provisioned":
        return "register"

    # -- desired state -------------------------------------------------------
    if desired == "absent":
        if actual == "planned":
            return "withdraw"
        if actual in ("idle", "busy"):
            return "drain"
        if actual in ("drained", "stopped"):
            return "deregister"
        if actual in ("removing", "failed"):
            return "remove"
        return "observe"

    if desired == "stopped":
        if actual == "busy":
            return "drain"
        if actual in ("idle", "drained"):
            return "stop"
        if actual in ("stopped", "planned", "failed"):
            return None
        return "observe"

    if desired == "drained":
        if actual in ("idle", "busy"):
            return "drain"
        if actual in ("drained", "planned", "stopped", "failed"):
            return None
        return "observe"

    # desired == running
    if actual == "planned":
        return "provision"
    if actual == "stopped":
        return "start"
    if actual == "drained":
        return "cancel_drain"
    if actual == "failed":
        # Not retried automatically. A failure that repeats would be retried
        # for ever, filling the trace and hiding the cause; a human `repair`
        # is one click and says someone looked.
        return None
    return "observe"
