"""Registration executes inside the runner's own service, never as the agent.

The privileged client exchanges bounded JSON bytes over an authenticated local
named pipe. In particular, Connection.recv()/send() (pickle) are forbidden:
the server belongs to a less privileged account than its client.
"""
import argparse
import csv
import io
import json
from multiprocessing.connection import Listener, answer_challenge, deliver_challenge
from multiprocessing import AuthenticationError
import ntpath
import os
import subprocess
import sys
import threading
import time

from . import naming

KEY_ROOT = r"C:\ProgramData\nomercy\runner-keys"
MAX_MESSAGE = 64 * 1024
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
WHOAMI = r"C:\Windows\System32\whoami.exe"
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000


class _AuthenticationDeadline:
    """Bound both standard-library HMAC exchanges, without decoding objects."""

    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def recv_bytes(self, maxlength):
        if not self.connection.poll(max(0, self.deadline - time.monotonic())):
            raise TimeoutError("runner registration authentication timed out")
        return self.connection.recv_bytes(maxlength)

    def send_bytes(self, value):
        self.connection.send_bytes(value)


def _pipe_client(address, *, family, authkey, timeout):
    """Open a pipe without allowing its server to act as this privileged client.

    multiprocessing.Client omits SQOS, granting the server an impersonation
    token. Identification permits the HMAC/JSON exchange while denying that
    server the ability to use the client's SYSTEM permissions.
    """
    import _winapi
    from multiprocessing.connection import PipeConnection
    if family != "AF_PIPE" or not isinstance(authkey, bytes):
        raise ValueError("registration requires an authenticated Windows pipe")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("runner registration pipe connection timed out")
        try:
            _winapi.WaitNamedPipe(address, max(1, min(1000, int(remaining * 1000))))
            handle = _winapi.CreateFile(
                address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                _winapi.NULL)
            break
        except OSError as error:
            if error.winerror not in (2, _winapi.ERROR_SEM_TIMEOUT, _winapi.ERROR_PIPE_BUSY):
                raise
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    connection = PipeConnection(handle)
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        bounded = _AuthenticationDeadline(connection, deadline)
        answer_challenge(bounded, authkey)
        deliver_challenge(bounded, authkey)
        return connection
    except BaseException:
        connection.close()
        raise


def key_path(runner_id):
    return ntpath.join(KEY_ROOT, naming.check(runner_id) + ".key")


def pipe_name(runner_id):
    return r"\\.\pipe\nomercy-register-" + naming.check(runner_id)


def require_service_identity(runner_id):
    """A misconfigured LocalSystem service must never host this endpoint."""
    from .runtimes.windows_process import service_sid
    result = subprocess.run([WHOAMI, "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, timeout=10)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    expected = service_sid(naming.unit_name(runner_id))
    if result.returncode or len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != expected:
        raise RuntimeError("registration endpoint requires its own service virtual account")


def execute_registration(root, request, env, run=subprocess.run):
    if not isinstance(request, dict) or set(request) != {"plan"} or not isinstance(request["plan"], dict):
        raise ValueError("registration request must contain a plan")
    script = os.path.join(root, "reg", "register.ps1")
    result = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                  "Bypass", "-File", script], input=json.dumps(request["plan"]),
                 capture_output=True, text=True, timeout=105, cwd=os.path.join(root, "reg"), env=env)
    token = str(request["plan"].get("token") or "")
    out, error = result.stdout[-MAX_MESSAGE // 3:], result.stderr[-MAX_MESSAGE // 3:]
    if token:
        out, error = out.replace(token, "***"), error.replace(token, "***")
    return {"ok": result.returncode == 0, "out": out, "error": error}


def serve_one(connection, handler):
    """Safe even if the peer knows its own runner key: decode no objects."""
    try:
        if not connection.poll(10):
            return
        request = json.loads(connection.recv_bytes(MAX_MESSAGE))
        try:
            answer = handler(request)
        except (OSError, ValueError, subprocess.SubprocessError):
            answer = {"ok": False, "out": "", "error": "registration process failed"}
        connection.send_bytes(json.dumps(answer).encode("utf-8"))
    finally:
        connection.close()


class RegistrationServer:
    def __init__(self, listener, handler):
        self.listener, self.handler = listener, handler
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="runner-registration", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.closed.is_set():
            try:
                connection = self.listener.accept()
                serve_one(connection, self.handler)
            except (OSError, ValueError, EOFError, AuthenticationError):
                continue

    def close(self):
        self.closed.set()
        self.listener.close()


def start_server(runner_id, root, env, handler=None):
    with open(key_path(runner_id), "rb") as stream:
        key = stream.read()
    if len(key) != 32:
        raise RuntimeError("registration key must be 32 bytes")
    listener = Listener(pipe_name(runner_id), family="AF_PIPE", authkey=key)

    return RegistrationServer(listener, handler or (
        lambda request: execute_registration(root, request, env)))


def request_registration(runner_id, plan, timeout=115, connect=None):
    with open(key_path(runner_id), "rb") as stream:
        key = stream.read()
    if len(key) != 32:
        raise RuntimeError("registration key must be 32 bytes")
    data = json.dumps({"plan": plan}).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("registration request is too large")
    deadline = time.monotonic() + timeout
    connector = connect or _pipe_client
    with connector(pipe_name(runner_id), family="AF_PIPE", authkey=key,
                   timeout=timeout) as connection:
        connection.send_bytes(data)
        if not connection.poll(max(0, deadline - time.monotonic())):
            raise RuntimeError("runner registration service did not answer")
        answer = json.loads(connection.recv_bytes(MAX_MESSAGE))
    if (not isinstance(answer, dict) or set(answer) != {"ok", "out", "error"}
            or type(answer["ok"]) is not bool or not isinstance(answer["out"], str)
            or not isinstance(answer["error"], str)):
        raise RuntimeError("runner registration service returned invalid JSON")
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-id", required=True)
    args = parser.parse_args(argv)
    try:
        plan = json.load(sys.stdin)
        if not isinstance(plan, dict):
            raise ValueError("registration plan must be an object")
        answer = request_registration(args.runner_id, plan)
        sys.stdout.write(answer["out"])
        sys.stderr.write(answer["error"])
        return 0 if answer["ok"] else 1
    except (OSError, ValueError, RuntimeError, EOFError, AuthenticationError) as exc:
        print(f"runner registration service unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
