"""Independent HTTPS peers and request-scoped, pushed lease results.

Only local owners choose endpoints. A peer bearer can send bounded intents;
a result bearer can update exactly one outgoing lease. Neither authenticates
a dashboard account or authorises runner operations. The existing controller
publishes on database events and exact delivery/credential deadlines.
"""
import hashlib
import hmac
import ipaddress
import json
import multiprocessing
import os
import re
import secrets
import ssl
import time
import threading
import uuid
from contextlib import nullcontext
from multiprocessing.connection import wait
from urllib.parse import urlsplit

from store import schema
from .federation import PROFILE, Refused, _integer
from .federation_broker import PinnedHTTPS, canonical, public_addresses, _uuid
from .federation_outbound import Outbound, REGISTRATION_ONCE
from .secrets import SecretStore
from . import retry

TABLES = """
CREATE TABLE IF NOT EXISTS federation_direct_targets (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, name TEXT NOT NULL, origin TEXT NOT NULL,
 sealed_credential BLOB, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_direct_outgoing (
 request_key TEXT PRIMARY KEY, actor TEXT NOT NULL, target_id TEXT NOT NULL,
 intent TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', receiver_request_id TEXT,
 sequence INTEGER NOT NULL DEFAULT 0, receipt_digest TEXT NOT NULL,
 expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_direct_receipts (
 request_id TEXT PRIMARY KEY REFERENCES federation_requests(id), request_key TEXT NOT NULL,
 origin TEXT NOT NULL, sealed_credential BLOB, credential_digest TEXT NOT NULL,
 expires_at INTEGER NOT NULL, last_state TEXT, sequence INTEGER NOT NULL DEFAULT 0,
 next_retry_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0
);
"""
TERMINAL = {"denied", "expired", "failed"}
STATES = TERMINAL | {"pending", "active", "retiring", "delivery-unknown"}
PENDING = {"approved": "pending", "activating": "pending"}
PEER_TOKEN = re.compile(r"nrf_[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}")
RESULT_TOKEN = re.compile(r"nrr_[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}")
EXCHANGE_TIMEOUT = 10
REAP_TIMEOUT = 1
MAX_EXCHANGE = 34816
MAX_REPLY = 32769
_exchange_lock = threading.Lock()
_unreaped_process = None


class TransportCleanupFailure(RuntimeError):
    """Stop convergence rather than overlap an owned transport still alive."""


