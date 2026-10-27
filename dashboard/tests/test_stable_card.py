"""A card that keeps its shape and its last known figures (2026-10-27).

What the owner saw: the Storage line alternating between
"0.71 GiB own · 0.64 GiB / 100 GiB" (two lines) and "0.71 GiB / 100 GiB",
CPU and Memory flipping to "unknown" whenever a beat carried nulls, and
"runner is busy - job details unavailable" while the job was known. Every
card below it moved each time.

Three levels: the controller's merge keeps a reading until the next
reading; the card shows a kept reading with its age while the runner is up;
and the renderer draws the same lines in the same slots whatever the
telemetry, the state or the messages.
"""
import re
from datetime import timedelta

import pytest

import cards
from tests.test_generic_card import NODE, render, spec_cards
from tests.test_telemetry_health import NOW, at, placed, spec  # noqa: F401

GIB = 1024 ** 3


def beat(service, rid, telemetry, when=None):
    service.inventory.accept_heartbeat("linux-1", {
        "host_id": "linux-1",
        "instances": [{"runner_id": rid, "state": "running",
                       "telemetry": telemetry}]}, at=when)


NULL_BEAT = {"cpu_percent": None, "mem_used_bytes": None,
             "mem_limit_bytes": None, "cpu_cores": None, "host_cores": None,
             "host_mem_bytes": None, "storage_volume_used_bytes": None,
             "storage_volume_total_bytes": None}


class TestTheMergeKeepsReadings:
    def test_a_beat_of_nulls_keeps_the_last_readings_and_their_time(self, placed):
        service, rid = placed
        first = NOW - timedelta(seconds=40)
        beat(service, rid, {"cpu_percent": 120.0, "mem_used_bytes": 3 * GIB,
                            "cpu_cores": 4, "mem_limit_bytes": 8 * GIB,
                            "host_cores": 4, "host_mem_bytes": 8 * GIB,
                            "storage_volume_used_bytes": 30 * GIB,
                            "storage_volume_total_bytes": 100 * GIB}, when=first)
        beat(service, rid, dict(NULL_BEAT), when=NOW)
        t = service.specs.get(rid)["telemetry"]
        assert t["cpu_percent"] == 120.0 and t["mem_used_bytes"] == 3 * GIB
        assert t["cpu_at"] == t["mem_at"] == "2026-09-18T05:59:20Z"
        assert t["at"] == "2026-09-18T06:00:00Z"
        assert (t["cpu_cores"], t["mem_limit_bytes"]) == (4, 8 * GIB)
        assert t["storage_volume_total_bytes"] == 100 * GIB

    def test_a_new_reading_replaces_the_old_one(self, placed):
        service, rid = placed
        beat(service, rid, {"cpu_percent": 120.0}, when=NOW - timedelta(seconds=40))
        beat(service, rid, {"cpu_percent": 5.0}, when=NOW)
        t = service.specs.get(rid)["telemetry"]
        assert t["cpu_percent"] == 5.0 and t["cpu_at"] == "2026-09-18T06:00:00Z"

    def test_a_beat_that_read_something_still_says_a_limit_is_gone(self, placed):
        service, rid = placed
        beat(service, rid, {"cpu_percent": 1.0, "cpu_cores": 4})
        beat(service, rid, {"cpu_percent": 1.0, "cpu_cores": None})
        assert service.specs.get(rid)["telemetry"]["cpu_cores"] is None


