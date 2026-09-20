"""Reading and writing RunnerSpecs.

Three rules are enforced here rather than trusted to callers, because each one
has a matching failure this fleet has already had in some form.

**The store mints the identity.** `runner_id` is a UUIDv4 generated here. A
caller that supplies one is refused rather than obeyed. Identity chosen by a
caller becomes identity derived from something a caller knows - a name, an
index, a position in a list - and that is how one runner ends up with three
names and no identity.

**Every change is optimistic.** An update carries the `spec_version` it was
based on and is refused if the row has moved on. Two operators on one dashboard
is the ordinary case, not the exotic one, and last-write-wins on a spec means
the second one silently discards the first one's change.

**Delete is soft.** The row stays readable for ever, because history refers to
it. A hard delete would leave every historical run pointing at nothing, which
is the failure NFR-11 exists to prevent.
"""
import json
import uuid
from datetime import datetime, timezone

from . import schema


class StaleSpec(Exception):
    """An update was based on a version the row has already moved past.

    Carries both versions so a caller can tell the operator what happened
    rather than just that it failed.
    """

    def __init__(self, runner_id, expected, actual):
        super().__init__(
            f"{runner_id} is at spec_version {actual}, not {expected}; "
            f"someone else changed it first")
        self.runner_id = runner_id
        self.expected = expected
        self.actual = actual


class UnknownSpec(Exception):
    pass


#: Columns a caller may set. `runner_id`, `created_at` and `spec_version` are
#: absent deliberately: the first two are minted here and the third is the
#: concurrency control, so a caller that could write them could defeat it.
WRITABLE = (
    "display_name", "provider", "platform", "architecture",
    "runtime_template", "host_id", "labels", "runner_group",
    "cpu_limit", "memory_limit", "disk_limit", "cache_policy",
    "desired_state", "actual_state", "registration_id", "registration_uuid",
    "last_seen_at", "current_operation", "last_error", "capabilities",
    "exec_unit_ref", "fleet_id", "adopt_unit", "last_note",
)

#: Stored as JSON text, decoded on the way out so callers never parse.
JSON_FIELDS = ("labels", "cache_policy", "capabilities", "telemetry",
               "adopt_unit")

REQUIRED = ("provider", "platform")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(fields):
    out = dict(fields)
    for key in JSON_FIELDS:
        if key in out and not isinstance(out[key], (str, type(None))):
            out[key] = json.dumps(out[key])
    return out


def _decode(row):
    if row is None:
        return None
    spec = dict(row)
    for key in JSON_FIELDS:
        raw = spec.get(key)
        if raw:
            try:
                spec[key] = json.loads(raw)
            except (ValueError, TypeError):
                # Keep the text rather than raising. A spec that cannot be read
                # is worse than one field that has to be looked at by eye.
                pass
    return spec


