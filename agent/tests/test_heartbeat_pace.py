"""A beat goes out on time, whatever the engine is doing.

Three missed beats and the controller marks the worker degraded, and a
degraded worker is sent nothing destructive (design 13.4). Measuring is the
slow part of a beat: `docker stats` over every running unit, and every
thirtieth beat a storage and cache probe of each one - which asks a nested
engine and can take a minute per unit on a busy worker.

On 2026-09-20 that silence read as an absence in the middle of a rebuild.
The worker went "degraded - no heartbeat for 344s" while it was measuring,
its removal was refused for exactly that, and the runner being rebuilt had
already been drained, deregistered and left running with nobody managing
it. So measuring happens on its own thread, and a beat carries the last
measurement while it is fresh, or nothing at all.
"""
import threading
import time

import pytest

from agent import heartbeat as hb

from .test_heartbeat_telemetry import agent_with_two_units


class FakeLink:
    def __init__(self):
        self.posted = []

    def post(self, path, payload):
        self.posted.append(payload)
        return True


@pytest.fixture
def sender():
    agent, _ = agent_with_two_units()
    sender = hb.HeartbeatSender(agent, "https://control:8444/beat",
                                ssl_context=None, interval=0.05)
    sender.link = FakeLink()
    return sender


class TestABeatIsNeverHeldUpByMeasuring:
    def test_one_sent_before_anything_was_measured_says_so(self, sender):
        sender.send_once()
        beat = sender.link.posted[-1]
        assert beat["measuring"] is True
        assert "instances" not in beat
        assert beat["host_id"] == sender.agent.host_id

    def test_a_slow_runtime_delays_the_measurement_and_not_the_beat(
            self, sender):
        held = threading.Event()
        instances = sender.agent.runtime.instances

        def slow():
            held.wait(5)
            return instances()

        sender.agent.runtime.instances = slow
        measuring = threading.Thread(target=sender.measure_once, daemon=True)
        measuring.start()
        started = time.monotonic()
        sender.send_once()
        assert time.monotonic() - started < 1
        assert sender.link.posted[-1]["measuring"] is True
        held.set()
        measuring.join(timeout=5)

    def test_a_fresh_measurement_goes_with_the_beat(self, sender):
        sender.measure_once()
        sender.send_once()
        beat = sender.link.posted[-1]
        assert "measuring" not in beat
        assert len(beat["instances"]) == 2

    def test_one_too_old_is_not_sent_as_new(self, sender):
        sender.measure_once()
        stale, payload = sender._measured
        sender._measured = (stale - hb.STALE_AFTER - 1, payload)
        sender.send_once()
        assert sender.link.posted[-1]["measuring"] is True

    def test_the_beat_is_stamped_when_it_is_sent(self, sender):
        sender.measure_once()
        measured_at = sender._measured[1]["sent_at"]
        sender._measured = (sender._measured[0],
                            dict(sender._measured[1], sent_at="old"))
        sender.send_once()
        assert sender.link.posted[-1]["sent_at"] != "old"
        assert measured_at


class TestTheDeepBeat:
    def test_the_first_measurement_is_deep_and_the_next_are_not(self, sender):
        sender.measure_once()
        first = sender.link.posted
        assert sender.measured == 1
        deep = [u for u in sender._measured[1]["instances"]
                if "storage_bytes" in (u.get("telemetry") or {})]
        assert deep, "the first measurement measures storage"
        sender.measure_once()
        shallow = [u for u in sender._measured[1]["instances"]
                   if "storage_bytes" in (u.get("telemetry") or {})]
        assert not shallow and first == []
