"""Real native notifications, durable replay and hostile spool boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from agent.heartbeat import _jobs
from agent.hooks.job_event import emit
from agent.job_event_stream import DirectoryChanges, OwnedSpool, read_events, validate_event
from agent.job_events import JobEventSender

RID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def event(state="started", **values):
    return {"event_id": "delivery-" + uuid.uuid4().hex, "runner_id": RID,
            "state": state, "job": "test", "repo": "owner/repo", "commit": "a" * 40,
            "run_id": "7", "run_attempt": "1", "job_id": "8",
            "at": datetime.now(timezone.utc).isoformat(), "result": None, **values}


def write(directory, body):
    temporary = directory / ".partial"
    temporary.write_text(json.dumps(body), encoding="utf-8")
    filename = uuid.uuid4().hex + ".json"
    temporary.replace(directory / filename)
    return filename


class Link:
    timeout = 1
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.messages = []
        self.received = threading.Event()
    def post(self, path, body):
        self.messages.append((path, body))
        self.received.set()
        return self.accepted


class Runtime:
    def __init__(self, directory):
        self.directory = directory
    def job_event_source(self, rid):
        assert rid == RID
        return {"directory": str(self.directory)}
    def jobs(self, ids):
        raise AssertionError("legacy log probing ran while native source was active")


def test_native_atomic_replace_and_cancellable_wait(tmp_path):
    watch = DirectoryChanges(tmp_path)
    try:
        write(tmp_path, event())
        with ThreadPoolExecutor(max_workers=1) as pool:
            changed = pool.submit(watch.wait)
            assert changed.result(timeout=5) is True
            waiting = pool.submit(watch.wait)
            watch.wake()
            assert waiting.result(timeout=5) is False
    finally:
        watch.wake()
        watch.close()


def test_real_native_source_delivers_without_heartbeat_polling_and_releases_handles(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    runtime, link = Runtime(spool), Link()
    sender = JobEventSender("worker-a", runtime, link, tmp_path / "private").start()
    runtime.job_event_cache = sender
    try:
        sender.reconcile([{"runner_id": RID, "state": "running"}])
        body = event()
        write(spool, body)  # also covers registration-before-initial-replay race
        assert link.received.wait(5)
        assert _jobs(runtime, [RID]) == {RID: "test"}
        assert link.messages[0][1]["host_id"] == "worker-a"
        assert link.messages[0][1]["kind"] == "job"
        assert "source" not in link.messages[0][1]
        sender.pause(RID)
        assert _jobs(runtime, [RID]) == {}
        moved = tmp_path / "spool-moved"
        spool.rename(moved)  # native Windows directory handles must be released
    finally:
        sender.stop()


def test_durable_outbox_replays_after_restart(tmp_path):
    root = tmp_path / "private"
    failed = Link(accepted=False)
    sender = JobEventSender("worker-a", Runtime(tmp_path), failed, root).start()
    body = event("finished", result="failure")
    try:
        sender.record(RID, body)
        assert failed.received.wait(5)
    finally:
        sender.stop()
    delivered = Link()
    sender = JobEventSender("worker-a", Runtime(tmp_path), delivered, root).start()
    try:
        assert delivered.received.wait(5)
        assert delivered.messages[0][1]["event_id"] == body["event_id"]
        assert delivered.messages[0][1]["result"] == "failure"
        sender.record(RID, body)
        with sqlite3.connect(root / "events.sqlite") as connection:
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        sender.stop()


def test_conflicting_identity_and_cross_runner_events_refused(tmp_path):
    sender = JobEventSender("worker-a", Runtime(tmp_path), Link(), tmp_path / "private").start()
    try:
        body = event()
        sender.record(RID, body)
        with pytest.raises(ValueError, match="conflicting"):
            sender.record(RID, {**body, "job": "changed"})
        with pytest.raises(ValueError, match="owned runner"):
            sender.record(RID, {**body, "runner_id": OTHER})
    finally:
        sender.stop()


def test_future_event_kept_as_evidence_cannot_freeze_cache(tmp_path):
    sender = JobEventSender("worker-a", Runtime(tmp_path), Link(), tmp_path / "private").start()
    try:
        sender.record(RID, event(at="2099-01-01T00:00:00Z", job="false future"))
        assert sender.jobs([RID]) == {}
        sender.record(RID, event())
        assert sender.jobs([RID]) == {RID: "test"}
        sender.record(RID, event("finished", result="success"))
        assert sender.jobs([RID]) == {}
        sender.record(RID, event())
        assert sender.jobs([RID]) == {}  # same execution cannot resurrect
    finally:
        sender.stop()


def test_incomplete_worker_evidence_keeps_identified_cache_and_durable_outbox(tmp_path):
    sender = JobEventSender("worker-a", Runtime(tmp_path), Link(), tmp_path / "private").start()
    try:
        sender.record(RID, event())
        sender.record(RID, event(job=None, run_id=None))
        sender.record(RID, event("finished", job=None, run_id=None))
        assert sender.jobs([RID]) == {RID: "test"}
        with sender._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
        sender.record(RID, event("finished"))
        assert sender.jobs([RID]) == {}
    finally:
        sender.stop()


def test_identified_worker_replaces_preexisting_incomplete_cache(tmp_path):
    sender = JobEventSender("worker-a", Runtime(tmp_path), Link(), tmp_path / "private").start()
    try:
        stale = event(job=None, run_id=None, at="9999-01-01T00:00:00Z")
        sender._jobs[RID] = stale
        with sender._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO current_jobs VALUES(?,?)", (RID, json.dumps(stale)))
        assert sender.jobs([RID]) == {}
        sender.record(RID, event())
        assert sender.jobs([RID]) == {RID: "test"}
    finally:
        sender.stop()


def test_hook_tests_cannot_inherit_the_ci_runner_event_spool(tmp_path):
    import subprocess
    import sys
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node is required to exercise the real hook subprocess")
    spool = tmp_path / "production-spool"
    spool.mkdir()
    env = dict(os.environ, NOMERCY_RUNNER_ID=RID, NOMERCY_JOB_EVENT_DIR=str(spool))
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-m", "pytest", "-q",
                           "agent/tests/test_windows_job_hooks.py::TestNode::test_plenty_of_room_lets_the_job_run"],
                          cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not list(spool.iterdir())  # neither .active-job nor forged events reach the live path


def test_failed_or_stopped_source_never_returns_stale_job(tmp_path):
    sender = JobEventSender("worker-a", Runtime(tmp_path), Link(), tmp_path / "private").start()
    try:
        sender.record(RID, event())
        sender.failed_sources.add(RID)
        assert sender.jobs([RID]) == {}
        sender.failed_sources.clear()
        assert sender.jobs([RID]) == {RID: "test"}
        sender.reconcile([{"runner_id": RID, "state": "stopped"}])
        assert sender.jobs([RID]) == {}
        with sender._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM current_jobs").fetchone()[0] == 0
    finally:
        sender.stop()


def test_junk_entries_do_not_hide_later_valid_event(tmp_path):
    for index in range(300):
        (tmp_path / ("junk-%04d" % index)).write_text("junk")
    body = event()
    write(tmp_path, body)
    assert [got for _, got in read_events(tmp_path, RID)] == [body]


def test_large_junk_scan_can_stop_in_one_batch(tmp_path):
    for index in range(600):
        (tmp_path / ("junk-%04d" % index)).write_text("junk")
    spool = OwnedSpool(tmp_path)
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) > 1
    try:
        assert list(spool.read(RID, stopped=cancelled)) == []
        assert len(checks) == 2
    finally:
        spool.close()


def test_oversized_invalid_and_hardlinked_files_are_not_observations(tmp_path):
    body = event()
    filename = write(tmp_path, body)
    os.link(tmp_path / filename, tmp_path / (uuid.uuid4().hex + ".json"))
    (tmp_path / (uuid.uuid4().hex + ".json")).write_text("x" * 4097)
    write(tmp_path, {**body, "secret": "must-not-escape"})
    assert list(read_events(tmp_path, RID)) == []


def test_ack_does_not_remove_conflicting_replacement(tmp_path):
    original = event()
    name = write(tmp_path, original)
    spool = OwnedSpool(tmp_path)
    try:
        replacement = event(job="new immutable event")
        (tmp_path / name).write_text(json.dumps(replacement))
        assert not spool.acknowledge(name, original)
        assert (tmp_path / name).exists()
        assert spool.acknowledge(name, replacement)
        assert not (tmp_path / name).exists()
    finally:
        spool.close()


def test_pinned_directory_cannot_redirect_privileged_reader(tmp_path):
    directory = tmp_path / "owned"
    directory.mkdir()
    body = event()
    write(directory, body)
    spool = OwnedSpool(directory)
    try:
        if os.name == "nt":
            with pytest.raises(OSError):
                directory.rename(tmp_path / "renamed")
        else:
            directory.rename(tmp_path / "renamed")
            directory.mkdir()
            write(directory, event(job="replacement"))
        assert [got for _, got in spool.read(RID)] == [body]
    finally:
        spool.close()


def test_native_registration_failure_releases_all_ancestor_handles(tmp_path, monkeypatch):
    captured = []
    import agent.job_event_stream as module
    real = module.OwnedSpool
    def opened(path):
        spool = real(path)
        captured.append(spool)
        return spool
    def broken(self):
        raise OSError("native registration failed")
    monkeypatch.setattr(module, "OwnedSpool", opened)
    monkeypatch.setattr(module.DirectoryChanges, "_register", broken)
    directory = tmp_path / "owned"
    directory.mkdir()
    with pytest.raises(OSError, match="registration"):
        module.DirectoryChanges(directory)
    assert captured[0].handles == []
    assert captured[0].directories == []
    directory.rename(tmp_path / "renamed")


def test_partial_native_registration_failure_closes_native_watch_and_spool(tmp_path, monkeypatch):
    import agent.job_event_stream as module
    opened = []
    real = module.DirectoryChanges._register
    def broken(self):
        real(self)
        opened.append(self)
        raise OSError("failure after native handles opened")
    monkeypatch.setattr(module.DirectoryChanges, "_register", broken)
    directory = tmp_path / "owned"
    directory.mkdir()
    with pytest.raises(OSError, match="native handles"):
        module.DirectoryChanges(directory)
    assert opened[0].closed
    assert not opened[0].spool.handles
    assert not opened[0].spool.directories
    directory.rename(tmp_path / "renamed")


def darwin_watch(tmp_path, monkeypatch, registration_error=False):
    """Apple Python's select API deliberately excludes USER/TRIGGER constants."""
    import agent.job_event_stream as module
    from types import SimpleNamespace

    class Spool:
        def __init__(self, directory):
            self.path = directory
            self.fd, self.writer = os.pipe()
            self.directories = [self.fd]
            self.closed = False
        def close(self):
            self.closed = True
            os.close(self.fd)
            os.close(self.writer)

    class Queue:
        def __init__(self):
            self.closed = False
            self.registered = []
            self.events = []
        def control(self, changes, count):
            if changes is not None:
                self.registered.extend(changes)
                if registration_error:
                    raise OSError("kqueue registration failed")
                return []
            return self.events
        def close(self):
            self.closed = True

    queue = Queue()
    select = SimpleNamespace(kqueue=lambda: queue,
                             kevent=lambda ident, **values: SimpleNamespace(ident=ident, **values),
                             KQ_FILTER_VNODE=-4, KQ_FILTER_READ=-1, KQ_EV_ADD=1, KQ_EV_CLEAR=32,
                             KQ_NOTE_RENAME=32, KQ_NOTE_DELETE=1, KQ_NOTE_WRITE=2)
    monkeypatch.setattr(module, "select", select)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "OwnedSpool", Spool)
    return module, queue, select


