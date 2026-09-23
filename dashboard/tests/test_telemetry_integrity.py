from datetime import timedelta

import pytest

import cards
from control.inventory import _telemetry
from tests.test_telemetry_health import NOW, at, spec


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, 10 ** 400])
def test_nonphysical_measurements_are_not_accepted(value):
    assert "cpu_percent" not in _telemetry({"cpu_percent": value})


def test_old_storage_does_not_look_fresh_because_cpu_keeps_arriving():
    telemetry = {"at": at(1), "cpu_percent": 100, "storage_bytes": 999,
                 "cache_bytes": 99, "storage_at": at(900), "cache_at": at(900)}
    card = cards.from_spec(spec(telemetry=telemetry), now=NOW)
    assert card["cpu"]["percent"] == 100
    assert card["storage"] is None and card["cache"] is None


def test_card_uses_measured_cache_cap_not_github_default():
    telemetry = {"at": at(1), "cache_at": at(4), "cache_bytes": 10,
                 "cache_cap_bytes": 20 * 10 ** 9}
    card = cards.from_spec(spec(telemetry=telemetry), now=NOW)
    assert card["cache"]["cap_bytes"] == 20 * 10 ** 9


def test_unknown_cache_cap_stays_unknown():
    card = cards.from_spec(spec(telemetry={"cache_bytes": 10, "cache_at": at(4)}), now=NOW)
    assert card["cache"]["cap_bytes"] is None


def test_busy_card_uses_stored_recent_job_and_drops_stale_name():
    runner = spec(actual_state="busy", forge_state="busy",
                  telemetry={"job": "build app", "at": at(5)})
    assert cards.from_spec(runner, now=NOW)["job"] == "build app"
    card = cards.from_spec(runner, now=NOW + timedelta(minutes=2))
    assert card["job"] != "build app"


def test_future_telemetry_is_not_evidence_of_live_usage():
    card = cards.from_spec(spec(telemetry={"at": at(-100), "cpu_percent": 80}), now=NOW)
    assert card["cpu"]["percent"] is None


# ---------------------------------------------------------------------------
# T-27: a storage figure says what it is a share of
# ---------------------------------------------------------------------------
# The bug read live, 2026-09-24: a Windows runner with its own 100 GB disk
# showed "STORAGE 0.12 GB", which an operator reasonably read as 0.12 GB
# free. It was 0.12 GB used, of a disk no total ever named.

def test_a_runner_with_a_disk_limit_reports_its_share_of_it():
    """The spec's own `disk_limit` - what the controller told this runner's
    disk to be, known before any heartbeat about it ever could be - fills
    the total whenever a beat's `disk_limit_bytes` has not (yet) arrived."""
    telemetry = {"at": at(1), "storage_bytes": 12 * 10 ** 7, "storage_at": at(1)}
    card = cards.from_spec(spec(disk_limit=100 * 10 ** 9, telemetry=telemetry),
                           now=NOW)
    assert card["storage"] == {"used_bytes": 12 * 10 ** 7,
                               "total_bytes": 100 * 10 ** 9}


def test_a_measured_disk_limit_is_preferred_over_the_configured_one():
    """What the agent actually measured wins over what was merely asked for,
    the same precedence a beat's other figures already have."""
    telemetry = {"at": at(1), "storage_bytes": 12 * 10 ** 7, "storage_at": at(1),
                 "disk_limit_bytes": 99 * 10 ** 9}
    card = cards.from_spec(spec(disk_limit=100 * 10 ** 9, telemetry=telemetry),
                           now=NOW)
    assert card["storage"]["total_bytes"] == 99 * 10 ** 9


def test_a_runner_without_a_disk_of_its_own_keeps_todays_behaviour():
    """No `disk_limit` on the spec and no `disk_limit_bytes` on the beat: a
    used figure, and no total - exactly as before this fix."""
    telemetry = {"at": at(1), "storage_bytes": 12 * 10 ** 7, "storage_at": at(1)}
    card = cards.from_spec(spec(telemetry=telemetry), now=NOW)
    assert card["storage"] == {"used_bytes": 12 * 10 ** 7, "total_bytes": None}
