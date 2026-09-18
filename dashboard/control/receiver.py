"""Where agents talk to the controller: heartbeats, and events (T-0405).

The mirror of `agent_client`. There the controller calls an agent and proves
who answered; here an agent calls the controller and the controller proves who
is calling - the same three ways. The client certificate must chain to the
control plane's authority, its subject must be a worker in the inventory, and
its fingerprint must be the one pinned for that worker.

**Identity comes from the certificate, never from the payload.** A heartbeat
names its sender, and that name is compared with the certificate's and refused
if they differ. Without that, any worker could beat on behalf of any other -
keeping a dead worker looking healthy, or reporting another worker's runners.

A caller that fails any check is refused before its body is read, and the
refusal goes to the audit trail. Nothing an agent sends is logged.

Not in the plan's file list. T-0404 needs heartbeats to reach the controller
and T-0405 needs events to, and the plan names the two ends but not the door
between them; this is the door.
"""
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audit
from .ca import fingerprint

HEARTBEAT_PATH = "/v1/heartbeat"
EVENT_PATH = "/v1/event"
MAX_BODY = 256 * 1024
HANDSHAKE_TIMEOUT = 10


def _common_name(peer_cert):
    for rdn in (peer_cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "runner-controller"
    sys_version = ""

    def log_message(self, fmt, *args):
        return

    def _reply(self, status, payload=None):
        data = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status, reason, what):
        audit.record(self.server.audit_path, verb=what,
                     decision=f"refused: {reason}",
                     parameters={"peer": self.server.peer_of(self)})
        self.close_connection = True
        self._reply(status, {"ok": False, "error": reason})

    def do_POST(self):                      # noqa: N802
        routes = {HEARTBEAT_PATH: "heartbeat", EVENT_PATH: "event"}
        what = routes.get(self.path)
        if what is None:
            self.close_connection = True
            return self._reply(404, {"ok": False, "error": "no such path"})

        host_id, reason = self.server.identify(self)
        if host_id is None:
            return self._refuse(403, reason, what)

        refusal = self.server.admit(self, host_id, what)
        if refusal is not None:
            return self._refuse(*refusal, what)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._refuse(400, "bad-length", what)
        if length < 0 or length > MAX_BODY:
            return self._refuse(413, "too-large", what)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._refuse(400, "not-json", what)

        try:
            result = self.server.take(what, host_id, body)
        except ValueError as e:
            return self._refuse(400, f"rejected: {e}", what)
        except Exception:                   # noqa: BLE001
            return self._refuse(500, "failed", what)
        self._reply(200, {"ok": True, "result": result})

    def _not_allowed(self):
        self.close_connection = True
        self._reply(405, {"ok": False, "error": "only POST is served"})

    do_GET = do_PUT = do_DELETE = do_PATCH = _not_allowed


class Receiver(ThreadingHTTPServer):
    """The controller's endpoint for agents. `start()` serves in the
    background."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, inventory, ssl_context, address=("127.0.0.1", 0),
                 on_event=None, audit_path=None):
        super().__init__(address, _Handler)
        self.inventory = inventory
        self.ssl_context = ssl_context
        self.on_event = on_event
        self.audit_path = audit_path or inventory.path
        self._thread = None

    # ---- TLS, on the request's own thread -----------------------------------

    def finish_request(self, request, client_address):
        request.settimeout(HANDSHAKE_TIMEOUT)
        try:
            request = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            audit.record(self.audit_path, verb="connect",
                         decision=f"refused: handshake ({type(e).__name__})",
                         parameters={"peer": client_address[0]})
            _close(request)
            return
        request.settimeout(None)
        super().finish_request(request, client_address)

    # ---- who is calling -----------------------------------------------------

    def peer_of(self, handler):
        try:
            return _common_name(handler.connection.getpeercert())
        except (AttributeError, ValueError):
            return None

    def identify(self, handler):
        """(host_id, None) for an enrolled worker presenting its pinned
        certificate, or (None, reason)."""
        subject = self.peer_of(handler)
        if not subject:
            return None, "no-subject"
        worker = self.inventory.get(subject)
        if worker is None:
            return None, "unknown-worker"
        pinned = worker.get("certificate_fingerprint")
        if not pinned:
            return None, "not-pinned"
        presented = fingerprint(handler.connection.getpeercert(
            binary_form=True))
        if presented != pinned:
            return None, "fingerprint-changed"
        return subject, None

    def admit(self, handler, host_id, what):
        """A further check on an identified caller; None lets it through.
        T-0406 checks the protocol version here."""
        return None

    # ---- what they send -----------------------------------------------------

    def take(self, what, host_id, body):
        if what == "heartbeat":
            return self.inventory.accept_heartbeat(host_id, body)
        if self.on_event is None:
            raise ValueError("events are not accepted here")
        return self.on_event(host_id, body)

    # ---- running ------------------------------------------------------------

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="controller-receiver",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()


def _close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def server_context(cert_file, key_file, ca_file):
    """The receiver's side: present the controller's receiving certificate,
    demand a worker's."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_file, key_file)
    context.load_verify_locations(ca_file)
    return context
