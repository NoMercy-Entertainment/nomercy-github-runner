"""The workers runners are placed on, and whether they can be trusted today.

Health is the part that matters. It is computed from the last heartbeat rather
than read from a flag, and the difference is not academic: a stored flag stays
`healthy` for ever if whatever was supposed to clear it also died, which is
precisely the failure a health field exists to catch.

The consequence is the gate. Unreachable and absent look identical from here,
and removing a runner on that evidence deletes capacity that was only out of
touch - so no destructive verb goes to a degraded worker.

A separate file from `test_runner_service.py`, which the plan named for both.
The inventory answers a different question from the service and mixing them
would make one long file about two subjects.
"""
from datetime import datetime, timedelta, timezone

import pytest

from control import inventory as inv
from control.inventory import Inventory, UnknownWorker
from store import schema


@pytest.fixture
def workers(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return Inventory(path)


def at(seconds_ago=0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


class TestRegistering:
    def test_a_worker_can_be_added(self, workers):
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX)
        assert workers.get("linux-worker-1")["kind"] == inv.HYPERV_LINUX

    def test_it_starts_unknown_rather_than_healthy(self, workers):
        """Nothing has answered yet. Calling that healthy would be a claim
        about a machine nobody has heard from."""
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX)
        assert workers.health("linux-worker-1") == inv.UNKNOWN

    def test_registering_twice_updates_rather_than_duplicates(self, workers):
        """An agent re-announces itself after every restart. A second row for
        one machine would let the scheduler place two runners where there is
        room for one."""
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX,
                                agent_version="1.0")
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX,
                                agent_version="1.1")

        assert len(workers.list()) == 1
        assert workers.get("linux-worker-1")["agent_version"] == "1.1"

    def test_re_registering_does_not_wipe_the_last_heartbeat(self, workers):
        """A restarting agent is not a machine that stopped answering."""
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX)
        workers.heartbeat("linux-worker-1")
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX,
                                agent_version="1.1")
        assert workers.health("linux-worker-1") == inv.HEALTHY

    def test_capabilities_survive_as_a_structure(self, workers):
        workers.register_worker("linux-worker-1", inv.HYPERV_LINUX,
                                capabilities={"job_containers": True})
        assert workers.get(
            "linux-worker-1")["capabilities"]["job_containers"] is True

    def test_an_invented_kind_is_refused(self, workers):
        with pytest.raises(ValueError, match="unknown worker kind"):
            workers.register_worker("mystery", "quantum-host")

    def test_there_is_no_apple_host_kind(self, workers):
        """The macOS appliance is an execution unit of the Linux worker. A
        second worker kind for it is the first place `uniform` would quietly
        stop being true."""
        assert "apple-host" not in inv.KINDS
        assert set(inv.KINDS) == {"hyperv-linux", "hyperv-windows"}

    def test_an_unknown_worker_cannot_heartbeat(self, workers):
        with pytest.raises(UnknownWorker):
            workers.heartbeat("never-registered")


