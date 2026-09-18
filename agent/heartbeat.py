"""The agent's heartbeat: every ten seconds, what it is and what it runs.

Design 13.4. Each beat carries the agent's version and protocol, what its
runtime can do, which verbs this worker serves, what it observes of each of its
execution units, and a few counters. Three missed beats and the controller
marks the worker degraded - and a degraded worker is sent nothing destructive.

**A beat never claims more than the agent knows.** If the runtime cannot list
its units, the beat says so and lists none, rather than an empty list: an empty
list would read as "I run nothing", which the controller could take as every
unit having gone. A unit whose state the runtime cannot tell is reported as
`unknown`, which the controller keeps as unknown.

**It sends nothing until it has proved who it is talking to.** The receiver's
certificate must chain to the control plane's authority and name the
controller. The beat carries no secret, but it does tell whoever receives it
which runners live here, and that is not for anyone else.

A failed beat is not retried: the next one is ten seconds away (design 17.2).
"""
import http.client
import json
import ssl
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import protocol, tls

INTERVAL = 10
TIMEOUT = 5


def build(agent, server=None):
    """One heartbeat's payload."""
    beat = {
        "host_id": agent.host_id,
        "agent_version": agent.version,
        "protocol_major": protocol.PROTOCOL_MAJOR,
        "served": sorted(agent.permitted),
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        beat["capabilities"] = dict(agent.runtime.capabilities() or {})
    except Exception:                       # noqa: BLE001
        beat["capabilities_error"] = True
    try:
        beat["instances"] = [
            {"runner_id": u["runner_id"],
             "state": u.get("state") if u.get("state") in
             ("running", "stopped", "absent") else "unknown"}
            for u in agent.runtime.instances()]
    except Exception:                       # noqa: BLE001
        # Not an empty list: that would say "I run nothing".
        beat["instances_error"] = True
    beat["counters"] = {
        "refused_connections": len(server.refusals) if server else 0,
    }
    return beat


class HeartbeatSender:
    def __init__(self, agent, url, ssl_context, server=None,
                 interval=INTERVAL, timeout=TIMEOUT):
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError("heartbeats go over https only")
        self.agent = agent
        self.host = parts.hostname
        self.port = parts.port or 443
        self.path = parts.path or "/v1/heartbeat"
        self.context = ssl_context
        self.server = server
        self.interval = interval
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self._stop = threading.Event()
        self._thread = None

    def send_once(self):
        """Send one beat. True when the controller took it."""
        conn = http.client.HTTPSConnection(self.host, self.port,
                                           context=self.context,
                                           timeout=self.timeout)
        try:
            conn.connect()
            if tls.common_name(conn.sock.getpeercert()) != \
                    tls.CONTROLLER_SUBJECT:
                self.failed += 1
                return False
            conn.request("POST", self.path,
                         body=json.dumps(build(self.agent, self.server)),
                         headers={"Content-Type": "application/json",
                                  "X-Protocol-Version":
                                      str(protocol.PROTOCOL_MAJOR)})
            ok = conn.getresponse().status == 200
        except (OSError, ssl.SSLError, http.client.HTTPException):
            ok = False
        finally:
            conn.close()
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    def _loop(self):
        while not self._stop.is_set():
            self.send_once()
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop,
                                        name="agent-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)