class TestTheCardShowsTheLastKnownReading:
    def test_a_kept_reading_is_shown_with_its_age_while_the_runner_is_up(self):
        runner = spec(telemetry={"at": at(2), "cpu_percent": 80.0,
                                 "cpu_at": at(70), "mem_used_bytes": 5,
                                 "mem_at": at(2)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert (card["cpu"]["percent"], card["cpu"]["age_s"]) == (80.0, 70)
        assert (card["memory"]["used_bytes"], card["memory"]["age_s"]) == (5, None)

    def test_a_runner_that_stopped_beating_has_no_reading(self):
        runner = spec(last_seen_at=at(300),
                      telemetry={"at": at(300), "cpu_percent": 80.0})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["cpu"]["percent"] is None
        assert card["reachable"] is False

    def test_a_stopped_unit_does_not_show_its_running_figures(self):
        runner = spec(unit_state="stopped",
                      telemetry={"at": at(2), "cpu_percent": 80.0, "cpu_at": at(60)})
        assert cards.from_spec(runner, now=NOW)["cpu"]["percent"] is None

    def test_a_reading_older_than_the_window_is_not_shown(self):
        runner = spec(telemetry={"at": at(2), "cpu_percent": 80.0,
                                 "cpu_at": at(cards.LAST_KNOWN + 60)})
        assert cards.from_spec(runner, now=NOW)["cpu"]["percent"] is None


def _event(state, seconds_ago, job="build"):
    return {"state": state, "job": job, "at": at(seconds_ago),
            "run_id": "1", "source": "worker"}


class TestTheNewestEvidenceNamesTheJob:
    def test_a_job_started_after_the_forge_said_idle_is_shown_busy_at_once(self):
        runner = spec(forge_state="idle", forge_seen_at=at(20),
                      telemetry={"at": at(2), "job_event": _event("started", 5)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert (card["state"], card["job"]) == ("busy", "build")

    def test_an_idle_runner_never_shows_a_start_whose_completion_was_lost(self):
        runner = spec(forge_state="idle", forge_seen_at=at(5),
                      telemetry={"at": at(2), "job_event": _event("started", 60),
                                 "job_event_received_at": at(60)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert (card["state"], card["job"]) == ("idle", None)

    def test_the_forge_saying_idle_after_the_start_is_idle(self):
        runner = spec(forge_state="idle", forge_seen_at=at(5),
                      telemetry={"at": at(2), "job_event": _event("started", 20)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["state"] == "idle"

    def test_a_completion_the_forge_has_not_caught_up_with_is_finishing(self):
        runner = spec(actual_state="busy", forge_state="busy", forge_seen_at=at(5),
                      telemetry={"at": at(2), "job_event": _event("finished", 10)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["state"] == "busy"
        assert card["job"] == "build - finishing"

    def test_busy_long_after_the_completion_is_another_job_not_the_old_one(self):
        runner = spec(actual_state="busy", forge_state="busy", forge_seen_at=at(5),
                      telemetry={"at": at(2), "job_event": _event(
                          "finished", cards.FINISHING_FOR + 60)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["job"] == cards.UNNAMED_JOB

    def test_a_completion_the_forge_lags_behind_never_reads_unavailable(self):
        """The forge saw the runner busy 20 s after the completion arrived:
        the runner is still releasing that job (Windows, 2026-10-27)."""
        runner = spec(actual_state="busy", forge_state="busy", forge_seen_at=at(1),
                      telemetry={"at": at(2), "job_event": _event("finished", 21),
                                 "job_event_received_at": at(21)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["job"] == "build - finishing"

    def test_the_controllers_arrival_time_wins_over_a_fast_worker_clock(self):
        """The Windows guest's clock ran 17 s ahead: its start stamped after
        the forge's idle, though it arrived before it."""
        runner = spec(forge_state="idle", forge_seen_at=at(5),
                      telemetry={"at": at(2), "job_event": _event("started", -12),
                                 "job_event_received_at": at(10)})
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert card["state"] == "idle"
        runner["telemetry"]["job_event_received_at"] = at(1)
        card = cards.from_spec(runner, worker_reachable=True, now=NOW)
        assert (card["state"], card["job"]) == ("busy", "build")


# ---------------------------------------------------------------------------
# the renderer: the same lines in the same slots, whatever the card says
# ---------------------------------------------------------------------------

def skeleton(html):
    """The card's structure: every tag and class, none of the text and
    none of the attributes that carry it."""
    # A button and an empty slot are one cell of the action grid; the
    # messages are the content of the one message slot.
    html = re.sub(r'<span class="aslot"[^>]*></span>', "<button></button>", html)
    html = re.sub(r'<span class="(cerr|cwarn|cnote)">[^<]*</span>', "", html)
    tags = re.findall(r"<(/?)(\w+)([^>]*)>", html)
    out = []
    for close, tag, attrs in tags:
        if tag == "button":
            out.append(close + "slot")
            continue
        cls = re.search(r'class="([^"]*)"', attrs)
        name = cls.group(1) if cls else ""
        name = re.sub(r"\b(fill) (crit|warn)\b", r"\1", name)
        name = re.sub(r"\bbadge \w+", "badge", name)
        name = re.sub(r"\bcjob none\b", "cjob", name)
        out.append(close + tag + "." + name)
    return out


def variants():
    base = dict(spec_cards()[2], job=None, last_error=None, label_drift=None,
                last_note=None, current_operation=None, readiness_note=None)
    busy = dict(base, state="busy", job="a job with a very long name " * 6)
    unnamed = dict(base, state="busy")
    errored = dict(base, last_error="the unit could not be started", state="failed")
    noted = dict(base, last_note="registered with labels of its own",
                 current_operation="op-1",
                 readiness_note="GitHub registration status is unverified")
    stopped = dict(base, state="stopped", actions=[
        dict(a, visible=a["verb"] in ("start", "recreate", "remove", "clear_cache", "logs"))
        for a in base["actions"]])
    shared = dict(base, storage={"used_bytes": 1, "total_bytes": 100 * GIB, "shared": True,
                                 "volume_used_bytes": 50 * GIB,
                                 "volume_total_bytes": 100 * GIB},
                  cpu=dict(base["cpu"], percent=None, age_s=None))
    aged = dict(base, cpu=dict(base["cpu"], age_s=75, total_cores=8))
    return [base, busy, unnamed, errored, noted, stopped, shared, aged]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTheSameShapeWhateverItSays:
    def test_every_variant_draws_the_same_skeleton(self):
        htmls = render("cardHTML", variants())
        first = skeleton(htmls[0])
        for html in htmls[1:]:
            assert skeleton(html) == first

    def test_the_storage_detail_is_one_format_with_or_without_the_volume(self):
        base = variants()[0]
        own = dict(base, storage={"used_bytes": int(0.71 * GIB),
                                  "total_bytes": 100 * GIB, "shared": False,
                                  "volume_used_bytes": None, "volume_total_bytes": None})
        with_volume = dict(own, storage=dict(own["storage"],
                                             volume_used_bytes=int(0.64 * GIB),
                                             volume_total_bytes=100 * GIB))
        a, b = render("cardHTML", [own, with_volume])
        detail = lambda h: re.search(r'data-m="storage".*?class="mdetail"[^>]*>([^<]*)<', h).group(1)
        assert detail(a) == "0.71 GiB / 100 GiB"
        assert detail(b) == "0.64 GiB / 100 GiB"
        assert " own · " not in detail(b)

    def test_a_last_known_reading_says_how_old_it_is(self):
        aged = variants()[-1]
        html = render("cardHTML", [aged])[0]
        assert re.search(r'data-m="cpu".*?class="msub">75 s ago<', html)
        assert 'mval">unknown' not in html.split('data-m="memory"')[0]

    def test_cores_in_use_always_have_one_decimal(self):
        base = variants()[0]
        zero = dict(base, cpu=dict(base["cpu"], percent=0))
        html = render("cardHTML", [zero])[0]
        assert re.search(r'data-m="cpu".*?class="mdetail"[^>]*>0\.0 / ', html)


def test_a_kept_reading_is_not_recorded_as_a_new_history_sample(tmp_path, monkeypatch):
    """The card may show the last known CPU with its age; a job's history
    must not record it again as if it were measured now."""
    import history
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()
    runner = {"runner_id": "r-1", "display_name": "gh-1", "provider": "github",
              "actual_state": "busy"}
    history.open_run("gh-1", "gh-1", "build", "2026-09-18T05:50:00Z", runner_id="r-1")
    kept = dict(runner, telemetry={"at": "2026-09-18T06:00:00Z", "cpu_percent": 80.0,
                                   "cpu_at": "2026-09-18T05:59:00Z", "mem_used_bytes": 5,
                                   "mem_at": "2026-09-18T06:00:00Z"})
    fresh = dict(runner, telemetry={"at": "2026-09-18T06:00:10Z", "cpu_percent": 70.0,
                                    "cpu_at": "2026-09-18T06:00:10Z", "mem_used_bytes": 6,
                                    "mem_at": "2026-09-18T06:00:10Z"})
    history.record_controller_logs(kept, "", when="2026-09-18T06:00:05Z")
    history.record_controller_logs(fresh, "", when="2026-09-18T06:00:15Z")
    with history._conn() as c:
        cpus = [r[0] for r in c.execute("SELECT cpu FROM samples")]
    assert cpus == [70.0]


def test_a_beat_whose_cache_reading_failed_keeps_the_last_one(placed):
    service, rid = placed
    beat(service, rid, {"cpu_percent": 1.0, "cache_bytes": 500, "storage_bytes": 9},
         when=NOW - timedelta(seconds=60))
    beat(service, rid, {"cpu_percent": 2.0, "cache_bytes": None, "storage_bytes": 10},
         when=NOW)
    t = service.specs.get(rid)["telemetry"]
    assert t["cache_bytes"] == 500 and t["cache_at"] == "2026-09-18T05:59:00Z"
    assert t["storage_bytes"] == 10
