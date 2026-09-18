"""The agent's line to the controller: heartbeats and events go out on it.

One place that proves who is on the other end before anything is sent. The
receiver's certificate must chain to the control plane's authority - the TLS
context requires that - and must name the controller, which is checked here.
What an agent sends says which runners live on its worker and how their work
went; that is for the controller and nobody else.
"""
import http.client
import json
import ssl
from urllib.parse import urlsplit

from . import protocol, tls


class ControllerLink:
    def __init__(self, base_url, ssl_context, timeout=5):
        parts = urlsplit(base_url)
        if parts.scheme != "https":
            raise ValueError("the controller is reached over https only")
        self.host = parts.hostname
        self.port = parts.port or 443
        self.context = ssl_context
        self.timeout = timeout

    def post(self, path, payload):
        """Send one message. True when the controller took it; never raises."""
        conn = http.client.HTTPSConnection(self.host, self.port,
                                           context=self.context,
                                           timeout=self.timeout)
        try:
            conn.connect()
            if tls.common_name(conn.sock.getpeercert()) != \
                    tls.CONTROLLER_SUBJECT:
                return False
            conn.request("POST", path, body=json.dumps(payload),
                         headers={"Content-Type": "application/json",
                                  "X-Protocol-Version":
                                      str(protocol.PROTOCOL_MAJOR)})
            return conn.getresponse().status == 200
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return False
        finally:
            conn.close()


class EventSender:
    """Sends an agent operation's progress to the controller.

    Retried a few times, unlike a heartbeat: a lost completion event would
    leave the controller waiting. It is not the only way the controller
    learns, though - repeating the request with the same Idempotency-Key
    returns the state so far - so an event that never arrives costs time,
    never correctness.
    """

    def __init__(self, link, attempts=3, pauses=(0.5, 1.0), sleep=None):
        self.link = link
        self.attempts = attempts
        self.pauses = pauses
        if sleep is None:
            import time
            sleep = time.sleep
        self.sleep = sleep
        self.sent = 0
        self.lost = 0

    def __call__(self, event):
        for attempt in range(self.attempts):
            if self.link.post(protocol.EVENT_PATH, event):
                self.sent += 1
                return True
            if attempt < len(self.pauses):
                self.sleep(self.pauses[attempt])
        self.lost += 1
        return False
