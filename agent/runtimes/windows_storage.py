"""Opt-in, fixed-size Windows runner disks. Never adopts an existing directory.

The privileged helper accepts a closed operation and UUID over JSON stdin;
paths come only from local worker configuration. Its manifest and VHDX live
outside the runner-writable volume. Every startup also proves the expected
volume through an unprivileged Win32 query in jobhost.
"""
import ctypes
import json
import ntpath
from pathlib import Path
import re

from .. import naming

DEFAULT_LIMIT = 100 * 1024 ** 3
DEFAULT_RESERVE = 20 * 1024 ** 3
MIN_LIMIT = 1024 ** 3
#: What the helper says when a runner has no owned disk at all - a plain
#: directory it will not adopt. The runtime measures such a runner as the
#: plain directory it is; any other refusal stays an error.
UNMANAGED = "No owned storage manifest"
_VOLUME = re.compile(r"^\\\\\?\\Volume\{[0-9a-f-]{36}\}\\$", re.I)


class WindowsStorage:
    def __init__(self, config, run, powershell):
        self.root = ntpath.normpath(config.get("root", r"D:\runner-disks"))
        drive, tail = ntpath.splitdrive(self.root)
        if (not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\")
                or tail == "\\" or ":" in tail or any(c in self.root for c in '*?"<>|')):
            raise ValueError("Windows storage root must be an absolute local directory")
        runner_root = ntpath.normcase(naming.WINDOWS_ROOT)
        storage_root = ntpath.normcase(self.root)
        if (storage_root == runner_root or storage_root.startswith(runner_root + "\\")
                or runner_root.startswith(storage_root + "\\")):
            raise ValueError("storage root must be separate from runner directories")
        self.default_limit = self._limit(config.get("default_limit", DEFAULT_LIMIT))
        self.reserve = config.get("reserve_bytes", DEFAULT_RESERVE)
        if type(self.reserve) is not int or self.reserve < 0:
            raise ValueError("reserve_bytes must be a nonnegative integer")
        self._run, self._powershell = run, powershell

    @staticmethod
    def _limit(value):
        if type(value) is not int or value < MIN_LIMIT or value % (1024 * 1024):
            raise ValueError("disk_limit must be whole MiB and at least 1 GiB")
        return value

    def _call(self, action, runner_id, limit=None):
        request = {"action": action, "runner_id": naming.check(runner_id),
                   "root": self.root, "runner_root": naming.WINDOWS_ROOT,
                   "reserve_bytes": self.reserve, "limit": limit}
        script = str(Path(__file__).with_name("windows_storage.ps1"))
        ok, out, err = self._run([
            self._powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
            "Bypass", "-File", script], input=json.dumps(request),
            # A verify may wait up to 8 s for the host's storage lock first.
            timeout=30 if action == "verify" else 3600)
        if not ok:
            raise RuntimeError("runner storage: " + (err or out or "helper failed"))
        try:
            answer = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError("runner storage helper returned no valid result") from exc
        if not isinstance(answer, dict) or answer.get("runner_id") != request["runner_id"]:
            raise RuntimeError("runner storage helper returned the wrong identity")
        if action != "remove" and not _VOLUME.fullmatch(answer.get("volume_guid", "")):
            raise RuntimeError("runner storage helper returned no volume identity")
        if action != "remove":
            keys = ("virtual_bytes", "capacity_bytes", "free_bytes")
            if (any(type(answer.get(key)) is not int for key in keys)
                    or not 0 <= answer["free_bytes"] <= answer["capacity_bytes"]
                    <= answer["virtual_bytes"] or answer["capacity_bytes"] <= 0):
                raise RuntimeError("runner storage helper returned invalid capacity")
        return answer

    def ensure(self, runner_id, limit=None):
        return self._call("ensure", runner_id,
                          self._limit(limit if limit is not None else self.default_limit))

    def mount(self, runner_id):
        return self._call("mount", runner_id)

    def verify(self, runner_id):
        return self._call("verify", runner_id)

    def remove(self, runner_id):
        return self._call("remove", runner_id)


def verify_volume(root, expected, kernel=None):
    """No administrative token needed; refuse a plain path or another volume.

    ``expected`` comes from the protected service command line, not unit.json
    on the writable runner disk. This check precedes reading that unit file.
    """
    if not isinstance(expected, str) or not _VOLUME.fullmatch(expected):
        raise RuntimeError("invalid expected runner volume identity")
    kernel = kernel or ctypes.WinDLL("kernel32", use_last_error=True)
    result = ctypes.create_unicode_buffer(128)
    if not kernel.GetVolumeNameForVolumeMountPointW(
            str(root).rstrip("\\/") + "\\", result, len(result)):
        raise RuntimeError("runner volume is not mounted at its owned directory")
    if result.value.casefold() != expected.casefold():
        raise RuntimeError("runner volume identity does not match its service")
    return True