class SpecStore:
    """Every method takes a `runner_id`, never a name (FR-4)."""

    def __init__(self, path=None):
        self.path = path or schema.DB_PATH

    def _conn(self):
        return schema.connect(self.path)

    # ---- writing -----------------------------------------------------------

    def create(self, **fields):
        """Mint a new spec and return its runner_id.

        Refuses a caller-supplied id. That is not pedantry: the whole design
        rests on identity being unrelated to anything a caller can guess or
        reconstruct, and the cheapest place to hold that line is here.
        """
        if "runner_id" in fields:
            raise ValueError(
                "runner_id is minted by the store; a caller that chooses it "
                "makes identity derivable, which is what FR-4 forbids")
        missing = [k for k in REQUIRED if not fields.get(k)]
        if missing:
            raise ValueError(f"a spec needs {', '.join(missing)}")

        unknown = sorted(set(fields) - set(WRITABLE))
        if unknown:
            raise ValueError(f"not settable: {unknown}")

        runner_id = str(uuid.uuid4())
        values = _encode(fields)
        columns = ["runner_id", "created_at", "spec_version"] + list(values)
        params = [runner_id, now(), 1] + [values[k] for k in values]
        placeholders = ", ".join("?" * len(columns))

        with self._conn() as c:
            c.execute(
                f"INSERT INTO runner_specs ({', '.join(columns)}) "
                f"VALUES ({placeholders})", params)
        return runner_id

    def adopt(self, capacity, **fields):
        """Mint a spec for a runner that already exists, and raise its
        fleet's capacity to keep it - in one transaction (T-0802).

        The two writes cannot be apart. A spec whose fleet still wants zero
        runners is surplus, and the next reconciler pass marks it for
        withdrawal; raising capacity first instead makes the pass plan a
        second runner for a cell that already has one serving. Either way a
        pass landing between them undoes an adoption, so neither may be
        visible without the other.
        """
        if "runner_id" in fields:
            raise ValueError("runner_id is minted by the store")
        if not fields.get("fleet_id"):
            raise ValueError("an adopted runner joins a fleet; its capacity "
                             "is what keeps it")
        missing = [k for k in REQUIRED if not fields.get(k)]
        if missing:
            raise ValueError(f"a spec needs {', '.join(missing)}")
        unknown = sorted(set(fields) - set(WRITABLE))
        if unknown:
            raise ValueError(f"not settable: {unknown}")

        runner_id = str(uuid.uuid4())
        values = _encode(fields)
        columns = ["runner_id", "created_at", "spec_version"] + list(values)
        params = [runner_id, now(), 1] + [values[k] for k in values]
        placeholders = ", ".join("?" * len(columns))
        with self._conn() as c:
            c.execute(
                f"INSERT INTO runner_specs ({', '.join(columns)}) "
                f"VALUES ({placeholders})", params)
            c.execute("UPDATE fleets SET desired_capacity = ? "
                      "WHERE fleet_id = ?",
                      (int(capacity), fields["fleet_id"]))
        return runner_id

    def update(self, runner_id, spec_version, **changes):
        """Apply changes if the row is still at `spec_version`; return the new
        version.

        The read and the write are one statement with the version in its WHERE
        clause, so two callers cannot both pass a check and then both write.
        Doing it as a SELECT followed by an UPDATE would leave exactly that
        window open.
        """
        unknown = sorted(set(changes) - set(WRITABLE))
        if unknown:
            raise ValueError(f"not settable: {unknown}")
        if not changes:
            raise ValueError("an update with no changes is not a change")

        values = _encode(changes)
        assignments = ", ".join(f"{k} = ?" for k in values)
        params = list(values.values()) + [runner_id, spec_version]

        with self._conn() as c:
            cur = c.execute(
                f"UPDATE runner_specs SET {assignments}, "
                f"spec_version = spec_version + 1 "
                f"WHERE runner_id = ? AND spec_version = ?", params)
            if cur.rowcount:
                return spec_version + 1
            self._explain_failed_write(c, runner_id, spec_version)

    def soft_delete(self, runner_id, spec_version=None):
        """Mark the spec absent without removing the row.

        `deleted_at` and `desired_state` move together: a row that is deleted
        but still says it wants to be running would be reconciled back into
        existence by the controller.
        """
        with self._conn() as c:
            sql = ("UPDATE runner_specs SET deleted_at = ?, "
                   "desired_state = 'absent', "
                   "spec_version = spec_version + 1 WHERE runner_id = ?")
            params = [now(), runner_id]
            if spec_version is not None:
                sql += " AND spec_version = ?"
                params.append(spec_version)
            cur = c.execute(sql, params)
            if not cur.rowcount:
                self._explain_failed_write(c, runner_id, spec_version)

    def _explain_failed_write(self, c, runner_id, spec_version):
        """Say which of the two things went wrong.

        "It did not apply" is useless to an operator: a spec that has moved on
        and a spec that never existed need different responses.
        """
        row = c.execute(
            "SELECT spec_version FROM runner_specs WHERE runner_id = ?",
            (runner_id,)).fetchone()
        if row is None:
            raise UnknownSpec(f"no spec with runner_id {runner_id}")
        raise StaleSpec(runner_id, spec_version, row["spec_version"])

    # ---- reading -----------------------------------------------------------

    def get(self, runner_id):
        """One spec, deleted or not.

        Deleted rows are returned here on purpose. History refers to them, and
        a referent that cannot be read is not a referent.
        """
        with self._conn() as c:
            return _decode(c.execute(
                "SELECT * FROM runner_specs WHERE runner_id = ?",
                (runner_id,)).fetchone())

    def list(self, include_deleted=False, **filters):
        """Specs, newest first. Deleted ones are excluded unless asked for.

        The filter keys are checked against the column list rather than
        interpolated, because these reach a WHERE clause.
        """
        unknown = sorted(set(filters) - set(WRITABLE))
        if unknown:
            raise ValueError(f"not a filterable column: {unknown}")

        where, params = [], []
        if not include_deleted:
            where.append("deleted_at IS NULL")
        for key, value in filters.items():
            where.append(f"{key} = ?")
            params.append(value)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM runner_specs {clause} "
                f"ORDER BY created_at DESC, runner_id", params).fetchall()
        return [_decode(r) for r in rows]
