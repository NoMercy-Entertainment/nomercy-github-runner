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

    def test_new_sample_wakes_sender_before_next_period(self, sender):
        sender.interval = 1
        thread = threading.Thread(target=sender._loop, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 1
            while not sender.link.posted and time.monotonic() < deadline:
                time.sleep(0.005)
            assert sender.link.posted[-1]["measuring"] is True
            sender.measure_once()
            deadline = time.monotonic() + 0.5
            while len(sender.link.posted) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert "instances" in sender.link.posted[-1]
        finally:
            sender.stop()

    def test_slow_measurement_does_not_add_a_second_full_interval(self, sender):
        sender.interval = 0.05
        started = []

        def slow():
            started.append(time.monotonic())
            time.sleep(0.08)

        sender.measure_once = slow
        thread = threading.Thread(target=sender._measure_loop, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 0.5
            while len(started) < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert len(started) >= 3
            assert started[1] - started[0] < 0.12
        finally:
            sender.stop()
            thread.join(1)


class TestTheBeatOutlivesOneBadBeat:
    """The thread that beats is the worker's only way of saying it is here.
    If it dies the worker is degraded for ever, and a degraded worker is
    sent nothing destructive - which is a fleet that cannot be managed
    until someone restarts its agent. The controller was recreated on
    2026-09-20 and its worker went silent for nine minutes, until the agent
    was restarted by hand.
    """

    def test_one_that_raises_does_not_end_the_loop(self, sender):
        beats = []

        class Angry:
            def post(self, path, payload):
                beats.append(payload)
                if len(beats) == 1:
                    raise RuntimeError("the controller went away")
                return True

        sender.link = Angry()
        sender.interval = 0.01
        thread = threading.Thread(target=sender._loop, daemon=True)
        thread.start()
        for _ in range(200):
            if len(beats) > 2:
                break
            time.sleep(0.01)
        sender._stop.set()
        thread.join(timeout=2)
        assert len(beats) > 2, "it kept beating"

    def test_a_measurement_that_raises_does_not_end_its_loop(self, sender):
        tries = []

        def angry():
            tries.append(1)
            raise RuntimeError("the engine is not answering")

        sender.agent.runtime.instances = angry
        sender.interval = 0.01
        thread = threading.Thread(target=sender._measure_loop, daemon=True)
        thread.start()
        for _ in range(200):
            if len(tries) > 2:
                break
            time.sleep(0.01)
        sender._stop.set()
        thread.join(timeout=2)
        assert len(tries) > 2


class TestTheDeepBeat:
    def test_deep_measurements_run_separately_and_retain_their_time(self, sender):
        sender.measure_once()
        sender.measure_depth_once()
        sender.measure_once()
        deep = [u for u in sender._measured[1]["instances"]
                if "storage_bytes" in (u.get("telemetry") or {})]
        assert deep
        stamp = deep[0]["telemetry"]["storage_at"]
        sender.measure_once()
        assert sender._measured[1]["instances"][0]["telemetry"]["storage_at"] == stamp

    def test_blocked_storage_does_not_block_fresh_unit_measurements(self, sender):
        sender.measure_once()
        held = threading.Event()
        entered = threading.Event()
        def probe(*args):
            entered.set()
            held.wait(2)
            return {"ok": False}
        sender.agent.runtime.probe = probe
        thread = threading.Thread(target=sender.measure_depth_once, daemon=True)
        thread.start()
        assert entered.wait(1)
        started = time.monotonic()
        sender.measure_once()
        assert time.monotonic() - started < 1
        assert sender._measured[1]["instances"][0]["state"] == "running"
        held.set()
        thread.join(2)
