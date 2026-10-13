"""Read-only owned VHDX measurements through Win32, without a shell/CIM host.

The image, device ID, partition, mount paths and NTFS volume must agree.
Mutation/startup still use the serialized PowerShell storage protocol.
"""
import ctypes
from ctypes import wintypes as w
from contextlib import contextmanager
import json
import ntpath
import os
import re
import stat
import struct
import uuid

from ..job_event_stream import OwnedSpool
from .. import naming


def _identifiers(data):
    if len(data) < 12:
        raise RuntimeError("truncated disk identity descriptor")
    _, size, count = struct.unpack_from("<III", data)
    if not 12 <= size <= len(data) or not 1 <= count <= 64:
        raise RuntimeError("invalid disk identity descriptor")
    out, offset = set(), 12
    for index in range(count):
        if offset + 16 > size:
            raise RuntimeError("truncated disk identifier")
        code, kind, length, following, association = struct.unpack_from("<IIHHI", data, offset)
        if not length or offset + 16 + length > size:
            raise RuntimeError("invalid disk identifier length")
        value = data[offset + 16:offset + 16 + length]
        if association == 0 and code == 1:
            out.add(value.hex().upper())
        if index + 1 < count:
            if following < 16 + length or offset + following >= size:
                raise RuntimeError("invalid disk identifier chain")
            offset += following
    return out


def _partition(data, expected):
    if len(data) < 48:
        raise RuntimeError("truncated GPT layout")
    style, count = struct.unpack_from("<II", data)
    if style != 1 or not 1 <= count <= 128 or 48 + count * 144 > len(data):
        raise RuntimeError("invalid GPT layout")
    found = []
    for index in range(count):
        offset = 48 + index * 144
        if struct.unpack_from("<I", data, offset)[0] != 1:
            continue
        identifier = str(uuid.UUID(bytes_le=bytes(data[offset + 48:offset + 64])))
        if identifier == str(uuid.UUID(expected)):
            bounds = struct.unpack_from("<qq", data, offset + 8)
            if bounds[0] < 0 or bounds[1] <= 0:
                raise RuntimeError("invalid owned partition bounds")
            found.append(bounds)
    if len(found) != 1:
        raise RuntimeError("owned partition identity is missing or ambiguous")
    return found[0]


