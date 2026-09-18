"""The six fleets, and how many runners each is meant to have.

A fleet is one cell of the provider x platform matrix: what to build, how many
of them, and with which defaults. The dashboard sets a number; the controller
converges on it. Nobody creates a runner by hand.

**They are rows, not code.** A seventh cell - `github/linux/arm64`, say -
needs a row, not a branch. That is the whole reason this table exists rather
than a constant somewhere: the previous shape of this system had the fleet
implicit in a compose file with six near-identical service blocks, and adding
one meant editing YAML in six places and hoping.

**Availability is derived; capacity is intended.** A cell exists when the
provider says it does, so `available` is refreshed from `provider.supports()`
every time the fleets are seeded. Capacity is what an operator asked for and is
never touched by seeding. Conflating the two would let a redeploy silently
reset someone's fleet to zero, or leave a fleet claiming to want four runners
of a kind that cannot be built.

**Why a fleet id is readable and a runner id is not.** A runner's identity must
not be derivable from anything a caller knows, because names are how this fleet
came to have three of them for one runner. A fleet is different in kind: it is
a coordinate in a fixed matrix, it is unique on `(provider, platform,
architecture)` by constraint, and there is exactly one of each. A readable
`github-linux-x64` is the coordinate written down, not a name that might drift
from what it points at.

Files: the plan named `store/specs.py` for this. Kept separate because that
module is about one table with its own rules - minted identity, optimistic
concurrency, soft delete - and none of the three apply here. A `FleetStore`
inside a module called `specs` would be a name that lies.
"""
import json

import providers

from . import schema

#: The six cells of design section 9.5. Architecture is x64 throughout: ARM64
#: is documented by GitHub but there is no ARM worker to place it on, so a
#: seventh row would claim capacity that cannot be satisfied.
CELLS = tuple(
    (provider.key, platform, providers.X64)
    for provider in providers.ALL
    for platform in providers.PLATFORMS
)


def fleet_id(provider_key, platform, architecture):
    """The coordinate, written down. See the module docstring."""
    return f"{provider_key}-{platform}-{architecture}"


class FleetStore:
    def __init__(self, path=None):
        self.path = path or schema.DB_PATH

    def _conn(self):
        return schema.connect(self.path)

    # ---- seeding -----------------------------------------------------------

    def seed(self, env=None):
        """Create any missing fleet and refresh every fleet's availability.

        Idempotent by construction: identity comes from the coordinate, so a
        second run finds the same six rows. `desired_capacity` is left alone on
        rows that already exist, because it is an operator's intent and seeding
        is not an operator.

        An unsupported cell is seeded at capacity 0 and `available=False`, with
        the provider's own reason stored beside it. Storing the reason matters:
        an unavailable fleet and a broken one look identical if all that is
        recorded is a false.
        """
        env = env if env is not None else {}
        with self._conn() as c:
            for provider_key, platform, arch in CELLS:
                provider = providers.by_key(provider_key)
                support = provider.supports(platform, arch, env)
                fid = fleet_id(provider_key, platform, arch)

                existing = c.execute(
                    "SELECT fleet_id FROM fleets WHERE fleet_id = ?",
                    (fid,)).fetchone()

                if existing is None:
                    c.execute(
                        "INSERT INTO fleets (fleet_id, provider, platform,"
                        " architecture, desired_capacity, labels, template,"
                        " available, unavailable_reason)"
                        " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                        (fid, provider_key, platform, arch,
                         json.dumps([]), self._template(provider, platform,
                                                        arch, env),
                         1 if support else 0, support.reason or None))
                else:
                    # Availability only. Capacity is not seeding's business.
                    c.execute(
                        "UPDATE fleets SET available = ?,"
                        " unavailable_reason = ? WHERE fleet_id = ?",
                        (1 if support else 0, support.reason or None, fid))

    def _template(self, provider, platform, architecture, env):
        """What this fleet's runners are built from, when that is knowable.

        None rather than a guess where the provider has no artefact: a template
        invented here would be discovered wrong at registration time, on a
        runner that already exists.
        """
        artefact = provider.agent_artifact(platform, architecture, env)
        return artefact.reference if artefact else None

    # ---- reading -----------------------------------------------------------

    def get(self, fid):
        with self._conn() as c:
            row = c.execute("SELECT * FROM fleets WHERE fleet_id = ?",
                            (fid,)).fetchone()
        return _decode(row)

    def list(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM fleets ORDER BY provider, platform,"
                " architecture").fetchall()
        return [_decode(r) for r in rows]

    # ---- capacity ----------------------------------------------------------

    def set_capacity(self, fid, count, requested_by=None,
                     idempotency_key=None):
        """Record that a fleet should have `count` runners; return an
        operation id.

        Setting capacity does not create anything. It states an intention and
        opens an operation the controller works through, which is what makes
        the change observable while it is happening and auditable afterwards.
        Returning the id rather than a boolean is what lets a caller follow it.

        A fleet that cannot be built is refused here rather than accepted and
        left to fail later on a runner that was half made.
        """
        if count < 0:
            raise ValueError("capacity cannot be negative")

        fleet = self.get(fid)
        if fleet is None:
            raise UnknownFleet(f"no fleet {fid}")
        if not fleet["available"]:
            raise FleetUnavailable(
                f"{fid} cannot be given capacity: "
                f"{fleet['unavailable_reason'] or 'the cell is unavailable'}")

        # Late import, and the direction is wrong on purpose. Operations carry
        # policy - deadlines, attempt counting, the rule that a closed outcome
        # is never overwritten - so they live in `control`, and a store reaching
        # up into the controller is not where this belongs. It is here because
        # the alternative was a second, simpler INSERT in this module, and that
        # second version had already drifted: it wrote no deadline, which makes
        # an operation the sweeper can never re-drive. One definition with a
        # noted layering wart beats two definitions that disagree. Capacity
        # moves to the service in T-1501 and this import goes with it.
        from control.operations import OperationStore

        operation, created = OperationStore(self.path).open(
            "set_capacity", fleet_id=fid, requested_by=requested_by,
            idempotency_key=idempotency_key,
            note=f"desired capacity {fleet['desired_capacity']} -> {count}")
        if not created:
            # A repeat of a request already carried out. Applying it again
            # would undo any change made since - "add one", sent twice,
            # must add one.
            return operation["operation_id"]

        with self._conn() as c:
            c.execute("UPDATE fleets SET desired_capacity = ?"
                      " WHERE fleet_id = ?", (count, fid))
        return operation["operation_id"]


class UnknownFleet(Exception):
    pass


class FleetUnavailable(Exception):
    """Asked for capacity in a cell that does not exist.

    Separate from UnknownFleet: the fleet is real, the platform is not
    buildable, and the reason says what would make it so.
    """


def _decode(row):
    if row is None:
        return None
    fleet = dict(row)
    fleet["available"] = bool(fleet["available"])
    if fleet.get("labels"):
        try:
            fleet["labels"] = json.loads(fleet["labels"])
        except (ValueError, TypeError):
            pass
    return fleet
