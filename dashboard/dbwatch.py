"""Native notifications for SQLite and its WAL; no periodic database reads."""
import ctypes
import os
from pathlib import Path
import select
import struct
import sys


class DatabaseChanges:
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent
        self.closed = False
        if sys.platform == "win32":
            from ctypes import wintypes as w
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = (
                ("FindFirstChangeNotificationW", [w.LPCWSTR, w.BOOL, w.DWORD], w.HANDLE),
                ("FindNextChangeNotification", [w.HANDLE], w.BOOL),
                ("FindCloseChangeNotification", [w.HANDLE], w.BOOL),
                ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            )
            for name, args, result in signatures:
                function = getattr(self.kernel, name)
                function.argtypes, function.restype = args, result
            self.fd = self.kernel.FindFirstChangeNotificationW(str(self.directory), False, 0x19)
            if not self.fd or self.fd == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        elif sys.platform.startswith("linux"):
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_init1.argtypes = [ctypes.c_int]
            self.libc.inotify_init1.restype = ctypes.c_int
            self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self.libc.inotify_add_watch.restype = ctypes.c_int
            self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1 failed")
            if self.libc.inotify_add_watch(self.fd, os.fsencode(self.directory), 0x03CA) < 0:
                os.close(self.fd)
                raise OSError(ctypes.get_errno(), "inotify_add_watch failed")
        elif sys.platform == "darwin":
            self.fd = os.open(self.directory, os.O_RDONLY)
            self.queue = select.kqueue()
            event = select.kevent(self.fd, filter=select.KQ_FILTER_VNODE,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                  fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE)
            self.queue.control([event], 0)
            self.mac_files = {}
            self._refresh_mac_files()
        else:
            raise RuntimeError("Native database notifications are unavailable on this platform")

    def wait(self, timeout=None):
        if self.closed:
            raise RuntimeError("Database watch is closed")
        if sys.platform == "win32":
            milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
            result = self.kernel.WaitForSingleObject(self.fd, milliseconds)
            if result == 258:
                return False
            if result != 0 or not self.kernel.FindNextChangeNotification(self.fd):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        if sys.platform == "darwin":
            events = self.queue.control(None, 16, timeout)
            if any(event.ident == self.fd and event.fflags &
                   (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE) for event in events):
                raise RuntimeError("Database directory was removed")
            if events:
                self._refresh_mac_files()
            return bool(events)
        if not select.select([self.fd], [], [], timeout)[0]:
            return False
        events = os.read(self.fd, 65536)
        offset, changed = 0, False
        while offset < len(events):
            _, mask, _, length = struct.unpack_from("iIII", events, offset)
            name = os.fsdecode(events[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
            offset += 16 + length
            if mask & 0x8000:
                raise RuntimeError("Database watch was removed")
            changed |= bool(mask & 0x4000) or name in {
                self.path.name, self.path.name + "-wal", self.path.name + "-journal", "users.json"}
        return changed

    def _refresh_mac_files(self):
        # kqueue on a directory sees replacements, but an existing WAL's
        # append needs a vnode watch on that file itself.
        for name in (self.path.name, self.path.name + "-wal", self.path.name + "-journal", "users.json"):
            path = self.directory / name
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                identity = None
            previous = self.mac_files.get(name)
            if previous and previous[1] != identity:
                os.close(previous[0])
                self.mac_files.pop(name)
            if identity is not None and name not in self.mac_files:
                try:
                    fd = os.open(path, os.O_RDONLY)
                except FileNotFoundError:
                    continue
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                      flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                      fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE)
                self.queue.control([event], 0)
                self.mac_files[name] = (fd, identity)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if sys.platform == "win32":
            self.kernel.FindCloseChangeNotification(self.fd)
        else:
            if hasattr(self, "queue"):
                self.queue.close()
                for fd, _ in self.mac_files.values():
                    os.close(fd)
            os.close(self.fd)
