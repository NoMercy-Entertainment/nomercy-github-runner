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