def origin(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) and len(value) <= 255 else None
        if (not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.port not in (None, 443) or parsed.path not in ("", "/") or
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", parsed.hostname)):
            raise ValueError()
        import ipaddress
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return "https://" + parsed.hostname
    except (ValueError, TypeError):
        pass
    raise Refused("Choose an HTTPS hostname on port 443 without a path or credentials")


def _validate_exchange(endpoint, path, credential, body):
    endpoint = origin(endpoint)
    if path not in {"capacity", "requests", "results"}:
        raise Refused("Only the typed direct peer API may receive a message")
    pattern = RESULT_TOKEN if path == "results" else PEER_TOKEN
    if not isinstance(credential, str) or not pattern.fullmatch(credential):
        raise Refused("A credential for this exact direct transport is required")
    try:
        if body is not None and len(canonical(body)) > 32768:
            raise Refused("A direct message must be smaller than 32 KiB")
        payload = canonical({"endpoint": endpoint, "path": path, "credential": credential, "body": body})
        if len(payload) > MAX_EXCHANGE:
            raise Refused("The direct exchange exceeds its private IPC bound")
    except Refused:
        raise
    except (ValueError, TypeError, RecursionError):
        raise Refused("A bounded JSON message is required") from None
    return payload


def _http_exchange(endpoint, path, credential, body=None):
    """Blocking exchange, run only inside the deadline-owned worker."""
    host = urlsplit(endpoint).hostname
    private = private_peer(endpoint)
    connection = PinnedHTTPS(host, private["address"] if private else public_addresses(host)[0])
    if private:
        # Trust applies to this exact locally configured peer, not the broker
        # or other dashboard traffic. SNI/hostname verification stays enabled.
        connection._context = ssl.create_default_context(cafile=private["ca_file"])
    try:
        raw = None if body is None else canonical(body)
        if raw is not None and len(raw) > 32768:
            raise Refused("A direct message must be smaller than 32 KiB")
        connection.request("GET" if body is None else "POST", "/api/v2/federation/peer/" + path,
                           body=raw, headers={"Accept": "application/json", "Content-Type": "application/json",
                                              "Authorization": "Bearer " + credential})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise Refused("The peer did not acknowledge this direct message")
        raw = response.read(32769)
        if len(raw) > 32768:
            raise Refused("The peer response exceeds 32 KiB")
        return raw
    finally:
        connection.close()


def private_peer(endpoint):
    """Owner-pinned LAN destinations; incoming intents cannot expand trust."""
    raw = os.environ.get("FEDERATION_DIRECT_PRIVATE_PEERS", "{}")
    try:
        if len(raw) > 8192:
            raise ValueError()
        peers = json.loads(raw)
        if not isinstance(peers, dict) or len(peers) > 16:
            raise ValueError()
        networks = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        for target, value in peers.items():
            if origin(target) != target or not isinstance(value, dict) or set(value) != {"address", "ca_file"}:
                raise ValueError()
            address = ipaddress.ip_address(value["address"])
            if address.version != 4 or not any(address in network for network in networks):
                raise ValueError()
            if not isinstance(value["ca_file"], str) or not os.path.isabs(value["ca_file"]):
                raise ValueError()
        return peers.get(endpoint)
    except (ValueError, TypeError, Refused):
        raise Refused("Private peers require exact HTTPS origins, RFC1918 addresses and absolute CA paths") from None


def _exchange_worker(connection, exchange):
    # No credential appears in spawn arguments or diagnostic output. The
    # private bounded message is read only after the child's ready signal.
    import sys
    with open(os.devnull, "w") as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            connection.send_bytes(b"R")
            payload = json.loads(connection.recv_bytes(MAX_EXCHANGE))
            raw = exchange(**payload)
            if not isinstance(raw, bytes) or len(raw) > 32768:
                raise Refused("The direct response exceeds its IPC bound")
            connection.send_bytes(b"J" + raw)
        except BaseException:
            try:
                connection.send_bytes(b"E")
            except (OSError, EOFError):
                pass
        finally:
            connection.close()


def _private_pipe(context):
    # A complete request/reply must fit before the native process-exit wait;
    # this avoids blocking on a partial IPC message after its readiness signal.
    if os.name == "nt":
        # Do not use or change multiprocessing's global BUFSIZE: some Python
        # versions use an 8 KiB pipe. This exact owned pair has explicit buffers.
        import _winapi
        from multiprocessing.connection import PipeConnection
        server = client = parent = child = None
        try:
            address = r"\\.\pipe\nomercy-direct-" + secrets.token_hex(24)
            server = _winapi.CreateNamedPipe(address,
                _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
                _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
                1, 2 * MAX_EXCHANGE, 2 * MAX_EXCHANGE, _winapi.NMPWAIT_WAIT_FOREVER, _winapi.NULL)
            client = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
            _winapi.SetNamedPipeHandleState(client, _winapi.PIPE_READMODE_MESSAGE, None, None)
            connected = _winapi.ConnectNamedPipe(server, overlapped=True)
            _, error = connected.GetOverlappedResult(True)
            if error:
                raise Refused("The owned private transport did not connect")
            parent = PipeConnection(server)
            server = None
            child = PipeConnection(client)
            client = None
            return parent, child
        except BaseException:
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            if server is not None:
                _winapi.CloseHandle(server)
            if client is not None:
                _winapi.CloseHandle(client)
            raise
    parent, child = context.Pipe(duplex=True)
    try:
        import socket
        for connection in (parent, child):
            owned = socket.socket(fileno=connection.fileno())
            try:
                owned.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * MAX_EXCHANGE)
                if owned.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) < 2 * MAX_EXCHANGE:
                    raise Refused("The private transport buffer is unavailable")
            finally:
                owned.detach()
        return parent, child
    except BaseException:
        parent.close()
        child.close()
        raise