def test_apple_select_without_user_filter_keeps_native_watch_and_pipe_cancellation(tmp_path, monkeypatch):
    module, queue, select = darwin_watch(tmp_path, monkeypatch)
    watch = module.DirectoryChanges(tmp_path)
    fds = [watch.fd, watch.stop_read, watch.stop_write]
    try:
        assert [row.filter for row in queue.registered] == [select.KQ_FILTER_VNODE, select.KQ_FILTER_READ]
        queue.events = [select.kevent(watch.spool.fd, filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_WRITE)]
        assert watch.wait() is True
        watch.wake()
        assert os.read(watch.stop_read, 1) == b"x"
        # Repeated cancellation stays bounded even if the pipe is already full.
        while True:
            try:
                os.write(watch.stop_write, b"x" * 4096)
            except BlockingIOError:
                break
        watch.wake()
        queue.events = [select.kevent(watch.stop_read, filter=select.KQ_FILTER_READ, fflags=0)]
        assert watch.wait() is False
    finally:
        watch.close()
    assert queue.closed and watch.spool.closed
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_apple_pipe_registration_failure_closes_all_resources(tmp_path, monkeypatch):
    module, queue, _ = darwin_watch(tmp_path, monkeypatch, registration_error=True)
    opened = []
    original = module.DirectoryChanges._register
    def register(self):
        opened.append(self)
        original(self)
    monkeypatch.setattr(module.DirectoryChanges, "_register", register)
    with pytest.raises(OSError, match="kqueue registration"):
        module.DirectoryChanges(tmp_path)
    watch = opened[0]
    assert watch.closed and watch.spool.closed and queue.closed
    for fd in (watch.fd, watch.stop_read, watch.stop_write):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_guest_stream_enforces_pin_and_cannot_reuse_insecure_ssh_master(tmp_path, monkeypatch):
    from agent.runtimes.guest_ssh import GuestExec
    import agent.runtimes.guest_ssh as module
    pin = tmp_path / "guest_known_hosts"
    pin.write_text("[127.0.0.1]:51001 ssh-ed25519 public-placeholder\n")
    pin.chmod(0o600)
    calls = []
    def started(argv, **options):
        calls.append((argv, options))
        return object()
    monkeypatch.setattr(module.subprocess, "Popen", started)
    guest = GuestExec(host="127.0.0.1", user="runner", port=51001, password="private-test-password")
    guest.job_event_stream("/Users/runner/runners/" + RID + "/logs/job-events", RID, str(pin))
    argv, options = calls[0]
    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=" + str(pin) in argv
    assert "ControlPath=none" in argv and "ControlMaster=no" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert "UserKnownHostsFile=/dev/null" not in argv
    assert not any("ControlPersist=" in value for value in argv)
    assert "private-test-password" not in " ".join(argv)
    assert options["env"]["SSHPASS"] == "private-test-password"
    assert "/Library/NoMercy/job-events/job_event_stream.py" in argv[-1]


