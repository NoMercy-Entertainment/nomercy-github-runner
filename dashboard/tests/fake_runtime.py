"""Runtime stand-ins for the controller tests.

In its own module rather than inside a test file, because the service resolves
a runtime by importing `"module:attribute"`. Naming a test module there would
import it a second time under a different name, giving a different class object
and re-running its top level - so an identity check would fail for a reason
that has nothing to do with what is being tested.
"""


class NeverBuilt:
    """Resolving this is fine; constructing it is a bug.

    The service is allowed to look a runtime up while validating a plan - that
    is how an unbuildable cell is refused early. It must not create one, because
    creating one is the work, and no call in the service performs work.
    """

    def __init__(self, *args, **kwargs):    # pragma: no cover - must not run
        raise AssertionError(
            "the service instantiated a runtime; a call performed work")


class Placeholder:
    """An arbitrary but real class, for proving the lookup reads the table."""


class RecordingRuntime:
    """Answers reads and remembers what it was asked.

    `calls` is a class attribute so a test can inspect what the service did to
    an instance the service built for itself. It lives here, not in a test
    file, for the same double-import reason as the classes above: a second
    copy of a test module would carry a second, always-empty `calls`.
    """

    calls = []

    def status(self, ref):
        RecordingRuntime.calls.append(("status", ref.handle, ref.kind.value))
        return {"running": True}

    def logs(self, ref, since_seconds=45):
        RecordingRuntime.calls.append(("logs", ref.handle, since_seconds))
        return "a log line"

    def telemetry(self, ref):
        RecordingRuntime.calls.append(("telemetry", ref.handle))
        return {"cpu_percent": 3.0}


class UnitRuntime:
    """A runtime adapter that keeps its units in a dict instead of an engine.

    Class-level state, because the provisioning flow builds a fresh instance
    for every call from the service's table, exactly as it will in production.
    `reset()` between tests. `fail_on` names the adapter methods that raise.
    """

    units = {}
    log = []
    fail_on = set()

    @classmethod
    def reset(cls):
        cls.units = {}
        cls.log = []
        cls.fail_on = set()
        cls.crash_after_create = False

    def _maybe_fail(self, name):
        if name in UnitRuntime.fail_on:
            raise RuntimeError(f"runtime {name} failed on purpose")

    #: Raised after the unit exists and before create returns: the crash
    #: T-0308 calls "create before record".
    crash_after_create = False

    def create(self, spec):
        from runtime.base import ExecUnitKind, ExecUnitRef
        from control.service import EXEC_KINDS
        handle = spec.get("name") or f"unit-{spec['runner_id'][:8]}"
        UnitRuntime.log.append(("create", handle, spec.get("host_id")))
        self._maybe_fail("create")
        if handle in UnitRuntime.units:
            # What a real engine does with a second unit of the same name.
            raise RuntimeError(f"a unit named {handle} already exists")
        UnitRuntime.units[handle] = {"storage": spec.get("storage")}
        if UnitRuntime.crash_after_create:
            UnitRuntime.crash_after_create = False
            from tests.fake_platform import Crash
            raise Crash("the controller died after creating the unit")
        return ExecUnitRef(kind=ExecUnitKind(EXEC_KINDS[spec["platform"]]),
                           handle=handle)

    def status(self, ref):
        from runtime.base import ExecUnitStatus
        return ExecUnitStatus(exists=ref.handle in UnitRuntime.units,
                              running=ref.handle in UnitRuntime.units)

    def remove(self, ref, keep_data=False):
        UnitRuntime.log.append(("remove", ref.handle, keep_data))
        self._maybe_fail("remove")
        UnitRuntime.units.pop(ref.handle, None)     # safe if absent

    def start(self, ref):
        UnitRuntime.log.append(("start", ref.handle))

    def stop(self, ref):
        UnitRuntime.log.append(("stop", ref.handle))

    def clear_cache(self, ref, policy):
        from runtime.base import Freed
        UnitRuntime.log.append(("clear_cache", ref.handle))
        return Freed(total_bytes=2048, measured=True)


class CacheUnitRuntime(UnitRuntime):
    """UnitRuntime whose units each hold a cache, for T-1602 and T-1603.

    Per unit, per scope, a number of bytes - cleared scope by scope, measured
    before and after, with `fail_scopes` failing on purpose. `cleared` lists
    every unit a clear was run in, in order, which is how a test sees that a
    clear touched no other runner. Class-level for the same reason as
    UnitRuntime; `reset_caches()` between tests.
    """

    caches = {}
    fail_scopes = set()
    cleared = []
    measure_fails = False

    @classmethod
    def reset_caches(cls):
        cls.caches = {}
        cls.fail_scopes = set()
        cls.cleared = []
        cls.measure_fails = False

    def clear_cache(self, ref, policy):
        from runtime.base import Freed
        cls = CacheUnitRuntime
        cls.cleared.append(ref.handle)
        mine = cls.caches.setdefault(ref.handle, {})
        scopes = list((policy or {}).get("scopes")
                      or ("engine-build-cache", "workspace"))
        before = {s: mine.get(s, 0) for s in scopes}
        per_scope, errors = {}, {}
        for scope in scopes:
            if scope in cls.fail_scopes:
                errors[scope] = f"{scope} could not be cleared"
                continue
            per_scope[scope] = mine.get(scope, 0)
            mine[scope] = 0
        after = {s: mine.get(s, 0) for s in scopes}
        if cls.measure_fails:
            return Freed(per_scope={}, errors=errors, total_bytes=0,
                         before=None, after=None, measured=False)
        return Freed(per_scope=per_scope, errors=errors,
                     total_bytes=sum(per_scope.values()), before=before,
                     after=after, measured=True)