class NativeDisk:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.v = ctypes.WinDLL("virtdisk", use_last_error=True)
        self.k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                      w.DWORD, w.DWORD, w.HANDLE]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
                                          ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        self.k.DeviceIoControl.restype = w.BOOL
        self.k.GetFileAttributesW.argtypes = [w.LPCWSTR]
        self.k.GetFileAttributesW.restype = w.DWORD
        self.k.GetVolumeNameForVolumeMountPointW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
        self.k.GetVolumePathNamesForVolumeNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD)]
        self.k.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD,
                                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, w.LPWSTR, w.DWORD]
        self.k.GetDiskFreeSpaceExW.argtypes = [w.LPCWSTR, *([ctypes.POINTER(ctypes.c_ulonglong)] * 3)]
        self.v.OpenVirtualDisk.argtypes = [ctypes.c_void_p, w.LPCWSTR, w.DWORD, w.DWORD,
                                          ctypes.c_void_p, ctypes.POINTER(w.HANDLE)]
        self.v.GetVirtualDiskInformation.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
        self.v.GetVirtualDiskPhysicalPath.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD), w.LPWSTR]

    @staticmethod
    def check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def plain(self, path, missing=False):
        attributes = self.k.GetFileAttributesW(path)
        if attributes == 0xffffffff and missing and ctypes.get_last_error() in (2, 3):
            return
        if attributes == 0xffffffff or attributes & 0x400:
            raise RuntimeError("owned storage path is missing or redirects IO")

    @contextmanager
    def device(self, path):
        handle = self.k.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
        self.check(handle != ctypes.c_void_p(-1).value)
        try:
            yield handle
        finally:
            self.k.CloseHandle(handle)

    def ioctl(self, handle, code, request=b""):
        data = ctypes.create_string_buffer(65536)
        incoming = ctypes.create_string_buffer(request) if request else None
        used = w.DWORD()
        self.check(self.k.DeviceIoControl(handle, code, incoming, len(request), data,
                                         len(data), ctypes.byref(used), None))
        if used.value > len(data):
            raise RuntimeError("oversized disk descriptor")
        return data.raw[:used.value]

    def volume(self, mount, expected):
        result = ctypes.create_unicode_buffer(128)
        self.check(self.k.GetVolumeNameForVolumeMountPointW(mount, result, len(result)))
        if result.value.casefold() != expected.casefold():
            raise RuntimeError("runner volume identity changed")
        paths = ctypes.create_unicode_buffer(32768)
        required = w.DWORD()
        self.check(self.k.GetVolumePathNamesForVolumeNameW(expected, paths, len(paths), ctypes.byref(required)))
        names = ctypes.wstring_at(paths, required.value).split("\0")
        if {p.casefold() for p in names if p} != {mount.casefold()}:
            raise RuntimeError("owned volume has unexpected access paths")
        filesystem = ctypes.create_unicode_buffer(32)
        self.check(self.k.GetVolumeInformationW(expected, None, 0, None, None, None, filesystem, len(filesystem)))
        if filesystem.value != "NTFS":
            raise RuntimeError("owned filesystem is not NTFS")

    def measure(self, state):
        self.plain(state["image"])
        # VIRTUAL_STORAGE_TYPE: device ID plus Microsoft vendor GUID.
        storage_type = ctypes.create_string_buffer(struct.pack("<I", 3) + uuid.UUID("EC984AEC-A0F9-47e9-901F-71415A66345B").bytes_le)
        handle = w.HANDLE()
        rc = self.v.OpenVirtualDisk(storage_type, state["image"], 0x80000, 0, None, ctypes.byref(handle))
        if rc:
            raise ctypes.WinError(rc)
        try:
            def info(version):
                data = ctypes.create_string_buffer(64)
                struct.pack_into("<I", data, 0, version)
                size, used = w.DWORD(len(data)), w.DWORD()
                result = self.v.GetVirtualDiskInformation(handle, ctypes.byref(size), data, ctypes.byref(used))
                if result:
                    raise ctypes.WinError(result)
                return data.raw
            if struct.unpack_from("<Q", info(1), 8)[0] != state["limit"] or struct.unpack_from("<I", info(7), 8)[0] != 2:
                raise RuntimeError("backing image size/type changed")
            path = ctypes.create_unicode_buffer(256)
            size = w.DWORD(ctypes.sizeof(path))
            rc = self.v.GetVirtualDiskPhysicalPath(handle, ctypes.byref(size), path)
            if rc:
                raise ctypes.WinError(rc)
            match = re.fullmatch(r"\\\\\.\\PhysicalDrive([0-9]+)", path.value, re.I)
            if not match:
                raise RuntimeError("owned image is not attached to a local disk")
            self.volume(state["mount"], state["volume_guid"])
            with self.device(path.value) as disk:
                ids = _identifiers(self.ioctl(disk, 0x2d1400, struct.pack("<III", 2, 0, 0)))
                if state["disk_id"].upper() not in ids:
                    raise RuntimeError("attached disk identity changed")
                partition = _partition(self.ioctl(disk, 0x70050), state["partition_id"])
            with self.device(state["volume_guid"].rstrip("\\")) as volume:
                extents = self.ioctl(volume, 0x560000)
                if len(extents) < 32 or struct.unpack_from("<I", extents)[0] != 1:
                    raise RuntimeError("owned volume spans unexpected devices")
                number = struct.unpack_from("<I", extents, 8)[0]
                bounds = struct.unpack_from("<qq", extents, 16)
                if number != int(match[1]) or bounds != partition:
                    raise RuntimeError("volume differs from the owned image partition")
            for area in ("work", "cache", "reg", "logs", "tmp"):
                self.plain(ntpath.join(state["volume_guid"], area), missing=True)
            free, total, unused = (ctypes.c_ulonglong() for _ in range(3))
            self.check(self.k.GetDiskFreeSpaceExW(state["volume_guid"], ctypes.byref(free), ctypes.byref(total), ctypes.byref(unused)))
            self.volume(state["mount"], state["volume_guid"])
            if not 0 <= free.value <= total.value <= state["limit"] or not total.value:
                raise RuntimeError("invalid owned disk capacity")
            return {"runner_id": state["runner_id"], "volume_guid": state["volume_guid"],
                    "virtual_bytes": state["limit"], "capacity_bytes": total.value, "free_bytes": free.value}
        finally:
            self.k.CloseHandle(handle)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("duplicate storage manifest field")
        result[key] = value
    return result


def _read_manifest(directory, manifest):
    import msvcrt
    handle = directory._windows_open(manifest)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        directory.kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
            raise RuntimeError("storage manifest must be bounded and singly linked")
        data = stream.read(16385)
    if len(data) > 16384:
        raise RuntimeError("oversized storage manifest")
    return json.loads(data, object_pairs_hook=_unique)


def _validate(state, root, rid):
    mount = ntpath.join(naming.WINDOWS_ROOT, rid) + "\\"
    if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1 or
            state.get("runner_id") != rid or state.get("image") != ntpath.join(root, rid + ".vhdx") or
            state.get("mount") != mount or state.get("stage") != "ready" or state.get("type") != "Fixed" or
            state.get("volume_acl") is not True or type(state.get("limit")) is not int or
            state["limit"] < 1024**3 or state["limit"] % (1024**2) or
            not isinstance(state.get("disk_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["disk_id"], re.I) or
            not isinstance(state.get("volume_guid"), str) or
            not re.fullmatch(r"\\\\\?\\Volume\{[0-9a-f-]{36}\}\\", state["volume_guid"], re.I)):
        raise RuntimeError("owned storage manifest is incomplete or inconsistent")
    try:
        uuid.UUID(state["partition_id"])
        uuid.UUID(state["volume_guid"][11:-2])
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("invalid owned partition/volume identity") from error
    return state


def measure(root, runner_id, native=None):
    rid = naming.check(runner_id)
    # Hold all protected metadata ancestors against junction replacement.
    directory = OwnedSpool(root)
    try:
        native = native or NativeDisk()
        manifest = ntpath.join(root, rid + ".json")
        try:
            state = _read_manifest(directory, manifest)
        except FileNotFoundError as error:
            from .windows_storage import UNMANAGED
            raise RuntimeError(UNMANAGED) from error
        return native.measure(_validate(state, root, rid))
    finally:
        directory.close()