def test_common_github_producer_allowlist_no_guessed_result_and_copies_match(tmp_path):
    env = {"NOMERCY_RUNNER_ID": RID, "NOMERCY_JOB_EVENT_DIR": str(tmp_path), "GITHUB_JOB": "test\njob",
           "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "2",
           "SECRET": "must-not-escape", "GITHUB_TOKEN": "must-not-escape", "RUNNER_RESULT": "success"}
    assert emit("started", env)
    assert emit("finished", env)
    records = [body for _, body in read_events(tmp_path, RID)]
    assert {body["state"] for body in records} == {"started", "finished"}
    assert all(body["result"] is None and body["job"] == "testjob" for body in records)
    assert len({body["job_id"] for body in records}) == 1
    first = records[0]["job_id"]
    assert first.startswith("hook:")
    assert emit("started", env)
    records = [body for _, body in read_events(tmp_path, RID)]
    assert len({body["job_id"] for body in records}) == 2  # same slug, new execution
    assert "must-not-escape" not in json.dumps(records)
    root = Path(__file__).resolve().parents[2]
    source = (root / "agent/hooks/job_event.py").read_bytes()
    for copy in ("agent/hooks/windows/job_event.py", "agent/hooks/macos/job_event.py", "images/linux/unit/runner/job_event.py"):
        assert (root / copy).read_bytes() == source