class TestHealthIsComputedNotStored:
    def test_a_recent_beat_is_healthy(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w", at=at(1))
        assert workers.health("w") == inv.HEALTHY

    def test_three_missed_beats_is_degraded(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w", at=at(60))
        assert workers.health("w") == inv.DEGRADED

    def test_it_degrades_by_the_passage_of_time_alone(self, workers):
        """The property a stored flag cannot have. Nothing runs in between;
        the worker simply stops being healthy because time passed."""
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w")

        assert workers.health("w") == inv.HEALTHY
        later = datetime.now(timezone.utc) + timedelta(seconds=60)
        assert workers.health("w", now=later) == inv.DEGRADED

    def test_the_boundary_is_three_beats(self, workers):
        """Pinned on both sides with an explicit `now`, because timestamps are
        stored to the second: reading the wall clock here would compare a beat
        against a moment a fraction of a second later and make the boundary
        itself look wrong."""
        workers.register_worker("w", inv.HYPERV_LINUX)
        beat = datetime.now(timezone.utc).replace(microsecond=0)
        workers.heartbeat("w", at=beat)
        window = timedelta(
            seconds=inv.HEARTBEAT_SECONDS * inv.MISSED_BEATS_BEFORE_DEGRADED)

        assert workers.health("w", now=beat + window) == inv.HEALTHY
        assert workers.health(
            "w", now=beat + window + timedelta(seconds=1)) == inv.DEGRADED

    def test_a_worker_that_never_answered_is_unknown_not_degraded(self,
                                                                  workers):
        """Never heard from and stopped answering are different problems."""
        workers.register_worker("w", inv.HYPERV_LINUX)
        assert workers.health("w") == inv.UNKNOWN

    def test_health_of_a_missing_worker_is_an_error(self, workers):
        with pytest.raises(UnknownWorker):
            workers.health("not-a-worker")


class TestNoDestructiveVerbToADegradedWorker:
    """The gate, stated once instead of remembered at each call site."""

    def test_a_healthy_worker_accepts_them(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w")
        assert workers.accepts_destructive_verbs("w") is True

    def test_a_degraded_one_does_not(self, workers):
        """Unreachable and absent look the same from here, and removing a
        runner on that evidence deletes capacity that was only out of touch."""
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w", at=at(60))
        assert workers.accepts_destructive_verbs("w") is False

    def test_nor_does_one_that_has_never_answered(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX)
        assert workers.accepts_destructive_verbs("w") is False


class TestListing:
    def test_workers_can_be_listed_by_kind(self, workers):
        workers.register_worker("linux-1", inv.HYPERV_LINUX)
        workers.register_worker("win-1", inv.HYPERV_WINDOWS)
        assert len(workers.list(kind=inv.HYPERV_WINDOWS)) == 1

    def test_only_healthy_workers_can_be_given_work(self, workers):
        workers.register_worker("good", inv.HYPERV_LINUX)
        workers.register_worker("gone", inv.HYPERV_LINUX)
        workers.heartbeat("good")
        workers.heartbeat("gone", at=at(60))

        assert [w["host_id"] for w in workers.healthy()] == ["good"]

    def test_healthy_can_be_narrowed_to_a_kind(self, workers):
        workers.register_worker("linux-1", inv.HYPERV_LINUX)
        workers.register_worker("win-1", inv.HYPERV_WINDOWS)
        workers.heartbeat("linux-1")
        workers.heartbeat("win-1")
        assert len(workers.healthy(kind=inv.HYPERV_WINDOWS)) == 1

    def test_an_empty_inventory_is_not_an_error(self, workers):
        assert workers.list() == []
        assert workers.healthy() == []


class TestHeartbeatCarriesWhatChanged:
    def test_it_can_update_the_agent_version(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX, agent_version="1.0")
        workers.heartbeat("w", agent_version="1.2")
        assert workers.get("w")["agent_version"] == "1.2"

    def test_it_can_update_capabilities(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX)
        workers.heartbeat("w", capabilities={"nested_builds": False})
        assert workers.get("w")["capabilities"]["nested_builds"] is False

    def test_it_leaves_alone_what_it_does_not_carry(self, workers):
        workers.register_worker("w", inv.HYPERV_LINUX, agent_version="1.0",
                                endpoint="https://worker:9443")
        workers.heartbeat("w")
        after = workers.get("w")
        assert after["agent_version"] == "1.0"
        assert after["endpoint"] == "https://worker:9443"


class TestSummaryHardware:
    """The page shows each worker's hardware beside its health, so the
    maximum a limit may be is visible where the limit is set."""

    def test_the_summary_carries_cores_memory_and_the_resolved_hardware(self, workers):
        workers.register_worker("rnr-linux-1", inv.HYPERV_LINUX, capabilities={
            "kind": "linux-container", "host_cores": 56,
            "memory_total_bytes": 84418977792, "memory_bytes": 24 * 1024**3})
        workers.heartbeat("rnr-linux-1")
        resources = workers.summary()[0]["resources"]
        assert resources["host_cores"] == 56
        assert resources["memory_total_bytes"] == 84418977792
        assert resources["hardware"]["cpus"] == 56
        assert resources["hardware"]["memory_bytes"] == 84418977792
        assert resources["hardware"]["memory_source"] == "measured"

    def test_a_worker_that_reports_nothing_reads_unknown(self, workers):
        workers.register_worker("macos-appliance-1", inv.HYPERV_LINUX)
        resources = workers.summary()[0]["resources"]
        assert resources["host_cores"] is None
        assert resources["hardware"]["cpus"] is None
        assert resources["hardware"]["memory_bytes"] is None
