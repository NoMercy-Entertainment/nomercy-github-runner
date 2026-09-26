"""The runner fleets, and how many runners each is meant to have.

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
a coordinate in the supported fleet set, it is unique on `(provider, platform,
architecture)` by constraint, and there is exactly one of each. A readable
`github-linux-x64` is the coordinate written down, not a name that might drift
from what it points at.

Files: the plan named `store/specs.py` for this. Kept separate because that
module is about one table with its own rules - minted identity, optimistic
concurrency, soft delete - and none of the three apply here. A `FleetStore`
inside a module called `specs` would be a name that lies.
"""
import json
import math

import providers

from . import schema

# The existing x64 cells plus Windows ARM64 for both forges. New rows start
# at zero capacity; seeding does not change an operator's existing capacity.
CELLS = tuple(
    (provider.key, platform, providers.X64)
    for provider in providers.ALL
    for platform in providers.PLATFORMS
) + tuple((provider.key, providers.WINDOWS, providers.ARM64)
          for provider in providers.ALL)


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
        second run finds the same rows. `desired_capacity` is left alone on
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

    def _resource_workers(self, fleet, host_id=None):
        from control import placement
        from control.inventory import Inventory
        return [worker for worker in Inventory(self.path).healthy(kind=placement.WORKER_KIND[fleet["platform"]])
                if (host_id is None or worker["host_id"] == host_id)
                and placement.declared(worker, "kind") == placement.RUNTIME_KIND[fleet["platform"]]
                and (placement.declared(worker, "architecture") or "x64") == fleet["architecture"]
                and placement.declared(worker, "capacity_valid") is not False]

    def resource_support(self, fid, host_id=None):
        """Fresh evidence for editable limits, shared by API and validation.

        One worker must advertise the whole appliance guarantee; flags from
        different workers must never be combined into an invented capability.
        """
        from control import placement
        fleet = self.get(fid)
        if fleet is None:
            raise UnknownFleet(f"no fleet {fid}")
        workers = self._resource_workers(fleet, host_id)
        mac = fleet["platform"] == "macos"
        enforced = [worker for worker in workers if placement.enforces_appliance_limits(worker)]

        def common_positive(key):
            values = [placement.declared(worker, key) for worker in enforced]
            if values and all(type(value) is int and value > 0 for value in values) and len(set(values)) == 1:
                return values[0]
            return None

        return {"cpu_limit_supported": bool(enforced) if mac else True,
                "memory_limit_supported": bool(enforced) if mac else True,
                "disk_quota_supported": any(placement.enforces_disk_quota(worker) for worker in workers),
                "appliance_per_runner": mac and bool(enforced),
                "per_runner_memory_overhead_bytes": common_positive("per_runner_memory_overhead_bytes") if mac else None,
                "guest_disk_virtual_bytes": common_positive("guest_disk_virtual_bytes") if mac else None}

    def supports_disk_quota(self, fid):
        return bool(self.get(fid)) and self.resource_support(fid)["disk_quota_supported"]

    def set_defaults(self, fid, values):
        allowed = {"labels", "runner_group", "unit_template", "cpu_limit", "memory_limit", "memory_swap_limit", "disk_limit", "cache_policy"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("unknown fleet setting")
        fleet = self.get(fid)
        if fleet is None:
            raise UnknownFleet(f"no fleet {fid}")
        values = dict(values)
        if values.get("memory_swap_limit") is not None and fleet["platform"] != "linux":
            raise ValueError("RAM + swap limits are supported only on Linux")
        if fleet["platform"] == "macos" and any(key in values for key in ("cpu_limit", "memory_limit")):
            support = self.resource_support(fid)
            if any(not support[key + "_supported"] for key in ("cpu_limit", "memory_limit") if key in values):
                raise ValueError("CPU/RAM changes require a healthy macOS worker confirming per-runner appliance enforcement")
        if values.get("disk_limit") is not None and not self.supports_disk_quota(fid):
            raise ValueError("Per-runner disk quotas are unavailable on this fleet's workers")
        for key, value in values.items():
            if key == "labels":
                if not isinstance(value, list) or len(value) > 100 or any(
                        not isinstance(v, str) or not v.strip() or len(v) > 256
                        or any(ch in v for ch in "\r\n\x00") for v in value):
                    raise ValueError("labels must be a list of nonempty single-line labels")
                values[key] = json.dumps(list(dict.fromkeys(v.strip() for v in value)))
            elif key in ("unit_template", "runner_group"):
                if value is not None and (not isinstance(value, str) or len(value) > 1024
                        or any(ch in value for ch in "\r\n\x00")):
                    raise ValueError(f"{key} must be single-line text")
                values[key] = value.strip() or None if value else None
            elif key == "cache_policy":
                if value is not None:
                    if not isinstance(value, dict) or set(value) - {"max_bytes", "scopes", "on_clear", "enabled"}:
                        raise ValueError("cache_policy accepts max_bytes, scopes, on_clear and enabled")
                    maximum = value.get("max_bytes")
                    if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0):
                        raise ValueError("cache max_bytes must be positive bytes")
                    scopes = value.get("scopes")
                    if scopes is not None and (not isinstance(scopes, list) or any(
                            scope not in ("engine-build-cache", "engine-images-unused", "workspace", "toolcache", "temp") for scope in scopes)):
                        raise ValueError("unknown cache scope")
                    if "on_clear" in value and value["on_clear"] not in ("skip-if-busy", "drain-first"):
                        raise ValueError("cache on_clear must be skip-if-busy or drain-first")
                    if "enabled" in value and not isinstance(value["enabled"], bool):
                        raise ValueError("cache enabled must be boolean")
                    if fleet["platform"] != "linux":
                        if maximum is not None or value.get("enabled") is True:
                            raise ValueError("automatic cache budgets are supported only on Linux")
                        if any(scope.startswith("engine-") for scope in (scopes or [])):
                            raise ValueError("engine cache scopes are supported only on Linux")
                    values[key] = json.dumps(value)
            elif key == "cpu_limit":
                if value is not None:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        raise ValueError("cpu_limit must be positive") from None
                    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
                        raise ValueError("cpu_limit must be positive")
                    if fleet["platform"] == "macos":
                        if not number.is_integer() or not 1 <= number <= 64:
                            raise ValueError("macOS appliance CPU limit must be a whole count from 1 to 64")
                        values[key] = str(int(number))
                    else:
                        values[key] = str(number)
            elif value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                        or value <= 0 or value > 2**63 - 1):
                raise ValueError(f"{key} must be positive bytes or null")
            elif key == "memory_limit" and value is not None and fleet["platform"] == "macos":
                if value % (1024**3) or not 4 * 1024**3 <= value <= 128 * 1024**3:
                    raise ValueError("macOS appliance memory must be whole GiB from 4 to 128")
        if values:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                current = dict(c.execute("SELECT * FROM fleets WHERE fleet_id=?", (fid,)).fetchone())
                current.update(values)
                memory, swap = current.get("memory_limit"), current.get("memory_swap_limit")
                if memory is not None and swap is not None and swap < memory:
                    raise ValueError("RAM + swap ceiling must be at least the memory limit")
                c.execute("UPDATE fleets SET " + ", ".join(f"{k}=?" for k in values)
                          + " WHERE fleet_id=?", (*values.values(), fid))
        return self.get(fid)

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
    for key in ("labels", "cache_policy", "resource_defaults"):
        if fleet.get(key):
            try:
                fleet[key] = json.loads(fleet[key])
            except (ValueError, TypeError):
                pass
    return fleet
