"""The control plane's state store.

The three properties worth testing here are the three the rest of the platform
leans on: identity is minted by the store, a concurrent change is refused
rather than lost, and a deleted spec stays readable so history keeps a
referent.

Every test runs against its own database file. A shared one would make the
order of tests part of their meaning, and a store whose tests depend on their
order is a store nobody can change safely.
"""
import sqlite3

import pytest

import providers
from store import schema
from store.specs import SpecStore, StaleSpec, UnknownSpec


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return SpecStore(path)


def a_spec(**over):
    fields = {"provider": "github", "platform": "linux",
              "architecture": "x64", "display_name": "github-runner-1"}
    fields.update(over)
    return fields


class TestIdentityIsMintedHere:
    def test_create_returns_an_id_the_caller_did_not_choose(self, store):
        runner_id = store.create(**a_spec())
        assert len(runner_id) == 36 and runner_id.count("-") == 4

    def test_two_specs_never_share_an_id(self, store):
        ids = {store.create(**a_spec()) for _ in range(50)}
        assert len(ids) == 50

    def test_a_caller_supplied_id_is_refused(self, store):
        """Identity a caller can choose becomes identity a caller can derive,
        which is the thing this table exists to stop."""
        with pytest.raises(ValueError, match="minted by the store"):
            store.create(runner_id="github-runner-1", **a_spec())

    def test_the_id_is_not_derived_from_the_display_name(self, store):
        a = store.create(**a_spec(display_name="github-runner-1"))
        b = store.create(**a_spec(display_name="github-runner-1"))
        assert a != b, "two runners may share a display name"


class TestRoundTrip:
    def test_what_goes_in_comes_back(self, store):
        runner_id = store.create(**a_spec(
            runner_group="beast", cpu_limit="0-15",
            memory_limit=34359738368, disk_limit=107374182400))
        spec = store.get(runner_id)
        assert spec["provider"] == "github"
        assert spec["platform"] == "linux"
        assert spec["cpu_limit"] == "0-15"
        assert spec["memory_limit"] == 34359738368

    def test_json_fields_survive_as_structures(self, store):
        """A caller that had to json.loads() every read would eventually
        forget to, and a label list would render as a string of brackets."""
        runner_id = store.create(**a_spec(
            labels=["self-hosted", "linux"],
            capabilities={"job_containers": True}))
        spec = store.get(runner_id)
        assert spec["labels"] == ["self-hosted", "linux"]
        assert spec["capabilities"]["job_containers"] is True

    def test_a_new_spec_starts_at_version_one(self, store):
        assert store.get(store.create(**a_spec()))["spec_version"] == 1

    def test_created_at_is_recorded(self, store):
        assert store.get(store.create(**a_spec()))["created_at"].endswith("Z")

    def test_an_unknown_id_is_none_not_an_error(self, store):
        assert store.get("not-a-runner") is None

    def test_a_spec_needs_a_provider_and_a_platform(self, store):
        with pytest.raises(ValueError, match="needs"):
            store.create(display_name="nameless")

    def test_an_unknown_column_is_refused(self, store):
        """Silently dropping it would leave the caller believing it was
        stored."""
        with pytest.raises(ValueError, match="not settable"):
            store.create(**a_spec(favourite_colour="green"))


