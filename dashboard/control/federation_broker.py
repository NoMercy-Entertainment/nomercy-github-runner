"""Optional NoMercy courier assertions and event-driven receipt delivery.

Only a locally configured issuer and fleet audience are trusted. An assertion
names one actor and exact intent envelope; it never authenticates a dashboard
account, changes local peer grants, or authorises runner verbs.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
import urllib.parse
import uuid

from store import schema
from .federation import Refused
from .secrets import SecretStore

ISSUERS = ("https://api.nomercy.tv", "https://api-dev.nomercy.tv")
TABLES = """
CREATE TABLE IF NOT EXISTS federation_courier_replays (
 issuer TEXT NOT NULL, nonce TEXT NOT NULL, request_key TEXT NOT NULL,
 body_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
 PRIMARY KEY(issuer, nonce)
);
CREATE TABLE IF NOT EXISTS federation_receipts (
 request_id TEXT PRIMARY KEY REFERENCES federation_requests(id),
 issuer TEXT NOT NULL,
 broker_request_id TEXT NOT NULL, sealed_credential BLOB,
 expires_at INTEGER NOT NULL, last_state TEXT, sequence INTEGER NOT NULL DEFAULT 0,
 next_retry_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0
);
"""


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect only to the checked address, with the original TLS SNI/identity."""
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        connected = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(connected, server_hostname=self.host)
        except Exception:
            connected.close()
            raise


