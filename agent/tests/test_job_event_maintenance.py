"""Storage maintenance waits for actual native initialization/handle release."""
import threading

import pytest

from agent import job_events
from agent.job_events import JobEventSender, _RunnerSource
from agent.verbs import Agent, dispatch

RID = "11111111-1111-4111-8111-111111111111"


class StorageRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.storage = threading.RLock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.maintained = threading.Event()

    def job_event_source(self, runner_id):
        assert runner_id == RID
        with self.storage:
            self.entered.set()
            assert self.release.wait(5), "test initialization was not released"
        return {"directory": str(self.directory)}

    def stop(self, runner_id):
        assert runner_id == RID
        assert self.storage.acquire(blocking=False), "maintenance overlapped owned storage initialization"
        try:
            self.maintained.set()
        finally:
            self.storage.release()


def sender_for(tmp_path, runtime, watch_factory):
    # No delivery/database or OS ACL setup is needed for this lifecycle proof.
    sender = JobEventSender("worker-one", runtime, None, tmp_path / "private", watch_factory)
    sender.active = True
    runtime.job_event_cache = sender
    sender._jobs[RID] = {"state": "started", "job": "old-job", "run_id": "7"}
    source = _RunnerSource(sender, RID)
    sender._sources[RID] = source
    sender.failed_sources.add(RID)
    source.start()
    return sender, source


def never_open(directory):
    raise AssertionError("a cancelled late source opened a native watch")


