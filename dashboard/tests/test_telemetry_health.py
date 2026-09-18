"""T-1803: per-instance telemetry and health, and `ready` from both halves.

A card shows what the unit uses from the heartbeats that carried it, and it
calls a runner ready only when the unit runs AND the forge shows it online.
The behaviour measured on 2026-09-17 - a process healthy for hours while the
forge had it offline, or could not be asked - reads as `unknown` or
`offline`, never as ready.
"""
from datetime import datetime, timedelta, timezone

import pytest

import cards
from control import inventory as inv
from store import schema
from tests.test_partial_failure import GH, passes, the_runner  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401

NOW = datetime(2026, 9, 18, 6, 0, 0, tzinfo=timezone.utc)


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def spec(**kw):
    base = {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            "provider": "github", "platform": "linux", "architecture": "x64",
            "host_id": "linux-1", "actual_state": "idle",
            "unit_state": "running", "last_seen_at": at(5),
            "forge_state": "idle", "forge_seen_at": at(20)}
    base.update(kw)
    return base


class TestReadyNeedsBothHalves:
    def test_both_up_is_ready(self):
        card = cards.from_spec(spec(), worker_reachable=True, now=NOW)
        assert card["readiness"] == {"process": "up", "forge": "online",
                                     "ready": True}
        assert card["state"] == "idle"

    def test_a_healthy_process_the_forge_cannot_confirm_is_unknown(self):
        """The measured failure: the process is fine, the forge could not be
        asked - that is not ready."""
        card = cards.from_spec(spec(forge_state="unknown",
                                    forge_seen_at=at(900)),
                               worker_reachable=True, now=NOW)
        assert card["readiness"]["ready"] is False
        assert card["state"] == "unknown"

    def test_a_forge_answer_that_is_old_is_not_an_answer(self):
        card = cards.from_spec(spec(forge_seen_at=at(900)), now=NOW)
        assert card["state"] == "unknown"

    def test_the_forge_saying_offline_is_offline(self):
        card = cards.from_spec(spec(forge_state="offline"), now=NOW)
        assert card["state"] == "offline"

    def test_a_silent_worker_is_unknown_and_unreachable(self):
        card = cards.from_spec(spec(last_seen_at=at(300)),
                               worker_reachable=True, now=NOW)
        assert card["readiness"]["process"] == "unknown"
        assert card["reachable"] is False
        assert card["state"] == "unknown"

    def test_a_unit_reported_stopped_is_stopped(self):
        card = cards.from_spec(spec(unit_state="stopped"), now=NOW)
        assert card["state"] == "stopped"

    def test_outside_serving_the_lifecycle_state_is_shown_as_it_is(self):
        card = cards.from_spec(spec(actual_state="registering",
                                    forge_state=None), now=NOW)
        assert card["state"] == "registering"


class TestTheCardShowsWhatTheUnitUses:
    @pytest.fixture
    def placed(self, tmp_path):
        from control.service import RunnerService
        from store.fleets import FleetStore
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed({})
        service = RunnerService(path)
        service.inventory.register_worker("linux-1", inv.HYPERV_LINUX)
        rid = service.planned_ids(service.plan(GH, 1))[0]
        s = service.specs.get(rid)
        service.specs.update(rid, s["spec_version"], host_id="linux-1",
                             actual_state="idle")
        return service, rid

    def beat(self, service, rid, telemetry, when=None):
        service.inventory.accept_heartbeat("linux-1", {
            "host_id": "linux-1",
            "instances": [{"runner_id": rid, "state": "running",
                           "telemetry": telemetry}]}, at=when)

    def test_every_measure_on_the_card_comes_from_a_heartbeat(self, placed):
        service, rid = placed
        self.beat(service, rid, {"cpu_percent": 312.5,
                                 "mem_used_bytes": 7 * 2 ** 30,
                                 "mem_limit_bytes": 32 * 2 ** 30,
                                 "storage_bytes": 90 * 10 ** 9,
                                 "cache_bytes": 12 * 10 ** 9})
        card = cards.from_spec(service.specs.get(rid), worker_reachable=True)
        assert card["cpu"]["percent"] == 312.5
        assert card["memory"] == {"used_bytes": 7 * 2 ** 30,
                                  "limit_bytes": 32 * 2 ** 30}
        assert card["storage"]["used_bytes"] == 90 * 10 ** 9
        assert card["cache"]["used_bytes"] == 12 * 10 ** 9
        assert card["last_seen_at"] == service.specs.get(rid)["last_seen_at"]

    def test_storage_and_cache_are_kept_between_deep_beats(self, placed):
        service, rid = placed
        self.beat(service, rid, {"cpu_percent": 1.0, "cache_bytes": 500})
        self.beat(service, rid, {"cpu_percent": 9.0})
        t = service.specs.get(rid)["telemetry"]
        assert t["cpu_percent"] == 9.0 and t["cache_bytes"] == 500

    def test_old_figures_are_unknown_not_shown_as_current(self, placed):
        service, rid = placed
        self.beat(service, rid, {"cpu_percent": 55.0},
                  when=datetime.now(timezone.utc) - timedelta(minutes=10))
        card = cards.from_spec(service.specs.get(rid))
        assert card["cpu"]["percent"] is None

    def test_a_beat_carries_only_numbers_the_card_can_show(self, placed):
        service, rid = placed
        self.beat(service, rid, {"cpu_percent": "lots", "evil": "<script>",
                                 "mem_used_bytes": 5})
        t = service.specs.get(rid)["telemetry"]
        assert "evil" not in t and t["cpu_percent"] is None
        assert t["mem_used_bytes"] == 5


class TestTheReconcilerRecordsWhatTheForgeSaid:
    def test_an_answer_is_recorded_with_its_time(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        s = the_runner(service)
        assert s["forge_state"] == "idle" and s["forge_seen_at"]

    def test_no_answer_is_unknown_and_keeps_the_last_time(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(GH)
        passes(service, reconciler)
        before = the_runner(service)["forge_seen_at"]
        forges.records = lambda provider: None      # the forge is down
        passes(service, reconciler, 2)
        s = the_runner(service)
        assert s["forge_state"] == "unknown"
        assert s["forge_seen_at"] == before
