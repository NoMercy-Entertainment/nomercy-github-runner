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
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol

from store import schema
from store.specs import StaleSpec

from . import states
from .operations import OPEN
from .service import RunnerService, forget_adoption

LEASE_NAME = "reconciler"
LEASE_SECONDS = 120
LEASE_RENEW_SECONDS = 30

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
DESTRUCTIVE = states.ENDS_WORK

#: Every step that must not be sent to a worker the controller does not trust.
#: The destructive ones, plus two whose FAILURE is destructive: a provision or
#: registration that fails runs the flow's compensations, and those remove the
#: unit. Letting them run against an unreachable worker would dispatch a
#: removal there by the back door - which the design forbids (17.3) and which a
#: mutation test of T-0308 turned up. Plus clear_cache, which deletes data.
#: A runner not yet placed is exempt: placement only ever picks a healthy one.
NEEDS_HEALTHY_WORKER = DESTRUCTIVE | {"provision", "register", "clear_cache", "start", "cancel_drain"}


class Executor(Protocol):
    """What the reconciler needs done in the world. See T-0305."""

    def observe(self, spec: Mapping[str, Any]) -> Optional[str]: ...
    def provision(self, spec: Mapping[str, Any],
                  on_placed=None) -> Mapping[str, Any]: ...
    def register(self, spec: Mapping[str, Any],
                 on_registered=None) -> Mapping[str, Any]: ...
    def abandon(self, spec: Mapping[str, Any]) -> tuple: ...
    def start(self, spec: Mapping[str, Any]) -> None: ...
    def stop(self, spec: Mapping[str, Any]) -> None: ...
    #: True when the runner is proven drained - no job, and none to come.
    def drain(self, spec: Mapping[str, Any]) -> bool: ...
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
        self._pass_lock = threading.Lock()
        self._lease_lost = threading.Event()

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

    def _renew(self):
        with schema.connect(self.path) as c:
            cursor = c.execute("UPDATE leases SET expires_at = ? WHERE name = ? AND holder = ?",
                               (_iso(_now() + timedelta(seconds=LEASE_SECONDS)),
                                LEASE_NAME, self.holder))
        return cursor.rowcount == 1

    def _keep_lease(self, stopped):
        while not stopped.wait(LEASE_RENEW_SECONDS):
            try:
                if self._renew():
                    continue
            except Exception:
                pass
            self._lease_lost.set()
            return

    def _lock_file(self):
        """OS lock fences live processes even when a paused lease expires."""
        stream = open(os.fspath(self.path) + ".reconciler.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return None
        return stream

    # ---- a pass ------------------------------------------------------------

    def _maintenance_holds(self, report):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='maintenance'").fetchone()
        held = bool(row and str(row[0]).lower() in ("true", "1"))
        if held and ("platform", "maintenance") not in report.held:
            report.held.append(("platform", "maintenance"))
        return held

    def pass_once(self):
        report = Report()
        if self._maintenance_holds(report):
            return report
        if not self._pass_lock.acquire(blocking=False):
            report.skipped = True
            return report
        lock_file = None
        heartbeat = None
        stopped = threading.Event()
        try:
            lock_file = self._lock_file()
            if lock_file is None or not self._acquire():
                report.skipped = True
                return report
            self._lease_lost.clear()
            heartbeat = threading.Thread(target=self._keep_lease, args=(stopped,), daemon=True)
            heartbeat.start()
            # One reading of each forge serves every runner this pass looks
            # at; asking per runner spent a token's hourly budget in minutes
            # and left every card reading `unknown`.
            begin = getattr(self.executor, "begin_pass", None)
            if begin:
                begin()
            self._sweep(report)
            for fleet in self.service.fleets.list():
                if self._lease_lost.is_set() or self._maintenance_holds(report):
                    break
                try:
                    self._converge_capacity(fleet, report)
                except Exception as error:
                    report.errors.append((fleet["fleet_id"], str(error)))
            for spec in self.service.specs.list():
                if self._maintenance_holds(report):
                    break
                if self._lease_lost.is_set():
                    report.errors.append((LEASE_NAME, "lease renewal failed; pass stopped"))
                    break
                try:
                    self._step(spec, report)
                except StaleSpec:
                    # The service changed this runner while the pass was
                    # looking at it. Nothing is lost: the next pass reads the
                    # new intent. Optimistic concurrency paying for itself.
                    report.held.append((spec["runner_id"], "changed mid-pass"))
                except Exception as error:
                    report.errors.append((spec["runner_id"], str(error)))
            return report
        finally:
            stopped.set()
            try:
                if heartbeat:
                    heartbeat.join()
                    self._release()
            finally:
                if lock_file:
                    lock_file.close()
                self._pass_lock.release()

    # ---- the sweep ---------------------------------------------------------

    #: States a runner is in while it is being created. A runner left in one
    #: of these past its operation's deadline was interrupted, and nothing but
    #: the sweep will ever move it: the machine's only ways out are forwards,
    #: which a crashed call cannot take, or into `failed`.
    CREATING = frozenset({"provisioning", "registering"})

    def _sweep(self, report):
        """Undo creations that were interrupted and not finished in time.

        Design 12.5: the reconciler sweeps for specs stuck in a transitional
        state past their deadline and runs the same guarded compensations.
        A registration that became healthy is recovered; busy or uncertain
        work is held with its identity and storage intact. Safe cleanup ends
        `failed` and still counts against capacity, preventing overshoot.

        Only creation is swept. A runner that is draining may be waiting on a
        job for hours and is never touched here; the others re-drive
        themselves on every pass.
        """
        for operation in self.service.operations.overdue():
            if self._maintenance_holds(report):
                break
            runner_id = operation["runner_id"]
            if not runner_id:
                continue
            spec = self.service.specs.get(runner_id)
            if not spec or spec["current_operation"] != \
                    operation["operation_id"]:
                continue
            if spec["actual_state"] not in self.CREATING:
                continue
            if (spec["actual_state"] == "provisioning" and
                    self.service.operations.progress(operation["operation_id"]).get("rollback_retry_preflight")):
                # Nothing was changed: re-observe through normal provisioning
                # rather than turning a temporary read failure into removal.
                continue
            if not self._worker_accepts(spec):
                report.held.append((runner_id, "sweep held: worker "
                                    f"{spec['host_id']} is not healthy"))
                continue
            if spec["actual_state"] == "registering":
                self._do_observe(spec, None, report, fresh=True)
                spec = self.service.specs.get(runner_id)
                if spec["actual_state"] not in self.CREATING:
                    continue
            was = spec["actual_state"]
            try:
                undone = self.executor.abandon(spec)
            except Exception as e:          # noqa: BLE001
                self._fail(spec, operation, e, report,
                           f"interrupted while {was}; sweep")
                continue
            self._fail(spec, operation, _Abandoned(was, undone), report)
            report.did("swept", runner_id, was)

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
        if operation is None and spec.get("current_operation"):
            # Completing an operation and clearing its spec pointer are two
            # writes. Resume safely after a crash between those writes.
            finished = self.service.operations.get(spec["current_operation"])
            if finished and finished["state"] not in OPEN:
                self._clear_operation(spec)
                spec = self.service.specs.get(spec["runner_id"])
        if operation is None and spec["actual_state"] in ("removing", "deregistering") and spec["desired_state"] != "absent":
            if spec.get("last_error"):
                report.held.append((spec["runner_id"], "failed recreate held: explicitly retry after resolving its error"))
                return
            # Interrupted work without an error resumes through the same
            # replacement preflight and rolling gate as an explicit request.
            # That preflight (`_act`'s own `validate_replacement`, on the
            # same `replacement_spec` -> `_cpu_window`/`_host_cores` ->
            # `cpusets.allocate` machinery guarded at the other call sites
            # below) can refuse - a pinned width that no longer fits this
            # host, say - and `service.recreate` raises rather than
            # swallowing it. Unguarded, that `Refused` would reach
            # `pass_once`'s generic handler on every single pass forever:
            # `last_error` never set, `current_operation` never set, so this
            # same branch re-enters and re-raises identically next time -
            # finding 2's sibling, at the recovery path rather than the
            # explicit one (2026-09-23).
            try:
                self.service.recreate(spec["runner_id"], requested_by="reconciler")
            except Exception as error:          # noqa: BLE001
                self._fail(spec, operation, error, report, "recreate resume")
                return
            spec = self.service.specs.get(spec["runner_id"])
            operation = self._operation(spec)

        if operation and operation["verb"] == "recreate" and not self._recreate_turn(spec, operation, report):
            return
        if operation and operation["verb"] == "clear_cache" and not self._cache_turn(spec, operation, report):
            return

        # Close a finished operation before deciding anything. A runner that
        # has reached what its operation asked for is usually still observed
        # on every pass, so waiting for "no action" to close it would leave a
        # started runner's `start` open for ever.
        if operation and self._close_if_fulfilled(spec, operation, report):
            spec = self.service.specs.get(spec["runner_id"])
            operation = None

        progress = (self.service.operations.progress(operation["operation_id"])
                    if operation else {})
        if (progress.get("rollback_held") and not progress.get("rollback_retry_preflight")
                and spec["actual_state"] == "provisioning"):
            report.held.append((spec["runner_id"], progress["rollback_held"]))
            return
        if operation and operation["verb"] == "clear_cache" and spec["actual_state"] in SERVING:
            observe = getattr(self.executor, "observe_fresh", self.executor.observe)
            try:
                seen = observe(spec)
            except Exception:
                seen = None
            if seen in SERVING and seen != spec["actual_state"]:
                spec = self._move(spec, seen)
            if seen != "idle" and (spec.get("cache_policy") or {}).get("on_clear") != "drain-first":
                self.service.operations.fail(operation["operation_id"],
                    "cache clear skipped: runner is busy or idle could not be confirmed")
                self._clear_operation(spec)
                report.held.append((spec["runner_id"], "cache clear skipped: not confirmed idle"))
                return
        action = decide(spec, operation, progress)
        if action is None:
            return
        if operation and operation["verb"] == "recreate" and action in {"deregister", "remove"}:
            try:
                self.service.validate_replacement(spec)
            except Exception as error:
                self._fail(spec, operation, error, report, "replacement preflight")
                return

        # The last lock. `decide` never asks for one of these on a busy
        # runner; this makes sure nothing ever can, whatever it decides.
        if states.aborts_a_job(action, spec["actual_state"]):
            report.held.append((spec["runner_id"],
                                f"{action} held: the runner is "
                                f"{spec['actual_state']}, and {action} would "
                                f"abort its job"))
            return

        # A drained runner is looked at once more before it is ended. A drain
        # at GitHub cannot take off the labels GitHub gives every runner, so a
        # job asking only for those can still reach it; if one has, the look
        # moves it back to draining and the lock above holds the step.
        if action in DESTRUCTIVE and spec["actual_state"] == "drained":
            self._do_observe(spec, None, report)
            spec = self.service.specs.get(spec["runner_id"])
            if states.aborts_a_job(action, spec["actual_state"]):
                report.held.append((spec["runner_id"],
                                    f"{action} held: the drained runner took "
                                    f"a job, and {action} would abort it"))
                return

        if action in NEEDS_HEALTHY_WORKER and not self._worker_accepts(spec):
            report.held.append((spec["runner_id"],
                                f"{action} held: worker "
                                f"{spec['host_id']} is not healthy"))
            return

        quiescent = getattr(self.executor, "quiescent", None)
        if action in DESTRUCTIVE | {"clear_cache"} and quiescent and not quiescent(spec):
            # A restarted controller can inherit stopping/failed while the
            # process still runs. Repeat the safe drain protocol rather than
            # assuming the interrupted step had already quiesced it.
            try:
                self._attempt(operation, "confirming quiescence before " + action)
                self.executor.drain(spec)
            except Exception as error:
                self._fail(spec, operation, error, report, "quiesce before " + action)
                return
            report.held.append((spec["runner_id"],
                                f"{action} held: unit may still be running a job; confirming it stopped"))
            report.did("quiesce", spec["runner_id"])
            return

        getattr(self, f"_do_{action}")(spec, operation, report)

    def _recreate_turn(self, spec, operation, report):
        """Only one replacement per fleet; preserve capacity after a failure."""
        siblings = [s for s in self._fleet_specs(spec["fleet_id"])
                    if s["runner_id"] != spec["runner_id"]]
        active = []
        for sibling in siblings:
            other = self._operation(sibling)
            if sibling["actual_state"] not in SERVING and not (
                    other and other["verb"] == "recreate" and other["state"] == "pending"):
                if operation["state"] == "pending":
                    report.held.append((spec["runner_id"], "recreate held: another fleet runner is unavailable"))
                    return False
            if other and other["verb"] == "recreate":
                active.append(other)
        if operation["state"] == "pending" and any(o["state"] == "running" for o in active):
            report.held.append((spec["runner_id"], "recreate held: replacement in progress"))
            return False
        if operation["state"] == "pending":
            first = min([operation] + active, key=lambda o: (o["requested_at"], o["operation_id"]))
            if first["operation_id"] != operation["operation_id"]:
                report.held.append((spec["runner_id"], "recreate queued"))
                return False
            try:
                self.service.validate_replacement(spec)
            except Exception as error:
                self.service.operations.fail(operation["operation_id"], str(error))
                self._clear_operation(spec)
                self._halt_recreates(spec, str(error), report)
                report.errors.append((spec["runner_id"], str(error)))
                return False
        return True

    def _cache_turn(self, spec, operation, report):
        """One cache clear per fleet at a time. A clear drains its runner
        first, and a fleet-wide "clear all cache" asks every idle runner at
        once; without turns the whole fleet was out of service together.
        The oldest request goes first. A runner of the fleet that is meant to
        serve but is not - still clearing, or on its way back - holds the
        rest; one stopped on purpose does not."""
        if operation["state"] != "pending" or spec["actual_state"] not in SERVING:
            return True
        queued = [operation]
        for sibling in self._fleet_specs(spec["fleet_id"]):
            if sibling["runner_id"] == spec["runner_id"]:
                continue
            if sibling["actual_state"] not in SERVING and sibling["desired_state"] == "running":
                # Meant to serve and not serving yet - clearing, or back from
                # a clear whose operation has already closed. Taking another
                # one out now is exactly what turns exist to prevent.
                report.held.append((spec["runner_id"], "cache clear queued: another runner of the fleet is out of service"))
                return False
            other = self._operation(sibling)
            if not other or other["verb"] != "clear_cache":
                continue
            if other["state"] == "running":
                report.held.append((spec["runner_id"], "cache clear queued: another runner of the fleet is clearing"))
                return False
            if other["state"] == "pending":
                queued.append(other)
        first = min(queued, key=lambda o: (o["requested_at"], o["operation_id"]))
        if first["operation_id"] != operation["operation_id"]:
            report.held.append((spec["runner_id"], "cache clear queued"))
            return False
        return True

    def _halt_recreates(self, failed_spec, reason, report):
        for sibling in self._fleet_specs(failed_spec["fleet_id"]):
            if sibling["runner_id"] == failed_spec["runner_id"]:
                continue
            operation = self._operation(sibling)
            if not operation or operation["verb"] != "recreate" or operation["state"] != "pending":
                continue
            error = f"fleet rebuild halted after {failed_spec['runner_id']} failed: {reason}"
            self.service.operations.fail(operation["operation_id"], error)
            self._clear_operation(sibling)
            report.errors.append((sibling["runner_id"], error))

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
        here raises rather than writing a state the design does not have.

        A runner that arrives in service has no error: `last_error` is the
        reason it is where it is, and once it is serving the reason is
        history - which the operation and the audit trail keep. Left on the
        row it is painted on the card for ever, and the page showed four
        idle runners in red over removals that had since succeeded
        (2026-09-21). A note is not an error and stays.
        """
        states.check(spec["actual_state"], to)
        if to in SERVING:
            extra.setdefault("last_error", None)
        self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                  actual_state=to, **extra)
        return self.service.specs.get(spec["runner_id"])

    def _fail(self, spec, operation, error, report, step=""):
        """Record a failed step on the runner and on its operation.

        A step failing from a state with no edge into `failed` - stopping,
        say - leaves the runner where it is with `last_error` set. It is then a
        transitional state past its deadline, which is exactly what the
        sweeper exists to find; forcing it into `failed` would write a
        transition the design does not have.
        """
        text = f"{step}: {error}" if step else str(error)
        message = f"{_iso(_now())} {text[:500]}"
        if getattr(error, "rollback_held", False):
            current = self.service.specs.get(spec["runner_id"])
            self.service.specs.update(current["runner_id"], current["spec_version"], last_error=message)
            if operation:
                self.service.operations.merge_progress(operation["operation_id"],
                    lambda progress: dict(progress, rollback_held=text,
                        rollback_retry_preflight=bool(getattr(error, "retry_preflight", False))))
            report.held.append((spec["runner_id"], text))
            return
        spec = self.service.specs.get(spec["runner_id"])
        extra = {"last_error": message}
        if operation:
            extra["current_operation"] = None
        # What the executor's compensations undid no longer exists, so the
        # spec must stop pointing at it. A failed runner that still named a
        # removed unit would send the next `remove` looking for it.
        if getattr(error, "removed_unit", False):
            extra["exec_unit_ref"] = None
        if getattr(error, "deregistered", False):
            extra["registration_id"] = None
            extra["registration_uuid"] = None
        if states.can(spec["actual_state"], "failed"):
            spec = self._move(spec, "failed", **extra)
        else:
            self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                      **extra)
        if operation:
            self.service.operations.fail(operation["operation_id"], text)
            if operation["verb"] == "recreate":
                self._halt_recreates(spec, text, report)
        report.errors.append((spec["runner_id"], text))

    def _attempt(self, operation, note):
        if operation:
            self.service.operations.attempt(operation["operation_id"], note)

    # ---- the steps ---------------------------------------------------------
    #
    # Each writes the transitional state BEFORE calling the executor, so an
    # interruption between the two leaves a record the sweeper can find
    # (design 12.5). Each then writes the resting state the call produced.

    def _do_provision(self, spec, operation, report):
        # A runner with nowhere to go waits in `planned`, with the reason,
        # rather than failing: it is not broken, the fleet is full, and a
        # failed runner would hold its place in the fleet until someone
        # repaired it (T-1502).
        effective = self.service.effective_spec(spec)
        changes = {key: effective.get(key) for key in
                   ("cpu_limit", "memory_limit", "memory_swap_limit", "disk_limit", "runtime_template")
                   if effective.get(key) != spec.get(key)}
        if changes:
            self.service.specs.update(spec["runner_id"], spec["spec_version"], **changes)
            spec = self.service.specs.get(spec["runner_id"])
        placement = getattr(self.executor, "placement", None)
        if spec["actual_state"] == "planned" and not spec["host_id"] and                 placement is not None:
            host_id, why = placement(spec)
            if host_id is None:
                report.held.append((spec["runner_id"], f"not placed: {why}"))
                return
        self._provision(spec, operation, report)

    def _provision(self, spec, operation, report):
        # A runner the fleet asked for has no operation of its own yet. It
        # gets one here, because the operation carries the deadline the sweep
        # measures against - without it, a runner interrupted mid-creation
        # could never be told apart from one that is merely slow.
        if operation is None:
            operation, _ = self.service.operations.open(
                "provision", runner_id=spec["runner_id"],
                requested_by="reconciler")
            self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                      current_operation=operation[
                                          "operation_id"])
            spec = self.service.specs.get(spec["runner_id"])
        self._attempt(operation, "provisioning")
        if spec["actual_state"] != "provisioning":
            spec = self._move(spec, "provisioning")
        try:
            result = self.executor.provision(
                spec, on_placed=self._recorder(spec, "host_id")) or {}
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "provision")
            return
        self.service.operations.merge_progress(operation["operation_id"],
            lambda progress: {key: value for key, value in progress.items()
                              if key not in ("rollback_held", "rollback_retry_preflight")})
        spec = self.service.specs.get(spec["runner_id"])
        self._move(spec, "provisioned",
                   exec_unit_ref=result.get("exec_unit_ref"),
                   host_id=result.get("host_id") or spec["host_id"])
        report.did("provision", spec["runner_id"])

    def _recorder(self, spec, *fields):
        """A hook that writes what a step made before the next step starts -
        12.5's "recorded before". Re-reads the spec each time, because the
        write it is making is itself a change to it."""
        runner_id = spec["runner_id"]

        def record(value):
            current = self.service.specs.get(runner_id)
            if isinstance(value, dict):
                changes = {k: value.get(k) for k in fields}
            else:
                changes = {fields[0]: value}
            self.service.specs.update(runner_id, current["spec_version"],
                                      **changes)
        return record

    def _do_register(self, spec, operation, report):
        self._attempt(operation, "registering")
        spec = self._move(spec, "registering")
        try:
            result = self.executor.register(
                spec, on_registered=self._recorder(
                    spec, "registration_id", "registration_uuid")) or {}
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "register")
            return
        spec = self.service.specs.get(spec["runner_id"])
        # Registered and online, but not with what the fleet asked for.
        # Not a failure - the runner works - and not silent either, so it is
        # a note: the error's column paints every card red, and a fleet of
        # adopted runners all carry labels of their own (T-0802).
        extra = {"last_note": (f"{_iso(_now())} registered with other labels "
                               f"than the fleet's: "
                               f"{result['drift']}"[:500])
                 if result.get("drift") else None}
        spec = self._move(spec, "idle",
                          registration_id=result.get("registration_id"),
                          registration_uuid=result.get("registration_uuid"),
                          **extra)
        report.did("register", spec["runner_id"])
        # Closed now rather than on the next pass: a runner that is serving
        # with its creation still "in flight" refuses every action asked of
        # it until then, for no reason anyone could see.
        if operation:
            self._close_if_fulfilled(spec, operation, report)

    def _do_start(self, spec, operation, report):
        self._attempt(operation, "starting")
        if spec["actual_state"] != "starting":      # a re-drive stays put
            spec = self._move(spec, "starting")
        try:
            self.executor.start(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "start")
            return
        self._move(spec, "idle")
        self._advance(operation, "started")
        report.did("start", spec["runner_id"])

    def _do_stop(self, spec, operation, report):
        self._attempt(operation, "stopping")
        if spec["actual_state"] != "stopping":      # a re-drive stays put
            spec = self._move(spec, "stopping")
        try:
            self.executor.stop(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "stop")
            return
        spec = self._move(spec, "stopped")
        self._advance(operation, "stopped")
        report.did("stop", spec["runner_id"])
        self._close_if_fulfilled(spec, operation, report)

    def _do_drain(self, spec, operation, report):
        """Asked for on every pass while the runner is draining, the way an
        interrupted stop is taken again: a drain that failed, or was cut off,
        or was never confirmed, is simply asked for again, and every layer of
        it is idempotent. The runner is drained when the executor proves it -
        the forge shows no job, and a runner drained on its worker has
        stopped - never because the machine last saw it idle: a job can
        arrive between that sighting and the drain."""
        if spec["actual_state"] != "draining":      # a re-drive stays put
            self._attempt(operation, "draining")
            spec = self._move(spec, "draining")
        try:
            done = self.executor.drain(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "drain")
            return
        if done is True:
            self._move(spec, "drained")
        report.did("drain", spec["runner_id"])

    def _do_cancel_drain(self, spec, operation, report):
        self._attempt(operation, "cancelling drain")
        try:
            self.executor.cancel_drain(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "cancel drain")
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
        if spec["actual_state"] != "deregistering":  # a re-drive stays put
            spec = self._move(spec, "deregistering")
        try:
            self.executor.deregister(spec)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "deregister")
            return
        # The record is gone, so the spec stops naming it: a later step that
        # finds ids here takes them to mean a record still exists.
        self._move(spec, "removing", registration_id=None,
                   registration_uuid=None)
        report.did("deregister", spec["runner_id"])

    def _do_remove(self, spec, operation, report):
        """The last step, and where recreate turns back.

        A recreate in flight keeps the storage and returns to provisioning
        under the same runner_id, which is what lets it find its storage by
        name. Anything else removes the storage too and ends at absent.
        """
        if spec.get("registration_id") or spec.get("registration_uuid"):
            # Still registered. The machine takes `failed` straight to
            # `removing`, and a runner can fail with its record intact - so
            # the record goes first, before the runner even moves, and a
            # removal that cannot get rid of it keeps the unit rather than
            # stranding the record (T-0903).
            self._attempt(operation, "deregistering before removal")
            try:
                self.executor.deregister(spec)
            except Exception as e:              # noqa: BLE001
                self._fail(spec, operation, e, report,
                           "deregister before remove - the unit is kept")
                return
            self.service.specs.update(spec["runner_id"], spec["spec_version"],
                                      registration_id=None,
                                      registration_uuid=None)
            spec = self.service.specs.get(spec["runner_id"])
        if spec["actual_state"] == "failed":
            spec = self._move(spec, "removing")
        # A recreate keeps the storage and comes back under the same
        # runner_id; a removal keeps nothing and ends at absent. The desired
        # state says which, and says it for a pass with no operation too -
        # a rebuild whose removal was refused has nothing else to go on
        # (2026-09-20).
        recreating = spec["desired_state"] != "absent"
        self._attempt(operation, "removing"
                      + (" (keeping storage)" if recreating else ""))
        try:
            self.executor.remove(spec, keep_data=recreating)
        except Exception as e:                  # noqa: BLE001
            self._fail(spec, operation, e, report, "remove")
            return
        if recreating:
            # The unit it was adopted from is gone, so there is nothing left
            # to adopt: the runner is built from its fleet's image like any
            # other, and under the fleet's own name (T-0802, 2026-09-20).
            forget_adoption(self.service.specs, spec)
            spec = self.service.specs.get(spec["runner_id"])
            try:
                replacement = self.service.replacement_spec(spec)
            except Exception as error:          # noqa: BLE001
                # The old unit is already gone by this point (`self.
                # executor.remove` above succeeded), so a `Refused` here -
                # a pinned width that no longer fits this host, say - must
                # not fall into the pass's generic handler: that leaves the
                # runner stuck in `removing` with no recorded reason and its
                # `recreate` operation open forever (finding 2, 2026-09-23).
                self._fail(spec, operation, error, report,
                           "replacement after removal")
                return
            fields = {key: replacement.get(key) for key in
                      ("runtime_template", "cpu_limit", "memory_limit", "memory_swap_limit", "disk_limit",
                       "labels", "cache_policy", "runner_group")}
            self.service.specs.update(spec["runner_id"], spec["spec_version"], **fields)
            spec = self.service.specs.get(spec["runner_id"])
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
            self._clear_operation(spec)
            report.errors.append((spec["runner_id"], "busy at clear time"))
            return
        self._attempt(operation, "clearing cache")
        try:
            freed = self.executor.clear_cache(spec) or {}
        except Exception as e:                  # noqa: BLE001
            self.service.operations.fail(operation["operation_id"],
                                         f"clear cache: {e}")
            self._clear_operation(spec)
            report.errors.append((spec["runner_id"], str(e)))
            return
        self.service.operations.succeed(operation["operation_id"], freed)
        self._clear_operation(spec)
        report.did("clear_cache", spec["runner_id"])

    def _do_observe(self, spec, operation, report, fresh=False):
        """Ask what the world looks like, and record it if it is news.

        Only observed edges are taken here. An executor reporting something the
        machine cannot reach from the current state is ignored and noted,
        because believing it would write a state the runner never passed
        through to get there.
        """
        try:
            observe = getattr(self.executor, "observe_fresh", self.executor.observe) if fresh else self.executor.observe
            seen = observe(spec)
        except Exception:                       # noqa: BLE001
            return
        finally:
            self._record_forge(spec)
        if not seen or seen == spec["actual_state"]:
            return
        edge = (spec["actual_state"], seen)
        if edge not in states.OBSERVED:
            report.held.append((spec["runner_id"],
                                f"ignored observation {edge}"))
            return
        self._move(spec, seen)
        report.did("observed", spec["runner_id"], seen)

    #: The forge's words that are an answer about the runner. "unknown" is
    #: the forge not answering, and does not move `forge_seen_at`.
    FORGE_ANSWERS = frozenset({"idle", "busy", "offline"})

    def _record_forge(self, spec):
        """What the forge said of this runner on this pass, and when it last
        said anything - an observation, written outside spec_version like
        unit_state, so it never collides with a change a person is making.
        Readiness is read from it: process up is not ready while the forge
        cannot confirm the runner (T-1803).

        The labels the forge's own record lists for the runner ride along on
        the same read (T-1803/W2b): both are consumed here, before either
        early return, so a reading with words but no labels - or the reverse
        - still gets written.
        """
        words = getattr(self.executor, "forge_words", None)
        has_word = isinstance(words, dict) and spec["runner_id"] in words
        labels = getattr(self.executor, "forge_labels", None)
        has_labels = isinstance(labels, dict) and spec["runner_id"] in labels
        if not has_word and not has_labels:
            return
        word = words.pop(spec["runner_id"]) if has_word else None
        names = labels.pop(spec["runner_id"]) if has_labels else None
        from store import schema
        with schema.connect(self.service.specs.path) as c:
            if has_word:
                if word in self.FORGE_ANSWERS:
                    c.execute("UPDATE runner_specs SET forge_state = ?,"
                              " forge_seen_at = ? WHERE runner_id = ?",
                              (word, _iso(_now()), spec["runner_id"]))
                else:
                    c.execute("UPDATE runner_specs SET forge_state = ?"
                              " WHERE runner_id = ?",
                              ("unknown", spec["runner_id"]))
            if has_labels and names is not None:
                import json
                c.execute("UPDATE runner_specs SET forge_labels = ?, forge_labels_at = ?"
                          " WHERE runner_id = ?",
                          (json.dumps(names), _iso(_now()), spec["runner_id"]))

    # ---- operations --------------------------------------------------------

    def _advance(self, operation, phase):
        if not operation:
            return

        def add(progress):
            # Merged, not replaced: the same progress also holds the state of
            # each agent call, written by events that can land at any moment.
            progress["done"] = list(progress.get("done", [])) + [phase]
            return progress

        self.service.operations.merge_progress(operation["operation_id"],
                                               add)

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


class _Abandoned(Exception):
    """Why a runner was swept: interrupted mid-creation, and what was undone.

    Raised by nobody - handed to `_fail` as a reason, so a swept runner is
    recorded exactly as a failed one is, with the compensations it had.
    """

    def __init__(self, state, undone):
        self.compensated = tuple(undone)
        super().__init__(
            f"interrupted while {state} and not finished before its "
            f"deadline; undone: {', '.join(undone) or 'nothing to undo'}")

    @property
    def removed_unit(self):
        return "remove_unit" in self.compensated

    @property
    def deregistered(self):
        return "deregister" in self.compensated


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

    # -- a removal whose operation is gone -----------------------------------
    # Nothing else looks at this: the sweep watches creations that are
    # overdue, and every other transitional state re-drives itself through
    # its operation. A spec left in `removing` with none open is stranded,
    # and its removal is the step to take again: one that finished would
    # have left `provisioning` or `absent`. Removing is safe to repeat, and
    # the desired state says where it ends. Assuming the unit was already
    # gone left a runner that had been deregistered still running and
    # unmanaged, when the removal had been refused for a degraded worker
    # (2026-09-20).
    if actual in ("removing", "deregistering") and verb is None:
        # Failed rebuilding needs an explicit retry; a crash without a
        # reported failure is recovered through _step's normal preflight.
        if desired == "absent" or not spec.get("last_error"):
            return "remove" if actual == "removing" else "deregister"
        return None

    # -- operations that are more than a desired state ----------------------
    if verb == "clear_cache":
        # Under a drain-first policy a runner at work is drained, and cleared
        # once its job is done - never under it. Under the default it is
        # skipped: the clear step refuses a runner that took a job meanwhile.
        policy = spec.get("cache_policy") or {}
        if actual in ("idle", "draining") or (
                actual == "busy" and policy.get("on_clear") == "drain-first"):
            return "drain"
        return "clear_cache"

    if verb == "restart" and "started" not in done:
        if actual in ("idle", "busy"):
            return "drain"                  # never abort the job
        if actual == "drained" and "stopped" not in done:
            return "stop"
        if actual == "stopped":
            return "start"
        # draining, stopping and starting fall through to be taken again.

    if verb == "recreate" and "rebuilding" not in done:
        if actual in ("idle", "busy"):
            return "drain"
        if actual in ("drained", "stopped"):
            return "deregister"
        if actual in ("removing", "failed"):
            return "remove"
        # draining and deregistering fall through to be taken again.

    if verb == "repair" and actual == "failed":
        return "repair"

    # -- a step that was interrupted part-way is taken again -----------------
    # Each of these has exactly one way forward, and each step is idempotent -
    # stopping a stopped unit, starting a started one, deleting a record that
    # is gone. So the step is simply taken again rather than waited on for
    # ever, which is what "observe" would have done: nothing reports these.
    if actual == "stopping":
        return "stop"
    if actual == "starting":
        return "start"
    if actual == "deregistering":
        return "deregister"
    if actual == "draining":
        return "drain"

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
        if actual in ("idle", "busy"):
            return "drain"
        if actual == "drained":
            return "stop"
        if actual in ("stopped", "planned", "failed"):
            return None
        return "observe"

    if desired == "drained":
        if actual in ("idle", "busy"):
            return "drain"
        if actual == "drained":
            # Kept looked at: a runner drained at GitHub can still be sent a
            # job that asks only for labels a drain cannot take off.
            return "observe"
        if actual in ("planned", "stopped", "failed"):
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