@pytest.mark.skipif(os.name != "nt", reason="Windows volume GUID path")
def test_windows_volume_guid_spool_handles_validated_mount_target(tmp_path):
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel.GetVolumeNameForVolumeMountPointW.restype = w.BOOL
    buffer = ctypes.create_unicode_buffer(128)
    assert kernel.GetVolumeNameForVolumeMountPointW(tmp_path.anchor, buffer, len(buffer))
    canonical = Path(buffer.value) / tmp_path.relative_to(tmp_path.anchor)
    env = {"NOMERCY_RUNNER_ID": RID, "NOMERCY_JOB_EVENT_DIR": str(canonical), "GITHUB_JOB": "canonical-volume"}
    assert emit("started", env)
    records = list(read_events(canonical, RID))
    assert len(records) == 1 and records[0][1]["job"] == "canonical-volume"
    spool = OwnedSpool(canonical)
    try:
        assert spool.acknowledge(*records[0])
    finally:
        spool.close()


def test_failed_pause_retains_live_source_and_balances_verb_lease(tmp_path):
    from agent.verbs import Agent, dispatch
    class Thread:
        def is_alive(self):
            return True
    class Source:
        thread = Thread()
        def stop(self):
            pass
    class Managed(Runtime):
        def stop(self, rid):
            raise AssertionError("storage work ran despite a live watcher")
    runtime = Managed(tmp_path)
    sender = JobEventSender("worker-a", runtime, Link(), tmp_path / "private").start()
    runtime.job_event_cache = sender
    source = Source()
    sender._sources[RID] = source
    try:
        with pytest.raises(RuntimeError, match="maintenance held"):
            dispatch(Agent("worker-a", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        assert sender._paused == {}
        assert sender._sources[RID] is source
        with pytest.raises(RuntimeError, match="maintenance held"):
            dispatch(Agent("worker-a", runtime, object()), "exec_unit.stop", {"runner_id": RID})
        assert sender._sources[RID] is source
    finally:
        sender._sources.clear()
        sender.stop()


@pytest.mark.parametrize("changes", [{"runner_id": OTHER}, {"at": "2026-10-11T00:00:00"},
                                      {"state": "unknown"}, {"source": "forge"}, {"job": "x" * 201},
                                      {"result": "success"}, {"event_id": "../../secret"}])
def test_invalid_spool_schema_is_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        validate_event(event(**changes), RID)
