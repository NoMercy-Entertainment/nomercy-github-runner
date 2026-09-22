"""No privileged client may execute or deserialize runner-controlled code."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
import pytest

from agent import windows_registration as registration
from agent import jobhost

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


class Connection:
    def __init__(self, data):
        self.data, self.sent, self.closed = data, None, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def recv(self):
        pytest.fail("pickle receive is forbidden")

    def send(self, value):
        pytest.fail("pickle send is forbidden")

    def recv_bytes(self, limit):
        assert limit == registration.MAX_MESSAGE
        return self.data

    def send_bytes(self, value):
        self.sent = value

    def poll(self, timeout):
        return True

    def close(self):
        self.closed = True


def test_service_decodes_json_bytes_and_rejects_pickle_without_execution():
    connection = Connection(b'\x80\x04this is not JSON')
    with pytest.raises(ValueError):
        registration.serve_one(connection, lambda value: pytest.fail("must not execute"))
    assert connection.closed


def test_system_client_uses_json_bytes_and_validates_untrusted_response(tmp_path, monkeypatch):
    monkeypatch.setattr(registration, "key_path", lambda _: tmp_path / "key")
    (tmp_path / "key").write_bytes(b"k" * 32)
    connection = Connection(json.dumps({"ok": True, "out": "{}", "error": ""}).encode())
    answer = registration.request_registration(RID, {"token": "secret"},
                                                 connect=lambda *a, **k: connection)
    assert answer["ok"]
    assert json.loads(connection.sent) == {"plan": {"token": "secret"}}
    connection.data = b'\x80\x04malicious pickle'
    with pytest.raises(ValueError):
        registration.request_registration(RID, {}, connect=lambda *a, **k: connection)


def test_registration_script_executes_only_in_service_handler_and_keeps_token_off_argv(tmp_path):
    captured = {}

    def run(args, **kwargs):
        captured.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 1, "", "secret token")

    answer = registration.execute_registration(str(tmp_path), {"plan": {"token": "secret token"}},
                                                {"TEMP": "owned"}, run=run)
    assert "secret token" not in " ".join(captured["args"])
    assert json.loads(captured["input"])["token"] == "secret token"
    assert captured["env"] == {"TEMP": "owned"}
    assert captured["timeout"] == 105
    assert answer == {"ok": False, "out": "", "error": "***"}


def test_a_localsystem_jobhost_cannot_start_a_registration_endpoint(monkeypatch):
    monkeypatch.setattr(jobhost, "packaged", lambda: False)
    monkeypatch.setattr(registration.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, '"nt authority\\system","S-1-5-18"\n', ""))
    monkeypatch.setattr(jobhost, "read_unit", lambda *a: pytest.fail("must not read unit as SYSTEM"))
    assert jobhost.main(["--root", "D:/unused", "--runner-id", RID]) == 7


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe")
def test_real_named_pipe_rejects_wrong_key_then_accepts_json_request(tmp_path, monkeypatch):
    rid = str(uuid.uuid4())
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    monkeypatch.setattr(registration, "key_path", lambda _: key)
    seen = []

    def handler(value):
        seen.append(value)
        return {"ok": True, "out": '{"registration_id":"test"}', "error": ""}

    server = registration.start_server(rid, str(tmp_path), {}, handler=handler)
    try:
        with pytest.raises(AuthenticationError):
            Client(registration.pipe_name(rid), family="AF_PIPE", authkey=b"wrong" * 8)
        result = registration.request_registration(rid, {"name": "owned"}, timeout=10)
        assert result["ok"]
        assert seen == [{"plan": {"name": "owned"}}]
    finally:
        server.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe token semantics")
def test_real_pipe_server_receives_only_an_identification_token(tmp_path, monkeypatch):
    """Observe this test user's token level; never perform a privileged action."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
    advapi.ImpersonateNamedPipeClient.restype = wintypes.BOOL
    advapi.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                                      ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenThreadToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    rid = str(uuid.uuid4())
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    monkeypatch.setattr(registration, "key_path", lambda _: key)
    listener = Listener(registration.pipe_name(rid), family="AF_PIPE", authkey=key.read_bytes())
    observed, errors = [], []

    def serve():
        try:
            with listener.accept() as connection:
                def inspect_token(request):
                    assert request == {"plan": {"name": "test-only"}}
                    token = wintypes.HANDLE()
                    assert advapi.ImpersonateNamedPipeClient(connection.fileno()), ctypes.get_last_error()
                    try:
                        assert advapi.OpenThreadToken(kernel.GetCurrentThread(), 0x0008, True,
                                                      ctypes.byref(token)), ctypes.get_last_error()
                        level, length = wintypes.DWORD(), wintypes.DWORD()
                        assert advapi.GetTokenInformation(token, 9, ctypes.byref(level),
                                                          ctypes.sizeof(level), ctypes.byref(length)), ctypes.get_last_error()
                        observed.append(level.value)
                    finally:
                        assert advapi.RevertToSelf(), ctypes.get_last_error()
                        if token.value:
                            kernel.CloseHandle(token)
                    return {"ok": True, "out": "{}", "error": ""}
                registration.serve_one(connection, inspect_token)
        except BaseException as exc:
            errors.append(exc)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        assert registration.request_registration(rid, {"name": "test-only"}, timeout=10)["ok"]
        server.join(timeout=10)
        assert not server.is_alive()
        assert not errors
        assert observed == [1], "SecurityIdentification only; SecurityImpersonation is 2"
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe")
def test_unresponsive_pipe_authentication_respects_client_timeout(tmp_path, monkeypatch):
    rid = str(uuid.uuid4())
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    monkeypatch.setattr(registration, "key_path", lambda _: key)
    listener = Listener(registration.pipe_name(rid), family="AF_PIPE")
    finished = threading.Event()

    def serve():
        with listener.accept():
            finished.wait(5)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="authentication timed out"):
            registration.request_registration(rid, {}, timeout=0.2)
        assert time.monotonic() - started < 3
    finally:
        finished.set()
        server.join(timeout=5)
        listener.close()
