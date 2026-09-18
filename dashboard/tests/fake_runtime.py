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
