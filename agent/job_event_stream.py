"""Bounded immutable job files, read only after native OS notifications.

This module is also the protected, standalone guest-side SSH producer. It
executes no runner file and never reads job logs, environments or payloads.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import select
import stat
import struct
import sys
import threading

MAX_EVENT = 4096
MAX_FILES = 256
FIELDS = {"event_id", "runner_id", "state", "job", "repo", "commit", "run_id",
          "run_attempt", "job_id", "at", "result"}
RESULTS = {"success", "failure", "cancelled", "timed_out", "skipped", "neutral",
           "action_required", "stale"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FILE = re.compile(r"[0-9a-f]{32}\.json")


def validate_event(body, runner_id):
    from datetime import datetime
    if not isinstance(body, dict) or set(body) - FIELDS:
        raise ValueError("job event contains unreviewed fields")
    if not UUID.fullmatch(runner_id) or body.get("runner_id") != runner_id:
        raise ValueError("job event is outside its owned runner")
    if body.get("state") not in ("started", "finished"):
        raise ValueError("invalid job transition")
    for key in FIELDS - {"result"}:
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > (200 if key == "job" else 150)
                                  or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("invalid bounded event field")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", body.get("event_id") or ""):
        raise ValueError("invalid event identity")
    at = body.get("at") or ""
    if len(at) > 40 or datetime.fromisoformat(at.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("event timestamp must include timezone")
    result = body.get("result")
    if result is not None and result not in RESULTS:
        raise ValueError("invalid job result")
    if body["state"] == "started" and result is not None:
        raise ValueError("started job has a result")
    return {key: body.get(key) for key in FIELDS}


def safe_directory(path):
    """Refuse symlinks/reparse points in every component before privileged IO."""
    path = Path(path)
    if not path.is_absolute() or path == path.parent:
        raise ValueError("spool requires an absolute owned directory")
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("unsafe event directory")
    return path


class OwnedSpool:
    """Pin every directory component; reads and acks remain in this spool.

    POSIX uses descriptor-relative no-follow IO. Windows holds every ancestor
    without FILE_SHARE_DELETE and opens files with OPEN_REPARSE_POINT, so a
    runner cannot redirect the management process through junction replacement.
    """
    def __init__(self, directory):
        self.path = Path(directory)
        self.fd, self.handles, self.directories = None, [], []
        if not self.path.is_absolute() or self.path == self.path.parent:
            raise ValueError("spool requires an absolute owned directory")
        try:
            if os.name == "nt":
                from ctypes import wintypes as w
                self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self.kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                                    w.DWORD, w.DWORD, w.HANDLE]
                self.kernel.CreateFileW.restype = w.HANDLE
                self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
                self.kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
                self.kernel.GetFileInformationByHandleEx.restype = w.BOOL
                self.kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
                self.kernel.SetFileInformationByHandle.restype = w.BOOL
                for component in reversed((self.path, *self.path.parents)):
                    handle = self._windows_open(str(component), directory=True)
                    self.handles.append(handle)
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                self.fd = os.open(self.path.anchor, flags)
                self.directories.append(self.fd)
                for component in self.path.parts[1:]:
                    next_fd = os.open(component, flags, dir_fd=self.fd)
                    self.fd = next_fd
                    self.directories.append(self.fd)
        except BaseException:
            self.close()
            raise

    def _windows_open(self, path, directory=False):
        # Native leaf opens do not follow reparse points. Read-only sharing
        # prevents mutation/deletion during validation and acknowledgement.
        # Attribute-only handles do not enforce delete sharing on NTFS.
        # GENERIC_READ pins directories against rename as well as replacement.
        access = 0x80000000 if directory else 0x80000000 | 0x10000
        share = 3 if directory else 1
        flags = 0x00200000 | (0x02000000 if directory else 0)
        handle = self.kernel.CreateFileW(path, access, share, None, 3, flags, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = (ctypes.c_uint32 * 2)()
        if not self.kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            self.kernel.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0] & 0x400 or bool(attributes[0] & 0x10) != directory:
            self.kernel.CloseHandle(handle)
            raise ValueError("reparse point or wrong type in owned spool")
        return handle

    def open(self, filename):
        if not FILE.fullmatch(filename):
            raise ValueError("unowned event filename")
        if os.name == "nt":
            import msvcrt
            handle = self._windows_open(str(self.path / filename))
            try:
                return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                self.kernel.CloseHandle(handle)
                raise
        return os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)

    def _body(self, file, runner_id):
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_EVENT:
            raise ValueError("event is not a bounded, singly-linked regular file")
        body = file.read(MAX_EVENT + 1)
        if len(body) > MAX_EVENT:
            raise ValueError("event exceeds its size limit")
        return validate_event(json.loads(body), runner_id)

    def read(self, runner_id, stopped=lambda: False):
        valid = 0
        # Scandir streams entries without materializing them. Junk cannot
        # occupy the first batch forever; each scan advances to later names.
        with os.scandir(self.path if os.name == "nt" else self.fd) as entries:
            for index, entry in enumerate(entries):
                # Walk in bounded batches; a huge junk directory cannot hold
                # source shutdown or native volume maintenance indefinitely.
                if index % MAX_FILES == 0 and stopped():
                    return
                if not FILE.fullmatch(entry.name):
                    continue
                try:
                    with os.fdopen(self.open(entry.name), "rb") as file:
                        event = self._body(file, runner_id)
                except (OSError, ValueError, TypeError, UnicodeError):
                    continue
                yield entry.name, event
                valid += 1
                if valid >= MAX_FILES:
                    return

    def acknowledge(self, filename, expected):
        """Delete only the same immutable event after its durable copy."""
        try:
            with os.fdopen(self.open(filename), "rb") as file:
                if self._body(file, expected["runner_id"]) != expected:
                    return False
                if os.name == "nt":
                    import msvcrt
                    delete = ctypes.c_ubyte(1)
                    if not self.kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(file.fileno()), 4,
                                                                  ctypes.byref(delete), ctypes.sizeof(delete)):
                        raise ctypes.WinError(ctypes.get_last_error())
                else:
                    info = os.fstat(file.fileno())
                    current = os.stat(filename, dir_fd=self.fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                        return False
                    os.unlink(filename, dir_fd=self.fd)
            return True
        except FileNotFoundError:
            return False

    def close(self):
        for fd in self.directories:
            os.close(fd)
        self.directories, self.fd = [], None
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles = []


def read_events(directory, runner_id):
    spool = OwnedSpool(directory)
    try:
        yield from spool.read(runner_id)
    finally:
        spool.close()


class DirectoryChanges:
    """One cancellable watch installed before the initial spool read."""
    def __init__(self, directory):
        self.spool = OwnedSpool(directory)
        self.closed = False
        self._guard = threading.Lock()
        try:
            self._register()
        except BaseException:
            self.close()
            raise

    def _register(self):
        directory = self.spool.path
        if sys.platform == "win32":
            from ctypes import wintypes as w
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = (
                ("FindFirstChangeNotificationW", [w.LPCWSTR, w.BOOL, w.DWORD], w.HANDLE),
                ("FindNextChangeNotification", [w.HANDLE], w.BOOL),
                ("FindCloseChangeNotification", [w.HANDLE], w.BOOL),
                ("CreateEventW", [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
                ("SetEvent", [w.HANDLE], w.BOOL), ("CloseHandle", [w.HANDLE], w.BOOL),
                ("WaitForMultipleObjects", [w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD], w.DWORD),
            )
            for name, args, result in signatures:
                function = getattr(self.kernel, name)
                function.argtypes, function.restype = args, result
            self.fd = self.kernel.FindFirstChangeNotificationW(str(directory), False, 0x1B)
            if not self.fd or self.fd == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.stop = self.kernel.CreateEventW(None, True, False, None)
            if not self.stop:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handles = (w.HANDLE * 2)(self.stop, self.fd)
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            libc.inotify_init1.argtypes, libc.inotify_init1.restype = [ctypes.c_int], ctypes.c_int
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            self.fd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), "inotify initialization failed")
            for directory_fd in self.spool.directories:
                mask = 0x0C00 | (0x03CA if directory_fd == self.spool.fd else 0)
                if libc.inotify_add_watch(self.fd, os.fsencode("/proc/self/fd/" + str(directory_fd)), mask) < 0:
                    raise OSError(ctypes.get_errno(), "inotify registration failed")
            self.stop_read, self.stop_write = os.pipe()
        elif sys.platform == "darwin":
            self.fd = os.dup(self.spool.fd)
            self.queue = select.kqueue()
            # Apple's bundled Python 3.9 exposes vnode/read filters, but not
            # EVFILT_USER. A pipe provides a native, cancellable wake instead.
            self.stop_read, self.stop_write = os.pipe()
            os.set_blocking(self.stop_write, False)
            self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                              fflags=select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE |
                                              (select.KQ_NOTE_WRITE if fd == self.spool.fd else 0))
                                for fd in self.spool.directories] + [
                select.kevent(self.stop_read, filter=select.KQ_FILTER_READ,
                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)], 0)
        else:
            raise RuntimeError("native job notifications are unavailable")

    def wait(self):
        if sys.platform == "win32":
            result = self.kernel.WaitForMultipleObjects(2, self.handles, False, 0xFFFFFFFF)
            if result == 0:
                return False
            if result != 1 or not self.kernel.FindNextChangeNotification(self.fd):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        if sys.platform == "darwin":
            events = self.queue.control(None, 8)
            if any(event.filter == select.KQ_FILTER_READ and event.ident == self.stop_read for event in events):
                return False
            if any(event.fflags & (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE) for event in events):
                raise RuntimeError("event spool was removed")
            return bool(events)
        ready, _, _ = select.select([self.fd, self.stop_read], [], [])
        if self.stop_read in ready:
            return False
        content = os.read(self.fd, 65536)
        offset = 0
        while offset < len(content):
            _, mask, _, length = struct.unpack_from("iIII", content, offset)
            if mask & (0x8000 | 0x0C00):
                raise RuntimeError("event spool watch was removed")
            offset += 16 + length
        return True  # overflow also triggers a bounded rescan

    def wake(self):
        with self._guard:
            if self.closed:
                return
            if sys.platform == "win32":
                self.kernel.SetEvent(self.stop)
            elif sys.platform == "darwin":
                try:
                    os.write(self.stop_write, b"x")
                except BlockingIOError:
                    pass  # An already signalled pipe still wakes the wait.
            else:
                os.write(self.stop_write, b"x")

    def close(self):
        """Called by the watching thread, after its wait exits."""
        with self._guard:
            if self.closed:
                return
            self.closed = True
            if sys.platform == "win32":
                if getattr(self, "fd", None) not in (None, 0, ctypes.c_void_p(-1).value):
                    self.kernel.FindCloseChangeNotification(self.fd)
                if getattr(self, "stop", None):
                    self.kernel.CloseHandle(self.stop)
            else:
                if hasattr(self, "queue"):
                    self.queue.close()
                for name in ("fd", "stop_read", "stop_write"):
                    fd = getattr(self, name, None)
                    if fd is not None and fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            self.spool.close()


def stream(directory, runner_id, output=sys.stdout, acknowledgements=sys.stdin):
    """Persistent SSH stream. Ack deletes only an event already durably copied."""
    directory = safe_directory(directory)
    watch = DirectoryChanges(directory)
    sent, lock = {}, threading.Lock()
    stop = threading.Event()

    def acknowledge():
        while True:
            line = acknowledgements.readline(200)
            if not line:
                stop.set()
                watch.wake()
                return
            event_id = line.strip()
            with lock:
                pending = sent.pop(event_id, None)
            if pending:
                try:
                    with watch._guard:
                        if not watch.closed:
                            watch.spool.acknowledge(*pending)
                except (OSError, ValueError):
                    pass

    threading.Thread(target=acknowledge, name="job-event-ack", daemon=True).start()
    try:
        while True:
            for filename, event in watch.spool.read(runner_id, stopped=stop.is_set):
                with lock:
                    if event["event_id"] in sent:
                        continue
                    sent[event["event_id"]] = filename, event
                output.write(json.dumps(event, separators=(",", ":")) + "\n")
                output.flush()
            if stop.is_set():
                return
            output.write(json.dumps({"ready": runner_id}) + "\n")
            output.flush()
            if not watch.wait():
                return
    finally:
        watch.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: job_event_stream.py <owned-spool> <runner-uuid>")
    stream(sys.argv[1], sys.argv[2])