def test_pause_waits_for_initialization_and_storage_release(tmp_path):
    runtime = StorageRuntime(tmp_path)
    sender, source = sender_for(tmp_path, runtime, never_open)
    assert runtime.entered.wait(1)
    joining, finished = threading.Event(), threading.Event()
    joins, errors = [], []
    original_join = source.thread.join

    def observed_join(timeout):
        joins.append(timeout)
        joining.set()
        original_join(timeout)

    source.thread.join = observed_join

    def maintenance():
        try:
            dispatch(Agent("worker-one", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    operation = threading.Thread(target=maintenance)
    operation.start()
    try:
        assert joining.wait(1)
        assert joins == [300]  # shared across all runtimes, including initialization
        assert not finished.is_set() and not runtime.maintained.is_set()
        assert source.thread.is_alive() and source.stop_event.is_set()
        runtime.release.set()
        assert finished.wait(1)
        assert errors == [] and runtime.maintained.is_set()
        assert not source.thread.is_alive() and RID not in sender._sources
        assert sender._paused == {} and RID in sender.failed_sources
        assert sender.jobs([RID]) == {}
    finally:
        runtime.release.set()
        operation.join(2)
        original_join(2)


def test_initializer_timeout_still_holds_maintenance_and_balances_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(job_events, "SOURCE_STOP_TIMEOUT", 0.02)
    runtime = StorageRuntime(tmp_path)
    sender, source = sender_for(tmp_path, runtime, never_open)
    assert runtime.entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match="storage maintenance held"):
            dispatch(Agent("worker-one", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        assert not runtime.maintained.is_set() and source.thread.is_alive()
        assert sender._sources[RID] is source and sender._paused == {}
        assert RID in sender.failed_sources and sender.jobs([RID]) == {}
    finally:
        runtime.release.set()
        source.thread.join(2)
        assert not source.thread.is_alive()


def test_late_native_watch_creation_closes_owned_handle_before_maintenance(tmp_path):
    runtime = StorageRuntime(tmp_path)
    runtime.release.set()
    creating, release, closed, joining = (threading.Event() for _ in range(4))

    class Watch:
        def __init__(self, directory):
            assert runtime.storage.acquire(blocking=False)
            creating.set()
            assert release.wait(5)

        def close(self):
            runtime.storage.release()
            closed.set()

        def wake(self):
            raise AssertionError("watch was not published when pause began")

    sender, source = sender_for(tmp_path, runtime, Watch)
    assert creating.wait(1)
    original_join = source.thread.join
    source.thread.join = lambda timeout: (joining.set(), original_join(timeout))[-1]
    errors, done = [], threading.Event()

    def maintenance():
        try:
            dispatch(Agent("worker-one", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    operation = threading.Thread(target=maintenance)
    operation.start()
    try:
        assert joining.wait(1) and source.watch is None
        assert not closed.is_set() and not runtime.maintained.is_set()
        release.set()
        assert done.wait(1) and errors == []
        assert closed.is_set() and runtime.maintained.is_set()
        assert not source.thread.is_alive() and sender._paused == {}
    finally:
        release.set()
        operation.join(2)
        original_join(2)


def test_late_replay_readiness_cannot_clear_paused_unknown_state(tmp_path):
    runtime = StorageRuntime(tmp_path)
    runtime.release.set()
    reading, release, closed = (threading.Event() for _ in range(3))

    class Watch:
        def __init__(self, directory):
            self.spool = self

        def read(self, runner_id, stopped):
            reading.set()
            assert release.wait(5)
            return []

        def wake(self):
            release.set()

        def wait(self):
            return False

        def close(self):
            closed.set()

    sender, source = sender_for(tmp_path, runtime, Watch)
    assert reading.wait(1)
    try:
        dispatch(Agent("worker-one", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        assert closed.is_set() and not source.thread.is_alive()
        assert RID in sender.failed_sources and sender.jobs([RID]) == {}
        assert sender._paused == {} and runtime.maintained.is_set()
    finally:
        release.set()
        source.thread.join(2)


def test_late_guest_readiness_releases_remote_handles_without_clearing_pause(tmp_path):
    runtime = StorageRuntime(tmp_path)
    reading, release, waited = (threading.Event() for _ in range(3))
    closed = []

    class Stream:
        def __init__(self, name):
            self.name = name

        def readline(self, maximum):
            reading.set()
            assert release.wait(5)
            return '{"ready": "' + RID + '"}\n'

        def close(self):
            closed.append(self.name)

    class Remote:
        def __init__(self):
            assert runtime.storage.acquire(blocking=False)
            self.stdin, self.stdout = Stream("stdin"), Stream("stdout")

        def terminate(self):
            release.set()

        def wait(self, timeout):
            runtime.storage.release()
            waited.set()

    class Guest:
        def job_event_stream(self, directory, runner_id, pin):
            return Remote()

    runtime.job_event_source = lambda rid: {"directory": str(tmp_path), "guest": Guest(), "known_hosts": "test-pin"}
    sender, source = sender_for(tmp_path, runtime, never_open)
    assert reading.wait(1)
    try:
        dispatch(Agent("worker-one", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        assert waited.is_set() and sorted(closed) == ["stdin", "stdout"]
        assert not source.thread.is_alive() and runtime.maintained.is_set()
        assert RID in sender.failed_sources and sender.jobs([RID]) == {}
        assert sender._paused == {}
    finally:
        release.set()
        source.thread.join(2)


@pytest.mark.parametrize("old_result", ["ready", "failure"])
def test_removed_source_cannot_change_replacement_readiness(tmp_path, old_result):
    old_reading, release_old, new_entered, release_new = (threading.Event() for _ in range(4))
    new_ready, wake_new = threading.Event(), threading.Event()
    calls, lock = [], threading.Lock()

    class Runtime:
        def job_event_source(self, rid):
            with lock:
                index = len(calls)
                calls.append(index)
            if index == 1:
                new_entered.set()
                if old_result == "ready":
                    assert release_new.wait(5)
            return {"directory": str(tmp_path)}

    class Watch:
        def __init__(self, directory):
            with lock:
                self.old = not hasattr(self.__class__, "created")
                self.__class__.created = True
            self.spool = self

        def read(self, rid, stopped):
            if self.old:
                old_reading.set()
                assert release_old.wait(5)
                if old_result == "failure":
                    raise RuntimeError("old source failure after replacement")
            return []

        def wait(self):
            if not self.old:
                new_ready.set()
                assert wake_new.wait(5)
            return False

        def wake(self):
            wake_new.set()

        def close(self):
            pass

    runtime = Runtime()
    sender, old = sender_for(tmp_path, runtime, Watch)
    assert old_reading.wait(1)
    replacement = _RunnerSource(sender, RID)
    with sender._lock:
        sender._sources[RID] = replacement
        sender.failed_sources.add(RID)
        replacement.start()
    try:
        assert new_entered.wait(1)
        if old_result == "failure":
            assert new_ready.wait(1) and RID not in sender.failed_sources
        # Mirrors reconcile's gap: replacement is installed before old.stop().
        assert not old.stop_event.is_set()
        release_old.set()
        old.thread.join(1)
        assert not old.thread.is_alive() and replacement.thread.is_alive()
        assert (RID in sender.failed_sources) == (old_result == "ready")
    finally:
        old.stop_event.set()
        replacement.stop_event.set()
        release_old.set()
        release_new.set()
        wake_new.set()
        old.thread.join(2)
        replacement.thread.join(2)
