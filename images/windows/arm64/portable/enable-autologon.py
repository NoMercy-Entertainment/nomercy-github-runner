"""Enable local Windows autologon; read the credential only from standard input."""

import ctypes
from ctypes import wintypes
import json
import os
import sys
import winreg


class UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.c_void_p),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p)]


def unicode_string(value):
    buffer = ctypes.create_unicode_buffer(value)
    length = len(value.encode("utf-16-le"))
    if length > 65532:
        raise ValueError("Credential exceeds Windows string limit")
    return UnicodeString(length, length + 2, ctypes.addressof(buffer)), buffer


def enable(username, password):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.LogonUserW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.LogonUserW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.LsaOpenPolicy.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ObjectAttributes),
                                    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.LsaOpenPolicy.restype = wintypes.LONG
    advapi.LsaStorePrivateData.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(UnicodeString),
                                         ctypes.POINTER(UnicodeString)]
    advapi.LsaStorePrivateData.restype = wintypes.LONG
    advapi.LsaRetrievePrivateData.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(UnicodeString),
                                            ctypes.POINTER(ctypes.POINTER(UnicodeString))]
    advapi.LsaRetrievePrivateData.restype = wintypes.LONG
    advapi.LsaFreeMemory.argtypes = [ctypes.c_void_p]
    advapi.LsaFreeMemory.restype = wintypes.LONG
    advapi.LsaClose.argtypes = [wintypes.HANDLE]
    advapi.LsaClose.restype = wintypes.LONG
    advapi.LsaNtStatusToWinError.argtypes = [wintypes.LONG]
    advapi.LsaNtStatusToWinError.restype = wintypes.ULONG

    def check(status):
        if status:
            raise ctypes.WinError(advapi.LsaNtStatusToWinError(status))

    computer = os.environ["COMPUTERNAME"]
    token = wintypes.HANDLE()
    if not advapi.LogonUserW(username, computer, password, 2, 0, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle(token)

    attributes = ObjectAttributes()
    attributes.Length = ctypes.sizeof(attributes)
    policy = wintypes.HANDLE()
    check(advapi.LsaOpenPolicy(None, ctypes.byref(attributes), 0x20 | 0x04,
                              ctypes.byref(policy)))
    name, name_buffer = unicode_string("DefaultPassword")
    secret, secret_buffer = unicode_string(password)
    retrieved = ctypes.POINTER(UnicodeString)()
    try:
        check(advapi.LsaStorePrivateData(policy, ctypes.byref(name), ctypes.byref(secret)))
        check(advapi.LsaRetrievePrivateData(policy, ctypes.byref(name), ctypes.byref(retrieved)))
        stored = ctypes.wstring_at(retrieved.contents.Buffer,
                                   retrieved.contents.Length // 2)
        if stored != password:
            raise RuntimeError("Autologon secret verification failed")
        del stored
    finally:
        if retrieved:
            advapi.LsaFreeMemory(retrieved)
        advapi.LsaClose(policy)
        ctypes.memset(ctypes.addressof(secret_buffer), 0, ctypes.sizeof(secret_buffer))

    path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    access = winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access) as key:
        for name in ("AutoLogonCount", "DefaultPassword"):
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        for name, value in (("DefaultUserName", username),
                            ("DefaultDomainName", computer),
                            ("AutoAdminLogon", "1")):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            if winreg.QueryValueEx(key, name)[0] != value:
                raise RuntimeError("Autologon registry verification failed")
    return {"autologon": "enabled", "username": username,
            "computer": computer, "credential_verified": True,
            "secret_verified": True, "reboot_performed": False}


if __name__ == "__main__":
    try:
        credential = json.load(sys.stdin)
        result = enable(credential["username"], credential.pop("password"))
        print(json.dumps(result), flush=True)
    except Exception as error:
        # Do not include input, credentials or a traceback in logs.
        code = getattr(error, "winerror", None)
        print(json.dumps({"autologon": "error", "error_type": type(error).__name__,
                          "winerror": code}), flush=True)
        sys.exit(1)