class TestConcurrentChangeIsRefused:
    def test_an_update_bumps_the_version(self, store):
        runner_id = store.create(**a_spec())
        assert store.update(runner_id, 1, display_name="renamed") == 2
        assert store.get(runner_id)["spec_version"] == 2

    def test_a_stale_version_is_refused(self, store):
        """Two operators on one dashboard is the ordinary case."""
        runner_id = store.create(**a_spec())
        store.update(runner_id, 1, display_name="first")

        with pytest.raises(StaleSpec) as caught:
            store.update(runner_id, 1, display_name="second")

        assert caught.value.expected == 1
        assert caught.value.actual == 2

    def test_a_refused_update_changes_nothing(self, store):
        runner_id = store.create(**a_spec())
        store.update(runner_id, 1, display_name="first")
        with pytest.raises(StaleSpec):
            store.update(runner_id, 1, display_name="second")
        assert store.get(runner_id)["display_name"] == "first"

    def test_an_update_to_a_missing_spec_says_so(self, store):
        """Distinguished from a stale one: they need different responses."""
        with pytest.raises(UnknownSpec):
            store.update("not-a-runner", 1, display_name="x")

    def test_a_caller_cannot_write_the_version_itself(self, store):
        """`spec_version` is a positional parameter, so the name is already
        taken and Python refuses the call before any check runs. That is a
        stronger guarantee than a validation branch, and it is asserted here so
        a later signature change cannot quietly give the name back."""
        runner_id = store.create(**a_spec())
        with pytest.raises(TypeError, match="multiple values"):
            store.update(runner_id, 1, spec_version=99)

    def test_a_caller_cannot_rewrite_when_a_spec_was_born_or_died(self, store):
        """created_at and deleted_at are the store's bookkeeping. A caller that
        could set them could make a spec look older than its history."""
        runner_id = store.create(**a_spec())
        for field in ("created_at", "deleted_at"):
            with pytest.raises(ValueError, match="not settable"):
                store.update(runner_id, 1, **{field: "2020-01-01T00:00:00Z"})

    def test_an_empty_update_is_refused(self, store):
        runner_id = store.create(**a_spec())
        with pytest.raises(ValueError, match="no changes"):
            store.update(runner_id, 1)


class TestSoftDelete:
    def test_a_deleted_spec_is_still_readable(self, store):
        """History refers to it; a referent that cannot be read is not one."""
        runner_id = store.create(**a_spec(display_name="github-runner-9"))
        store.soft_delete(runner_id)
        spec = store.get(runner_id)
        assert spec is not None
        assert spec["display_name"] == "github-runner-9"
        assert spec["deleted_at"].endswith("Z")

    def test_a_deleted_spec_no_longer_wants_to_run(self, store):
        """Otherwise the controller reconciles it straight back into being."""
        runner_id = store.create(**a_spec())
        store.soft_delete(runner_id)
        assert store.get(runner_id)["desired_state"] == "absent"

    def test_a_deleted_spec_is_out_of_the_default_listing(self, store):
        live = store.create(**a_spec(display_name="alive"))
        gone = store.create(**a_spec(display_name="gone"))
        store.soft_delete(gone)
        listed = [s["runner_id"] for s in store.list()]
        assert live in listed and gone not in listed

    def test_it_can_be_asked_for(self, store):
        gone = store.create(**a_spec())
        store.soft_delete(gone)
        assert gone in [s["runner_id"]
                        for s in store.list(include_deleted=True)]

    def test_deleting_a_missing_spec_says_so(self, store):
        with pytest.raises(UnknownSpec):
            store.soft_delete("not-a-runner")


class TestListing:
    def test_it_filters_on_a_column(self, store):
        store.create(**a_spec(provider="github"))
        store.create(**a_spec(provider="forgejo"))
        assert len(store.list(provider="forgejo")) == 1

    def test_an_unknown_filter_is_refused(self, store):
        """These reach a WHERE clause, so the column list is the allowlist."""
        with pytest.raises(ValueError, match="not a filterable column"):
            store.list(favourite_colour="green")


