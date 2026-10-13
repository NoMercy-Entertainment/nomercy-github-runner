"""Reachable workers alone cannot make unknown/failed cards count as online."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api_v2
import cards
from tests.test_telemetry_health import spec, NOW, at


def test_all_reachable_with_stale_forge_are_unknown_not_nineteen_online():
    runners = [cards.from_spec(spec(runner_id=str(i), forge_seen_at=at(121)),
                               worker_reachable=True, now=NOW) for i in range(19)]
    assert all(c["reachable"] is True and c["state"] == "unknown" for c in runners)
    assert cards.summary(runners) == {"total": 19, "online": 0, "busy": 0, "attention": 19}
    assert all("registration status is unverified" in c["readiness_note"] for c in runners)


@pytest.mark.parametrize("state,forge,age,online", [("idle", "idle", 5, True),
    ("busy", "busy", 5, True), ("idle", "offline", 5, False),
    ("failed", "idle", 5, False), ("draining", "idle", 5, False),
    ("idle", "unknown", 5, False), ("idle", "idle", 121, False)])
def test_online_requires_verified_serving_state(state, forge, age, online):
    card = cards.from_spec(spec(actual_state=state, forge_state=forge, forge_seen_at=at(age)),
                           worker_reachable=True, now=NOW)
    assert card["online"] is online
    assert cards.summary([card])["online"] == int(online)


def test_fresh_forge_busy_updates_display_without_changing_lifecycle():
    card = cards.from_spec(spec(actual_state="idle", forge_state="busy"), worker_reachable=True, now=NOW)
    assert card["state"] == "busy" and card["lifecycle_state"] == "idle"
    assert card["job"] == "runner is busy - job details unavailable"
    assert cards.summary([card])["busy"] == 1


@pytest.mark.parametrize("state", ["draining", "drained"])
@pytest.mark.parametrize("error", [None, "service start was not confirmed: unknown"])
def test_reachable_confirmed_drains_keep_actual_errors_in_attention(state, error):
    card = cards.from_spec(spec(actual_state=state, last_error=error), worker_reachable=True, now=NOW)
    assert card["reachable"] is True and card["readiness"]["ready"] is True
    assert card["attention"] is bool(error)
    assert cards.summary([card])["attention"] == int(bool(error))


def test_api_and_fleet_headings_share_card_summary(monkeypatch):
    runners = [cards.from_spec(spec(fleet_id="github-linux-x64", forge_seen_at=at(121)),
                              worker_reachable=True, now=NOW)]
    monkeypatch.setattr(api_v2, "control_plane", lambda: (None, "not running"))
    monkeypatch.setattr(api_v2, "all_cards", lambda service: runners)
    monkeypatch.setattr(api_v2, "controller_health", lambda: {"running": False})
    monkeypatch.setitem(api_v2._status, "fn", lambda: {})
    snapshot = api_v2.fleet_snapshot()
    fleet = next(f for f in snapshot["fleets"] if f["fleet_id"] == "github-linux-x64")
    assert snapshot["summary"] == fleet["summary"] == cards.summary(runners)
    assert snapshot["summary"]["online"] == 0 and fleet["summary"]["attention"] == 1
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable for actual shared heading renderer")
    script = str(Path(cards.__file__).parent / "templates" / "_card.js")
    code = "const ui=require(process.argv[1]);console.log(ui.fleetHeadHTML(JSON.parse(process.argv[2])));"
    result = subprocess.run([node, "-e", code, script, json.dumps(fleet)],
                            text=True, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "0 online" in result.stdout