def public_addresses(hostname):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(
        hostname, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise Refused("The broker must resolve only to public addresses")
    return addresses


def http_json(url, body=None, headers=None):
    parsed = urllib.parse.urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in ISSUERS or not parsed.path.startswith("/v1/runner-fleets") or parsed.query or parsed.fragment:
        raise Refused("Only the locally selected NoMercy broker API may receive a message")
    connection = PinnedHTTPS(parsed.hostname, public_addresses(parsed.hostname)[0])
    try:
        connection.request("GET" if body is None else "POST", parsed.path,
            body=None if body is None else canonical(body),
            headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise Refused("The broker refused this message; sign in again if access has expired")
        raw = response.read(32769)
        if len(raw) > 32768:
            raise Refused("Broker response is too large")
        return json.loads(raw)
    finally:
        connection.close()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _decode(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", value):
        raise Refused("Courier assertion is invalid")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Broker:
    def __init__(self, path, clock=time.time, http=http_json):
        self.path, self.clock, self.http = path, clock, http
        with schema.connect(path) as c:
            c.executescript(TABLES)

    def settings(self):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='federation_broker'").fetchone()
        return json.loads(row[0]) if row else {"enabled": False}

    def configure(self, value):
        if not isinstance(value, dict) or set(value) != {"enabled", "issuer", "fleet_id"} or type(value["enabled"]) is not bool:
            raise Refused("Choose an enabled state, known broker and registered fleet identity")
        if value["issuer"] not in ISSUERS or not _uuid(value["fleet_id"]):
            raise Refused("A known broker and registered fleet UUID are required")
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_broker',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))

    def _jwks(self, issuer):
        now = int(self.clock())
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='federation_broker_jwks'").fetchone()
        cached = json.loads(row[0]) if row else {}
        if cached.get("issuer") == issuer and cached.get("expires_at", 0) > now:
            return cached.get("keys", [])
        document = self.http(issuer + "/v1/runner-fleets/.well-known/jwks.json")
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
            raise Refused("Broker signing keys are unavailable")
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_broker_jwks',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({"issuer": issuer,
                       "expires_at": now + 300, "keys": keys}),))
        return keys

    def begin_registration(self, issuer, subject):
        if issuer not in ISSUERS or not isinstance(subject, str) or not subject:
            raise Refused("A known broker and current signed-in owner are required")
        nonce = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(64))
        value = {"issuer": issuer, "subject": subject, "nonce_digest": hashlib.sha256(nonce.encode()).hexdigest(),
                 "expires_at": int(self.clock()) + 300, "fleet_id": None}
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_registration',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
        return nonce

    def _registration(self):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='federation_registration'").fetchone()
        value = json.loads(row[0]) if row else {}
        if value.get("expires_at", 0) <= int(self.clock()):
            raise Refused("There is no current owner-initiated broker registration")
        return value

    def verify(self, assertion, envelope, purpose="capacity"):
        settings = self.settings()
        pending = self._registration() if purpose == "registration" else None
        if pending:
            settings = {"issuer": pending["issuer"], "fleet_id": envelope.get("fleet_id")}
        elif not settings.get("enabled"):
            raise Refused("NoMercy courier is not enabled on this dashboard")
        if not isinstance(assertion, str) or not assertion.startswith("nmc_") or len(assertion) > 12288:
            raise Refused("A dedicated courier assertion is required")
        try:
            head, body, signature = assertion[4:].split(".")
            header, claims = json.loads(_decode(head)), json.loads(_decode(body))
            if not isinstance(header, dict) or set(header) != {"typ", "alg", "kid"} or header["typ"] != "JWT" or header["alg"] != "RS256":
                raise Refused("Unsupported courier assertion")
            keys = self._jwks(settings["issuer"])
            jwk = next((key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"] and
                        key.get("kty") == "RSA" and key.get("alg") == "RS256" and key.get("use") == "sig"), None)
            if not jwk:
                raise Refused("Broker signing key is not trusted")
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
            modulus = int.from_bytes(_decode(jwk["n"]), "big")
            exponent = int.from_bytes(_decode(jwk["e"]), "big")
            if not 2048 <= modulus.bit_length() <= 4096 or exponent != 65537:
                raise Refused("Unsupported broker signing key")
            rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(_decode(signature),
                (head + "." + body).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
            if not isinstance(claims, dict) or set(claims) != {"iss", "aud", "sub", "purpose", "request_id", "body_hash", "iat", "nbf", "exp", "jti"} or claims["purpose"] != purpose:
                raise Refused("Courier claims are invalid")
            now = int(self.clock())
            if any(type(claims[key]) is not int for key in ("iat", "nbf", "exp")) or not (
                    now - 135 <= claims["iat"] <= now + 15 and claims["nbf"] <= now + 15 and
                    claims["iat"] - 15 <= claims["nbf"] <= claims["iat"] and
                    now < claims["exp"] <= claims["iat"] + 120):
                raise Refused("Courier assertion expired or has an invalid lifetime")
            if claims["iss"] != settings["issuer"] or claims["aud"] != settings["fleet_id"]:
                raise Refused("Courier issuer or fleet audience is not trusted")
            if not _uuid(claims["jti"]) or not _uuid(claims["request_id"]) or not isinstance(claims["sub"], str):
                raise Refused("Courier identity is invalid")
            digest = hashlib.sha256(canonical(envelope)).hexdigest()
            if claims["body_hash"] != digest or claims["request_id"] != envelope.get("request_key"):
                raise Refused("Courier assertion names a different request envelope")
            if pending and (claims["sub"] != pending["subject"] or not _uuid(claims["aud"])):
                raise Refused("Registration identity does not match the initiating local owner")
        except Refused:
            raise
        except Exception:
            raise Refused("Courier assertion could not be verified") from None
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            peer = c.execute("SELECT * FROM federation_peers WHERE subject=? AND transport='broker' AND enabled=1",
                             (claims["sub"],)).fetchone() if purpose == "capacity" else None
            outgoing = None
            if purpose == "result" and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_outgoing'").fetchone():
                outgoing = c.execute("SELECT 1 FROM federation_outgoing WHERE request_key=? AND actor=?", (claims["request_id"], claims["sub"])).fetchone()
            if not peer and not pending and not outgoing:
                raise Refused("This person has no enabled local peer permission")
            old = c.execute("SELECT * FROM federation_courier_replays WHERE issuer=? AND nonce=?",
                            (claims["iss"], claims["jti"])).fetchone()
            if old and (old["body_hash"] != digest or old["request_key"] != claims["request_id"]):
                raise Refused("Courier nonce was already used for another envelope")
            c.execute("INSERT OR IGNORE INTO federation_courier_replays VALUES(?,?,?,?,?)",
                      (claims["iss"], claims["jti"], claims["request_id"], digest, claims["exp"]))
        return {**(dict(peer) if peer else {}), "verified_issuer": claims["iss"], "subject": claims["sub"]}

    def challenge(self, envelope, identity):
        if set(envelope) != {"request_key", "fleet_id", "ownership_nonce"} or not _uuid(envelope["fleet_id"]):
            raise Refused("A registration challenge contains only its fleet, request and ownership nonce")
        nonce = envelope["ownership_nonce"]
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", nonce):
            raise Refused("The registration nonce is invalid")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            pending = self._registration()
            if identity["subject"] != pending["subject"] or identity["verified_issuer"] != pending["issuer"] or not hmac.compare_digest(
                    hashlib.sha256(nonce.encode()).hexdigest(), pending["nonce_digest"]):
                raise Refused("The broker did not prove the owner-initiated registration nonce")
            if pending["fleet_id"] not in (None, envelope["fleet_id"]):
                raise Refused("This nonce already identifies another broker fleet")
            pending["fleet_id"] = envelope["fleet_id"]
            c.execute("UPDATE platform_settings SET value=? WHERE key='federation_registration'", (json.dumps(pending),))
            settings = {"enabled": True, "issuer": pending["issuer"], "fleet_id": envelope["fleet_id"]}
            c.execute("INSERT INTO platform_settings(key,value) VALUES('federation_broker',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(settings),))
            c.execute("INSERT INTO federation_activity(at,actor,action,detail) VALUES(?,?,'broker-verified',?)",
                      (int(self.clock()), identity["subject"], envelope["fleet_id"]))
        return hashlib.sha256((nonce + "|" + envelope["fleet_id"]).encode()).hexdigest()

    def receipt(self, request_row, receipt, issuer=None):
        if not isinstance(receipt, dict) or set(receipt) != {"request_id", "credential", "expires_at"} or not _uuid(receipt["request_id"]):
            raise Refused("A bounded broker receipt identity is required")
        credential = receipt["credential"]
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential):
            raise Refused("Receipt credential is invalid")
        from .redact import remember
        remember(credential)
        sealed = SecretStore(self.path)._fernet(create=True).encrypt(credential.encode())
        expires = receipt["expires_at"]
        if type(expires) is not int or not int(self.clock()) < expires <= int(self.clock()) + 2 * 7200 * 60 + 3600:
            raise Refused("Receipt expiry is invalid")
        with schema.connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO federation_receipts(request_id,issuer,broker_request_id,sealed_credential,expires_at) VALUES(?,?,?,?,?)",
                      (request_row["id"], issuer or self.settings()["issuer"], receipt["request_id"], sealed, expires))

    def publish(self):
        now = int(self.clock())
        with schema.connect(self.path) as c:
            c.execute("DELETE FROM federation_courier_replays WHERE expires_at<=?", (now,))
            c.execute("UPDATE federation_receipts SET sealed_credential=NULL,next_retry_at=NULL WHERE expires_at<=? AND sealed_credential IS NOT NULL", (now,))
        settings = self.settings()
        if not settings.get("enabled"):
            return
        with schema.connect(self.path) as c:
            rows = c.execute("SELECT receipt.*,request.state FROM federation_receipts receipt JOIN federation_requests request "
                             "ON request.id=receipt.request_id WHERE receipt.sealed_credential IS NOT NULL "
                             "AND (receipt.next_retry_at IS NULL OR receipt.next_retry_at<=?)", (now,)).fetchall()
        mapping = {"approved": "pending", "activating": "pending"}
        for row in rows:
            state = mapping.get(row["state"], row["state"])
            if row["last_state"] == state:
                continue
            sequence = row["sequence"] + 1
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_receipts SET sequence=? WHERE request_id=?", (sequence, row["request_id"]))
            try:
                credential = SecretStore(self.path)._fernet().decrypt(row["sealed_credential"]).decode()
                reply = self.http(row["issuer"] + "/v1/runner-fleets/receipts/" + row["broker_request_id"],
                          {"sequence": sequence, "state": state, "receiver_request_id": row["request_id"]},
                          {"X-Fleet-Receipt": credential})
                if not isinstance(reply, dict) or reply.get("received") is not True:
                    raise Refused("Broker did not acknowledge this receipt")
            except Exception:
                attempts = row["attempts"] + 1
                retry = now + min(300, 5 * 2 ** min(attempts - 1, 6))
                with schema.connect(self.path) as c:
                    c.execute("UPDATE federation_receipts SET attempts=?,next_retry_at=? WHERE request_id=?",
                              (attempts, retry, row["request_id"]))
                continue
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_receipts SET last_state=?,next_retry_at=NULL,attempts=0,"
                          "sealed_credential=CASE WHEN ? THEN NULL ELSE sealed_credential END WHERE request_id=?",
                          (state, int(state in ("denied", "expired", "failed")), row["request_id"]))