class TestTheSchemaMatchesTheDesign:
    """Section 11.1 lists twenty-six columns. All of them, by name and type."""

    EXPECTED = {
        "runner_id": "TEXT", "display_name": "TEXT", "provider": "TEXT",
        "platform": "TEXT", "architecture": "TEXT",
        "runtime_template": "TEXT", "host_id": "TEXT", "labels": "TEXT",
        "runner_group": "TEXT", "cpu_limit": "TEXT",
        "memory_limit": "INTEGER", "disk_limit": "INTEGER",
        "cache_policy": "TEXT", "desired_state": "TEXT",
        "actual_state": "TEXT", "registration_id": "TEXT",
        "registration_uuid": "TEXT", "created_at": "TEXT",
        "last_seen_at": "TEXT", "current_operation": "TEXT",
        "last_error": "TEXT", "capabilities": "TEXT",
        "exec_unit_ref": "TEXT", "fleet_id": "TEXT",
        "spec_version": "INTEGER", "deleted_at": "TEXT",
    }

    def columns(self, store, table="runner_specs"):
        with schema.connect(store.path) as c:
            return {r["name"]: r["type"]
                    for r in c.execute(f"PRAGMA table_info({table})")}

    def test_every_field_of_section_11_1_exists_with_its_type(self, store):
        assert self.columns(store) == self.EXPECTED

    def test_the_other_four_tables_exist(self, store):
        with schema.connect(store.path) as c:
            names = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"workers", "fleets", "operations", "audit"} <= names

    def test_operations_can_be_made_idempotent(self, store):
        """The UNIQUE key is the whole retry mechanism: a repeated request
        finds the first operation instead of starting a second one."""
        with schema.connect(store.path) as c:
            c.execute("INSERT INTO operations (operation_id, idempotency_key,"
                      " verb, requested_at) VALUES ('op1', 'k', 'create', 'n')")
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO operations (operation_id,"
                          " idempotency_key, verb, requested_at)"
                          " VALUES ('op2', 'k', 'create', 'n')")

    def test_foreign_keys_are_enforced_on_every_connection(self, store):
        """SQLite defaults this OFF per connection, so a connection that
        forgets the pragma accepts a spec pointing at a fleet that is not
        there."""
        with schema.connect(store.path) as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO runner_specs (runner_id, provider,"
                          " platform, created_at, fleet_id)"
                          " VALUES ('r1', 'github', 'linux', 'n', 'nope')")


class TestNoColumnHoldsASecret:
    """T-0201's security line, checked rather than promised.

    A registration token lives for minutes and is a credential. What this table
    keeps is the forge's id for a runner, which is not one. The list of names
    comes from providers, so a token added there is caught here too.
    """

    def test_no_column_is_named_after_a_redacted_field(self, store):
        with schema.connect(store.path) as c:
            tables = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                names = {r["name"].lower()
                         for r in c.execute(f"PRAGMA table_info({table})")}
                clash = names & {f.lower() for f in providers.REDACTED_FIELDS}
                assert clash == set(), f"{table} holds {clash}"

    def test_no_column_name_mentions_a_token(self, store):
        with schema.connect(store.path) as c:
            tables = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                for row in c.execute(f"PRAGMA table_info({table})"):
                    assert "token" not in row["name"].lower(), (
                        f"{table}.{row['name']}")


class TestInitIsSafeToRepeat:
    def test_running_it_twice_keeps_the_data(self, store):
        runner_id = store.create(**a_spec())
        schema.init(store.path)
        assert store.get(runner_id) is not None


class TestAControlDatabaseFromBeforeTheLeaseTable:
    """The lease table arrived with the reconciler (T-0302), after the store.

    A new table needs no ALTER - CREATE TABLE IF NOT EXISTS creates it in a
    database that lacks it - but "needs no migration" is a claim, and the plan
    asks for every schema change to be shown to migrate, not asserted to.
    """

    def test_init_adds_the_table_and_keeps_the_specs(self, tmp_path):
        path = str(tmp_path / "control.db")
        schema.init(path)
        store = SpecStore(path)
        runner_id = store.create(**a_spec())
        with schema.connect(path) as c:
            c.execute("DROP TABLE leases")

        schema.init(path)

        with schema.connect(path) as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "leases" in tables
        assert store.get(runner_id) is not None