def _reap_exchange(process):
    global _unreaped_process
    if process.pid is not None:
        process.join(0)
        if process.exitcode is None:
            try:
                process.terminate()
            except OSError:
                pass
            process.join(REAP_TIMEOUT)
        if process.exitcode is None:
            try:
                process.kill()
            except OSError:
                pass
            process.join(REAP_TIMEOUT)
        if process.exitcode is None:
            # Retain its handle and fence every later exchange. The exception
            # bypasses ordinary delivery retries, so convergence fails closed.
            _unreaped_process = process
            raise TransportCleanupFailure("The owned direct transport could not be stopped")
    process.close()


def _bounded_exchange(payload, exchange, timeout):
    deadline = time.monotonic() + timeout
    if not _exchange_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise Refused("A direct exchange exceeded its total deadline")
    parent = child = process = None
    try:
        if _unreaped_process is not None:
            raise Refused("A prior owned direct transport has not been stopped")
        context = multiprocessing.get_context("spawn")
        parent, child = _private_pipe(context)
        process = context.Process(target=_exchange_worker, args=(child, exchange), daemon=True)
        process.start()
        child.close()
        child = None
        ready = wait([parent, process.sentinel], timeout=max(0, deadline - time.monotonic()))
        if parent not in ready or time.monotonic() >= deadline:
            raise Refused("A direct exchange exceeded its total deadline")
        if parent.recv_bytes(MAX_REPLY) != b"R":
            raise Refused("The private direct transport is unavailable")
        parent.send_bytes(payload)
        if not wait([process.sentinel], timeout=max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
            raise Refused("A direct exchange exceeded its total deadline")
        # The child has exited after sending one complete bounded response.
        # Reap its exact owned process before acknowledging progress or retry.
        _reap_exchange(process)
        process = None
        if not wait([parent], timeout=0):
            raise Refused("The direct exchange was not acknowledged")
        answer = parent.recv_bytes(MAX_REPLY)
        if not answer.startswith(b"J"):
            raise Refused("The direct exchange was not acknowledged")
        try:
            return json.loads(answer[1:])
        except (ValueError, UnicodeDecodeError):
            raise Refused("The peer did not return a JSON response") from None
    except (OSError, EOFError):
        raise Refused("The private direct exchange was not acknowledged") from None
    finally:
        try:
            if process is not None and process is not _unreaped_process:
                _reap_exchange(process)
        finally:
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            _exchange_lock.release()


def http_json(endpoint, path, credential, body=None):
    """One absolutely bounded DNS/TLS/HTTP exchange, never a background daemon."""
    return _bounded_exchange(_validate_exchange(endpoint, path, credential, body), _http_exchange, EXCHANGE_TIMEOUT)


class Direct:
    def __init__(self, path, service=None, clock=time.time, http=http_json):
        self.path, self.service, self.clock, self.http = path, service, clock, http
        with schema.connect(path) as c:
            c.executescript(TABLES)

    def _seal(self, credential):
        from .redact import remember
        remember(credential)
        return SecretStore(self.path)._fernet(create=True).encrypt(credential.encode())

    def add(self, actor, name, endpoint, credential, expires):
        endpoint = origin(endpoint)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise Refused("A peer name of 1 to 80 characters is required")
        if not isinstance(credential, str) or not PEER_TOKEN.fullmatch(credential) or not _uuid(credential[4:40]):
            raise Refused("Use only the peer's narrow direct request credential")
        _integer(expires, int(self.clock()) + 1, int(self.clock()) + 90 * 86400, "peer credential expiry")
        identity = str(uuid.uuid4())
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO federation_direct_targets VALUES(?,?,?,?,?,?,?)",
                      (identity, actor, name.strip(), endpoint, self._seal(credential), expires, int(self.clock())))
            c.execute("INSERT INTO federation_activity(at,actor,action,detail) VALUES(?,?,'direct-connected',?)",
                      (int(self.clock()), actor, identity))
        return identity

    def targets(self, actor):
        with schema.connect(self.path) as c:
            return [dict(row) for row in c.execute("SELECT id,name,origin,expires_at,"
                "(sealed_credential IS NOT NULL AND expires_at>?) AS enabled FROM federation_direct_targets WHERE actor=? ORDER BY name,id",
                (int(self.clock()), actor))]

    def disconnect(self, actor, identity):
        with schema.connect(self.path) as c:
            if not c.execute("UPDATE federation_direct_targets SET sealed_credential=NULL WHERE id=? AND actor=?", (identity, actor)).rowcount:
                raise Refused("This owner has no such direct connection")
            c.execute("INSERT INTO federation_activity(at,actor,action,detail) VALUES(?,?,'direct-disconnected',?)",
                      (int(self.clock()), actor, identity))

    def _target(self, actor, identity):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT * FROM federation_direct_targets WHERE id=? AND actor=?", (identity, actor)).fetchone()
        if not row or not row["sealed_credential"] or row["expires_at"] <= int(self.clock()):
            raise Refused("Choose this owner's unexpired direct peer connection")
        return row, SecretStore(self.path)._fernet().decrypt(row["sealed_credential"]).decode()

    def capacity(self, actor, identity):
        target, token = self._target(actor, identity)
        reply = self.http(target["origin"], "capacity", token)
        value = reply.get("capacity") if isinstance(reply, dict) else None
        supported = {"github-linux-x64", "github-windows-x64", "github-macos-x64"}
        if (not isinstance(value, dict) or value.get("profile") != PROFILE or
                not isinstance(value.get("available_profiles"), list) or len(value["available_profiles"]) > len(supported) or
                any(not isinstance(profile, str) or profile not in supported for profile in value["available_profiles"])):
            raise Refused("The peer did not return its bounded capacity contract")
        return value

    def submit(self, actor, identity, intent, group, callback_origin):
        callback_origin = origin(callback_origin)  # Deployment-owned; never taken from an HTTP request.
        target, token = self._target(actor, identity)
        capacity = self.capacity(actor, identity)  # One fresh preflight before a new intent, never a status loop.
        if not isinstance(intent, dict) or intent.get("profile") not in capacity["available_profiles"]:
            raise Refused("The peer has no safe lending support for this profile")
        credential = retry.call(REGISTRATION_ONCE, Outbound(self.path, self.service, self.clock).registration, intent, group)
        key = str(uuid.uuid4())
        receipt = "nrr_" + key + "." + secrets.token_urlsafe(32)
        from .redact import remember
        remember(receipt)
        expires = int(self.clock()) + 2 * 7200 * 60 + 3600
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT sealed_credential,expires_at FROM federation_direct_targets WHERE id=? AND actor=?",
                                (identity, actor)).fetchone()
            if not current or current["sealed_credential"] != target["sealed_credential"] or current["expires_at"] <= int(self.clock()):
                raise Refused("The direct connection changed before this new intent was recorded")
            c.execute("INSERT INTO federation_direct_outgoing(request_key,actor,target_id,intent,receipt_digest,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                      (key, actor, identity, json.dumps(intent), hashlib.sha256(receipt.encode()).hexdigest(), expires, int(self.clock())))
            c.execute("INSERT INTO federation_activity(at,actor,action,request_id,detail) VALUES(?,?,'direct-requested',?,'')",
                      (int(self.clock()), actor, key))
        try:
            reply = self.http(target["origin"], "requests", token, {"request_key": key, "intent": intent,
                "credential": credential, "receipt": {"origin": callback_origin, "credential": receipt, "expires_at": expires}})
            remote = reply.get("request") if isinstance(reply, dict) else None
            if not isinstance(remote, dict) or not _uuid(remote.get("id")) or not isinstance(remote.get("state"), str):
                raise Refused("The peer did not acknowledge this capacity intent")
            state = PENDING.get(remote["state"], remote["state"])
            if state not in STATES:
                raise Refused("The peer did not acknowledge a valid capacity state")
            with schema.connect(self.path) as c:
                if not c.execute("UPDATE federation_direct_outgoing SET receiver_request_id=?,state=CASE WHEN sequence=0 THEN ? ELSE state END "
                    "WHERE request_key=? AND (receiver_request_id IS NULL OR receiver_request_id=?)",
                    (remote["id"], state, key, remote["id"])).rowcount:
                    raise Refused("The acknowledgement identifies another receiver request")
        except Exception:
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_direct_outgoing SET state='delivery-unknown' WHERE request_key=? AND sequence=0", (key,))
            raise Refused("Delivery was not acknowledged; its local intent remains recorded for pushed results") from None
        return key

    def results(self, actor):
        with schema.connect(self.path) as c:
            rows = c.execute("SELECT request_key,target_id AS target_fleet_id,intent,state,receiver_request_id,sequence,created_at "
                             "FROM federation_direct_outgoing WHERE actor=? ORDER BY created_at DESC LIMIT 100", (actor,)).fetchall()
        return [dict(row, intent=json.loads(row["intent"]), transport="direct") for row in rows]

    def authenticate(self, credential):
        if not isinstance(credential, str) or not RESULT_TOKEN.fullmatch(credential):
            return None
        with schema.connect(self.path) as c:
            row = c.execute("SELECT request_key,actor,receipt_digest,expires_at FROM federation_direct_outgoing WHERE request_key=?",
                            (credential[4:40],)).fetchone()
        if not row or row["expires_at"] <= int(self.clock()) or not hmac.compare_digest(row["receipt_digest"], hashlib.sha256(credential.encode()).hexdigest()):
            return None
        return {"request_key": row["request_key"], "actor": row["actor"]}

    def receive(self, identity, body):
        if (not isinstance(body, dict) or set(body) != {"request_key", "receiver_request_id", "state", "sequence"} or
                body["request_key"] != identity["request_key"] or not _uuid(body["receiver_request_id"]) or
                not isinstance(body["state"], str) or body["state"] not in STATES):
            raise Refused("A result identifies only its own bounded lease state")
        _integer(body["sequence"], 1, 2147483647, "sequence")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM federation_direct_outgoing WHERE request_key=? AND actor=?",
                            (identity["request_key"], identity["actor"])).fetchone()
            if not row or row["expires_at"] <= int(self.clock()) or row["receiver_request_id"] not in (None, body["receiver_request_id"]):
                raise Refused("The result identifies another receiver lease or an expired receipt")
            if body["sequence"] <= row["sequence"]:
                return
            if row["state"] in TERMINAL and body["state"] != row["state"]:
                raise Refused("A terminal lease cannot be revived")
            c.execute("UPDATE federation_direct_outgoing SET receiver_request_id=?,state=?,sequence=? WHERE request_key=?",
                      (body["receiver_request_id"], body["state"], body["sequence"], row["request_key"]))
            c.execute("INSERT INTO federation_activity(at,actor,action,request_id,detail) VALUES(?,?,'direct-result',?,?)",
                      (int(self.clock()), row["actor"], row["request_key"], body["state"]))

    def validate_receipt(self, request_key, value):
        if (not isinstance(value, dict) or set(value) != {"origin", "credential", "expires_at"} or
                not isinstance(value["credential"], str) or not RESULT_TOKEN.fullmatch(value["credential"]) or
                not _uuid(request_key) or value["credential"][4:40] != request_key):
            raise Refused("A request-scoped result credential is required")
        endpoint = origin(value["origin"])
        _integer(value["expires_at"], int(self.clock()) + 1, int(self.clock()) + 2 * 7200 * 60 + 3600, "receipt expiry")
        return endpoint

    def receipt(self, request_row, value, connection=None):
        endpoint = self.validate_receipt(request_row["request_key"], value)
        digest = hashlib.sha256(value["credential"].encode()).hexdigest()
        with (schema.connect(self.path) if connection is None else nullcontext(connection)) as c:
            if connection is None:
                c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM federation_direct_receipts WHERE request_id=?", (request_row["id"],)).fetchone()
            if old:
                if old["origin"] != endpoint or old["expires_at"] != value["expires_at"] or not hmac.compare_digest(old["credential_digest"], digest):
                    raise Refused("An accepted result receipt cannot be retargeted")
                return
            c.execute("INSERT INTO federation_direct_receipts(request_id,request_key,origin,sealed_credential,credential_digest,expires_at) VALUES(?,?,?,?,?,?)",
                      (request_row["id"], request_row["request_key"], endpoint, self._seal(value["credential"]), digest, value["expires_at"]))

    def publish(self):
        now = int(self.clock())
        with schema.connect(self.path) as c:
            c.execute("UPDATE federation_direct_targets SET sealed_credential=NULL WHERE expires_at<=? AND sealed_credential IS NOT NULL", (now,))
            c.execute("UPDATE federation_direct_receipts SET sealed_credential=NULL,next_retry_at=NULL WHERE expires_at<=? AND sealed_credential IS NOT NULL", (now,))
            rows = c.execute("SELECT receipt.*,request.state FROM federation_direct_receipts receipt JOIN federation_requests request "
                "ON request.id=receipt.request_id WHERE receipt.sealed_credential IS NOT NULL AND (receipt.next_retry_at IS NULL OR receipt.next_retry_at<=?) "
                "AND (receipt.last_state IS NULL OR receipt.last_state!=CASE WHEN request.state IN ('approved','activating') THEN 'pending' ELSE request.state END) "
                "ORDER BY receipt.request_id LIMIT 1", (now,)).fetchall()
        # Give lease convergence another turn between network deliveries. Own
        # database writes plus next_deadline continue queued work without polling.
        for row in rows:
            state = PENDING.get(row["state"], row["state"])
            if row["last_state"] == state:
                continue
            sequence = row["sequence"] + 1
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_direct_receipts SET sequence=? WHERE request_id=?", (sequence, row["request_id"]))
            try:
                token = SecretStore(self.path)._fernet().decrypt(row["sealed_credential"]).decode()
                reply = self.http(row["origin"], "results", token, {"request_key": row["request_key"],
                    "receiver_request_id": row["request_id"], "state": state, "sequence": sequence})
                if not isinstance(reply, dict) or reply.get("received") is not True:
                    raise Refused("The peer did not acknowledge this result")
            except TransportCleanupFailure:
                # The controller's existing expiry-worker exception handler
                # marks its lifecycle failed and fences new incoming requests.
                raise
            except Exception:
                attempts = row["attempts"] + 1
                with schema.connect(self.path) as c:
                    c.execute("UPDATE federation_direct_receipts SET attempts=?,next_retry_at=? WHERE request_id=?",
                              (attempts, int(self.clock()) + min(300, 5 * 2 ** min(attempts - 1, 6)), row["request_id"]))
                continue
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_direct_receipts SET last_state=?,attempts=0,next_retry_at=NULL,"
                    "sealed_credential=CASE WHEN ? THEN NULL ELSE sealed_credential END WHERE request_id=?",
                    (state, int(state in TERMINAL), row["request_id"]))

    def next_deadline(self):
        now = int(self.clock())
        with schema.connect(self.path) as c:
            return c.execute("SELECT MIN(deadline) FROM (SELECT expires_at AS deadline FROM federation_direct_targets WHERE sealed_credential IS NOT NULL "
                "UNION ALL SELECT expires_at FROM federation_direct_receipts WHERE sealed_credential IS NOT NULL "
                "UNION ALL SELECT next_retry_at FROM federation_direct_receipts WHERE sealed_credential IS NOT NULL "
                "UNION ALL SELECT ? FROM federation_direct_receipts receipt JOIN federation_requests request ON request.id=receipt.request_id "
                "WHERE receipt.sealed_credential IS NOT NULL AND receipt.expires_at>? AND (receipt.next_retry_at IS NULL OR receipt.next_retry_at<=?) "
                "AND (receipt.last_state IS NULL OR receipt.last_state!=CASE WHEN request.state IN ('approved','activating') THEN 'pending' ELSE request.state END))",
                (now, now, now)).fetchone()[0]
