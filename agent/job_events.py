"""Native job notifications with a private durable outbox and memory cache.

Runner-writable files are untrusted data. Only the owning runtime determines
their location, and only mTLS authenticates the resulting worker observation.
Delivery retries are triggered by events or failed sends, never by job queries.
"""
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading

from . import naming, protocol
from .job_event_stream import DirectoryChanges, read_events, safe_directory, validate_event


# Source initialization can verify owned storage and ACLs before a native watch
# exists. Wait for that work to release its handles on every platform; this is
# a maintenance bound, independent of the runner's job timeout.
SOURCE_STOP_TIMEOUT = 300


class JobEventSender:
    def __init__(self, host_id, runtime, link, root, watch_factory=DirectoryChanges):
        self.host_id, self.runtime, self.link = host_id, runtime, link
        self.root, self.watch_factory = Path(root), watch_factory
        self.active = False
        self._stop, self._ready = threading.Event(), threading.Event()
        self._lock, self._sources, self._paused = threading.RLock(), {}, {}
        self._jobs, self.failed_sources = {}, set()
        self._delivery = None

    def _connect(self):
        return sqlite3.connect(self.root / "events.sqlite", timeout=20)

    def start(self):
        safe_directory(self.root.parent)
        self.root.mkdir(mode=0o700, exist_ok=True)
        safe_directory(self.root)
        if os.name == "nt":
            identity = subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe"),
                                       "/user", "/fo", "csv", "/nh"], check=True, capture_output=True,
                                      text=True, timeout=5)
            sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
            if not sid.startswith("S-1-") or not all(char in "S-0123456789" for char in sid):
                raise ValueError("management identity SID could not be read")
            subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe"), str(self.root),
                            "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                            "*" + sid + ":(OI)(CI)F", "/Q"],
                           check=True, capture_output=True, timeout=30)
        if os.name == "posix" and (self.root.stat().st_uid != os.geteuid() or self.root.stat().st_mode & 0o077):
            raise ValueError("job-event outbox is not privately owned")
        database = self.root / "events.sqlite"
        if database.exists():
            info = database.lstat()
            if database.is_symlink() or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("unsafe job-event outbox")
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
                               "delivered INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE IF NOT EXISTS current_jobs(runner_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            self._jobs = {rid: json.loads(payload) for rid, payload in
                          connection.execute("SELECT runner_id,payload FROM current_jobs")}
        if os.name == "posix":
            os.chmod(database, 0o600)
        self.active = True
        self._delivery = threading.Thread(target=self._deliver, name="agent-job-events", daemon=True)
        self._delivery.start()
        return self

    def reconcile(self, instances):
        """Use the heartbeat's existing ownership observation, without log reads."""
        if not self.active:
            return
        owned = {naming.check(item["runner_id"]) for item in instances if item.get("state") not in ("stopped", "absent")}
        with self._lock:
            owned -= self._paused.keys()
            dead = {rid for rid, source in self._sources.items() if source.thread and not source.thread.is_alive()}
            removed_ids = (set(self._sources) - owned | dead) - self._paused.keys()
            removed = [self._sources.pop(rid) for rid in removed_ids]
            stopped = [item["runner_id"] for item in instances if item.get("state") in ("stopped", "absent")]
            if stopped:
                with self._connect() as connection:
                    connection.executemany("DELETE FROM current_jobs WHERE runner_id=?", [(rid,) for rid in stopped])
                for rid in stopped:
                    self._jobs.pop(rid, None)
            for rid in owned - set(self._sources):
                source = _RunnerSource(self, rid)
                self._sources[rid] = source
                self.failed_sources.add(rid)
                source.start()
        for source in removed:
            source.stop()

    def record(self, runner_id, event):
        event = validate_event(event, naming.check(runner_id))
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            previous = connection.execute("SELECT payload FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            if previous:
                if previous[0] != payload:
                    raise ValueError("job event identity has conflicting content")
                return
            if connection.execute("SELECT COUNT(*) FROM events WHERE delivered=0").fetchone()[0] >= 10000:
                raise RuntimeError("job event outbox is full; leave producer event for retry")
            connection.execute("INSERT INTO events(event_id,payload) VALUES(?,?)", (event["event_id"], payload))
            current = self._jobs.get(runner_id)
            def rank(item):
                at = datetime.fromisoformat(item["at"].replace("Z", "+00:00")).astimezone(timezone.utc)
                job_id = item.get("job_id") or ""
                return at, int(job_id) if job_id.isdigit() else 0, item["state"] == "finished"
            bounded_clock = rank(event)[0] <= datetime.now(timezone.utc) + timedelta(seconds=30)
            same_execution = current and event.get("run_id") and event.get("run_id") == current.get("run_id") and (
                event.get("run_attempt") == current.get("run_attempt")) and (
                event.get("job_id") == current.get("job_id") if event.get("job_id") and current.get("job_id") else
                event.get("job") == current.get("job"))
            terminal_start = same_execution and current["state"] == "finished" and event["state"] == "started"
            identified = bool(event.get("job") and event.get("run_id"))
            current_identified = bool(current and current.get("job") and current.get("run_id"))
            if identified and bounded_clock and not terminal_start and (not current_identified or rank(event) >= rank(current)):
                connection.execute("INSERT OR REPLACE INTO current_jobs VALUES(?,?)", (runner_id, payload))
                connection.commit()  # durable before cache update or producer acknowledgement
                self._jobs[runner_id] = event
        self._ready.set()

    def pause(self, runner_id):
        """Release native directory handles before runner storage maintenance."""
        with self._lock:
            self._paused[runner_id] = self._paused.get(runner_id, 0) + 1
            source = self._sources.get(runner_id)
            self.failed_sources.add(runner_id)
        if source:
            source.stop()
            if source.thread and source.thread.is_alive():
                raise RuntimeError("native job watcher did not stop; storage maintenance held")
            with self._lock:
                if self._sources.get(runner_id) is source:
                    self._sources.pop(runner_id)

    def unpause(self, runner_id):
        with self._lock:
            count = self._paused.get(runner_id, 0)
            if count > 1:
                self._paused[runner_id] = count - 1
            else:
                self._paused.pop(runner_id, None)

    def jobs(self, runner_ids):
        with self._lock:
            return {rid: event.get("job") for rid in runner_ids
                    if rid not in self.failed_sources and (event := self._jobs.get(rid)) and event["state"] == "started"
                    and event.get("job") and event.get("run_id")}

    def _deliver(self):
        pause = 1
        while not self._stop.is_set():
            self._ready.clear()
            with self._lock, self._connect() as connection:
                row = connection.execute("SELECT event_id,payload FROM events WHERE delivered=0 ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                self._ready.wait()
                continue
            event_id, payload = row
            event = dict(json.loads(payload), kind="job", host_id=self.host_id)
            try:
                delivered = self.link.post(protocol.EVENT_PATH, event)
            except Exception:
                delivered = False
            if not delivered:
                if self._stop.wait(pause):
                    return
                pause = min(pause * 2, 30)
                continue
            pause = 1
            with self._lock, self._connect() as connection:
                connection.execute("UPDATE events SET delivered=1 WHERE event_id=?", (event_id,))
                connection.execute("DELETE FROM events WHERE delivered=1 AND rowid NOT IN "
                                   "(SELECT rowid FROM events WHERE delivered=1 ORDER BY rowid DESC LIMIT 10000)")

    def stop(self):
        self.active = False
        self._stop.set()
        self._ready.set()
        with self._lock:
            sources, self._sources = list(self._sources.values()), {}
        for source in sources:
            source.stop()
        if self._delivery:
            self._delivery.join(timeout=self.link.timeout + 1)


class _RunnerSource:
    def __init__(self, owner, runner_id):
        self.owner, self.runner_id = owner, runner_id
        self.stop_event = threading.Event()
        self.watch, self.thread, self.remote = None, None, None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="job-watch-" + self.runner_id, daemon=True)
        self.thread.start()

    def _run(self):
        pause = 1
        while not self.stop_event.is_set():
            try:
                source = self.owner.runtime.job_event_source(self.runner_id)
                if self.stop_event.is_set():
                    return
                if source.get("guest"):
                    self._remote(source)
                else:
                    self._local(source["directory"])
                if not self.stop_event.is_set():
                    raise RuntimeError("native event source ended")
            except Exception:
                # No exception detail: it may contain private paths. A failed
                # source remains unknown and never falls back to log polling.
                with self.owner._lock:
                    if self.owner._sources.get(self.runner_id) is not self:
                        return
                    self.owner.failed_sources.add(self.runner_id)
                if self.stop_event.wait(pause):
                    return
                pause = min(pause * 2, 30)

    def _local(self, directory):
        directory = safe_directory(directory)
        self.watch = self.owner.watch_factory(directory)
        try:
            if self.stop_event.is_set():
                return
            while not self.stop_event.is_set():
                for filename, event in self.watch.spool.read(self.runner_id, stopped=self.stop_event.is_set):
                    if self.stop_event.is_set():
                        return
                    self.owner.record(self.runner_id, event)
                    self.watch.spool.acknowledge(filename, event)
                if not self._mark_ready():
                    return
                if not self.watch.wait():
                    return
        finally:
            self.watch.close()
            self.watch = None

    def _remote(self, source):
        self.remote = source["guest"].job_event_stream(source["directory"], self.runner_id, source["known_hosts"])
        try:
            if self.stop_event.is_set():
                return
            while not self.stop_event.is_set():
                content = self.remote.stdout.readline(4098)
                if not content:
                    return
                if len(content) > 4097 or not content.endswith("\n"):
                    raise ValueError("remote event exceeded its bounded frame")
                event = json.loads(content)
                if event == {"ready": self.runner_id}:
                    if not self._mark_ready():
                        return
                    continue
                self.owner.record(self.runner_id, event)
                self.remote.stdin.write(event["event_id"] + "\n")
                self.remote.stdin.flush()
        finally:
            self.remote.terminate()
            try:
                self.remote.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.remote.kill()
                self.remote.wait(timeout=5)
            for stream in (self.remote.stdin, self.remote.stdout):
                stream.close()
            self.remote = None

    def _mark_ready(self):
        # A pause marks the cached job unknown before stopping this thread.
        # Late replay/guest readiness must not clear that mark during teardown.
        with self.owner._lock:
            if (self.owner._sources.get(self.runner_id) is not self or
                    self.stop_event.is_set() or self.runner_id in self.owner._paused):
                return False
            self.owner.failed_sources.discard(self.runner_id)
            return True

    def stop(self):
        self.stop_event.set()
        # The source thread may close and clear either reference concurrently.
        watch, remote = self.watch, self.remote
        if watch:
            watch.wake()
        if remote:
            remote.terminate()
        if self.thread:
            self.thread.join(timeout=SOURCE_STOP_TIMEOUT)
