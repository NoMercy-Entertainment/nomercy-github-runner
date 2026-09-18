"""The agent's HTTP endpoint: one path per verb, and nothing else.

`POST /v1/op/<verb>` with a JSON body. The verb is in the path rather than the
body - unlike the example in design 13.3, which is updated to match - because
the plan asks for an unknown verb to be refused before any parsing, and a verb
inside the body cannot be known without parsing it. Here an unknown verb is a
404 decided from the request line alone: the body is never read, so a hostile
or malformed one never reaches the JSON parser.

Only POST is served. Every other method is a 405, and every path outside
`/v1/op/` a 404.

**Bodies are never logged.** The default request log of `http.server` is
replaced: it would print to stderr, and while it only prints the request line,
the one body this agent routinely receives carries a registration token. The
rule is kept absolute so it does not depend on remembering which verb that is.
"""
import collections
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import protocol, tls
from .verbs import VERBS, Refused, dispatch

#: No legitimate body is anywhere near this. A unit spec with a full
#: environment is a few kilobytes.
MAX_BODY = 256 * 1024


def _verb_from(path):
    """The verb named by a request path, or None when the path names none."""
    if not path.startswith(protocol.OP_PATH):
        return None
    verb = path[len(protocol.OP_PATH):]
    return verb if verb in VERBS else None


class _Handler(BaseHTTPRequestHandler):
    server_version = "runner-agent"
    sys_version = ""

    def log_message(self, fmt, *args):     # noqa: D401 - see module docstring
        return

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status, reason):
        # The connection is closed after a refusal: the body may not have been
        # read, and a keep-alive connection would read it as the next request.
        self.close_connection = True
        self._reply(status, {"ok": False, "error": reason})

    def do_POST(self):                      # noqa: N802 - http.server's name
        verb = _verb_from(self.path)
        if verb is None:
            return self._refuse(404, "no such verb")

        # Decided from the request line and headers alone, before the body.
        refusal = self.server.admit(self, verb)
        if refusal is not None:
            return self._refuse(*refusal)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._refuse(400, "Content-Length is not a number")
        if length < 0 or length > MAX_BODY:
            return self._refuse(413, "body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._refuse(400, "body is not JSON")

        try:
            result = self.server.handle_verb(self, verb, body)
        except Refused as e:
            return self._refuse(e.status, e.reason)
        except Exception:                   # noqa: BLE001
            # The detail stays on the worker. An exception message can carry
            # anything the runtime was holding, including a token.
            return self._refuse(500, "the agent failed to carry out the verb")
        status, payload = result
        self._reply(status, payload)

    def _method_not_allowed(self):
        self.close_connection = True
        self._reply(405, {"ok": False, "error": "only POST is served"})

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_not_allowed


class AgentServer(ThreadingHTTPServer):
    """The agent's server. `start()` serves on a background thread.

    `admit` decides whether a request may go on, from its verb and headers,
    before the body is read. `handle_verb` is what runs an admitted request;
    the default runs it synchronously, and T-0405 replaces that with an
    asynchronous one.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, agent, address=("127.0.0.1", 0), ssl_context=None,
                 controller_subject=tls.CONTROLLER_SUBJECT):
        super().__init__(address, _Handler)
        self.agent = agent
        self.ssl_context = ssl_context
        self.controller_subject = controller_subject
        #: Connections refused before a request was read, with why. Kept in
        #: memory for the heartbeat to report; never includes what was sent.
        self.refusals = collections.deque(maxlen=200)
        self._thread = None

    def finish_request(self, request, client_address):
        """Handshake here, on the request's own thread, then serve.

        A plain-text client, one without a certificate, one whose certificate
        another authority issued, and one whose certificate names anything but
        the controller are all dropped before a byte of HTTP is read.
        """
        if self.ssl_context is not None:
            request.settimeout(tls.HANDSHAKE_TIMEOUT)
            try:
                request = self.ssl_context.wrap_socket(request,
                                                       server_side=True)
            except (ssl.SSLError, OSError) as e:
                self.refusals.append(("handshake", type(e).__name__))
                _close(request)
                return
            subject = tls.common_name(request.getpeercert())
            if subject != self.controller_subject:
                self.refusals.append(("subject", subject or ""))
                _close(request)
                return
            request.settimeout(None)
        super().finish_request(request, client_address)

    def admit(self, handler, verb):
        """None to let a request through, or (status, reason) to refuse it.

        A verb this worker is not configured to serve is a 403 here, before
        the body is read - the same point an unknown verb is refused at, and
        for the same reason: a body that will not be acted on is not parsed.
        """
        if verb not in self.agent.permitted:
            self.refusals.append(("not-permitted", verb))
            return 403, "not permitted on this worker"
        return None

    def handle_verb(self, handler, verb, body):
        return 200, {"ok": True, "result": dispatch(self.agent, verb, body)}

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        # A short poll so `stop()` returns promptly; the loop does nothing
        # between polls but check whether it has been asked to stop.
        self._thread = threading.Thread(target=self.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="agent-server", daemon=True)
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
