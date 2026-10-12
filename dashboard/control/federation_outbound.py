"""Owner-driven NoMercy registration, bounded requests and pushed results.

An account bearer is used only with the fixed NoMercy API. It is encrypted
server-side after an explicit sharing sign-in, expires locally, and is never
delegated to a peer callback or browser. GitHub PATs stay on their owner.
"""
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import providers
from store import schema
from .federation import Refused, _integer
from .federation_broker import Broker, ISSUERS, http_json
from .secrets import SecretStore

TABLES = """
CREATE TABLE IF NOT EXISTS federation_identities (
 subject TEXT PRIMARY KEY, sealed_access BLOB, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_outgoing (
 request_key TEXT PRIMARY KEY, actor TEXT NOT NULL, target_fleet_id TEXT NOT NULL,
 intent TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', broker_request_id TEXT,
 receiver_request_id TEXT, sequence INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
);
"""


class Outbound:
    def __init__(self, path, service=None, clock=time.time, http=http_json):
        self.path, self.service, self.clock, self.http = path, service, clock, http
        with schema.connect(path) as c:
            c.executescript(TABLES)

    def retain_identity(self, claims, token):
        expires = token.get("expires_in")
        access = token.get("access_token")
        if type(expires) is not int or not 1 <= expires <= 3600 or not isinstance(access, str) or not 1 <= len(access) <= 16384:
            raise Refused("An expiring account token is required for the optional NoMercy connection")
        from .redact import remember
        remember(access)
        sealed = SecretStore(self.path)._fernet(create=True).encrypt(access.encode())
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO federation_identities(subject,sealed_access,expires_at) VALUES(?,?,?) "
                      "ON CONFLICT(subject) DO UPDATE SET sealed_access=excluded.sealed_access,expires_at=excluded.expires_at",
                      (claims["sub"], sealed, int(self.clock()) + expires))

    def _access(self, subject):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT * FROM federation_identities WHERE subject=?", (subject,)).fetchone()
        if not row or not row["sealed_access"] or row["expires_at"] <= int(self.clock()):
            raise Refused("Sign in for capacity sharing again; the NoMercy API access has expired")
        return SecretStore(self.path)._fernet().decrypt(row["sealed_access"]).decode()

    def call(self, subject, path, body=None, issuer=None):
        issuer = issuer or Broker(self.path, self.clock).settings().get("issuer")
        if issuer not in ISSUERS or not re.fullmatch(r"(?:/register|/retry-results|/[0-9a-f-]{36}/(?:grants|requests)|)", path):
            raise Refused("Only a known broker's typed fleet API can receive an owner message")
        return self.http(issuer + "/v1/runner-fleets" + path, body,
                         {"Authorization": "Bearer " + self._access(subject)})

    def register(self, subject, issuer, name, public_origin):
        parsed = urlsplit(public_origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.port not in (None, 443):
            raise Refused("The dashboard needs its configured public HTTPS origin; request headers cannot choose it")
        if issuer not in ISSUERS or not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise Refused("Choose a known broker and fleet name")
        self._access(subject)  # Do not replace a current nonce when sign-in is missing.
        broker = Broker(self.path, self.clock, self.http)
        nonce = broker.begin_registration(issuer, subject)
        supported = {"github-linux-x64", "github-windows-x64", "github-windows-arm64", "github-macos-x64"}
        profiles = [row["fleet_id"] for row in self.service.fleets.list() if row["provider"] == "github" and row["available"] and row["fleet_id"] in supported]
        reply = self.call(subject, "/register", {"name": name, "callback_origin": public_origin.rstrip("/"),
            "ownership_nonce": nonce, "capabilities": {"protocol": 1, "profiles": profiles}}, issuer)
        fleet = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(fleet, dict) or not isinstance(fleet.get("id"), str):
            raise Refused("The broker did not accept this fleet registration")
        try:
            if str(uuid.UUID(fleet["id"])) != fleet["id"]:
                raise ValueError()
        except ValueError:
            raise Refused("The broker returned an invalid fleet identity") from None
        return {"fleet_id": fleet["id"], "verified": fleet.get("verified_at") is not None,
                "message": "The broker will verify this dashboard's TLS callback before requests are allowed"}

    def directory(self, subject):
        settings = Broker(self.path, self.clock).settings()
        if not settings.get("enabled"):
            raise Refused("The broker has not proved this dashboard's registration yet")
        reply = self.call(subject, "")
        fleets = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(fleets, list) or len(fleets) > 100:
            raise Refused("The broker did not return a bounded fleet directory")
        return [row for row in fleets if isinstance(row, dict) and row.get("id") != settings["fleet_id"] and row.get("verified") is True]

    def grant(self, subject, person, enabled):
        settings = Broker(self.path, self.clock).settings()
        if not settings.get("enabled"):
            raise Refused("The broker registration is still pending")
        return self.call(subject, f"/{settings['fleet_id']}/grants", {"user_id": person, "enabled": enabled})

    def submit(self, subject, target_id, intent, group):
        if not self.service:
            raise Refused("The local requester controller is unavailable")
        settings = Broker(self.path, self.clock).settings()
        known = next((row for row in self.directory(subject) if row.get("id") == target_id), None)
        if not known or set(intent) != {"profile", "organization", "count", "lease_minutes"}:
            raise Refused("Choose capacity from the permitted fleet directory")
        if intent["profile"] not in (known.get("capabilities") or {}).get("profiles", []):
            raise Refused("The peer does not advertise this profile")
        _integer(intent["count"], 1, 100, "count")
        _integer(intent["lease_minutes"], 1, 7200, "lease_minutes")
        client = providers.by_key("github").forge_client(self.service.env)
        if not client or intent["organization"] != client.org or not isinstance(group, str) or group.casefold() in {"fillz", "default", "stoney"}:
            raise Refused("Request only the local GitHub organisation using a separate restricted group")
        observed = next((row for row in client.runner_groups() or [] if row.get("name") == group), None)
        if not observed or observed.get("visibility") != "selected" or observed.get("allows_public_repositories") is not False or observed.get("inherited") is not False or observed.get("default") is not False:
            raise Refused("Lent registrations require a private selected group")
        repos = client._get(f"/orgs/{client.org}/actions/runner-groups/{observed['id']}/repositories", params={"per_page": 1})
        if not isinstance(repos, dict) or type(repos.get("total_count")) is not int or repos["total_count"] < 1:
            raise Refused("Select the intended private repositories for the lent group")
        token = client.registration_token()
        if not isinstance(token, str) or not re.fullmatch(r"[A-Z0-9]{20,128}", token):
            raise Refused("GitHub did not mint a short registration credential")
        from .redact import remember
        remember(token)
        key = str(uuid.uuid4())
        with schema.connect(self.path) as c:
            c.execute("INSERT INTO federation_outgoing(request_key,actor,target_fleet_id,intent,created_at) VALUES(?,?,?,?,?)",
                      (key, subject, target_id, json.dumps(intent), int(self.clock())))
        try:
            reply = self.call(subject, f"/{target_id}/requests", {"source_fleet_id": settings["fleet_id"], "request_key": key,
                "intent": intent, "credential": {"registration_token": token, "expires_at": int(self.clock()) + 1800, "runner_group": group}})
            remote = reply.get("request") if isinstance(reply, dict) else None
            if not isinstance(remote, dict) or not isinstance(remote.get("id"), str):
                raise Refused("The broker did not acknowledge this bounded capacity intent")
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_outgoing SET broker_request_id=? WHERE request_key=?", (remote["id"], key))
        except Exception:
            with schema.connect(self.path) as c:
                c.execute("UPDATE federation_outgoing SET state='delivery-unknown' WHERE request_key=? AND sequence=0", (key,))
            raise Refused("Delivery was not acknowledged; its local request remains recorded for a signed result") from None
        return key

    def results(self, subject):
        with schema.connect(self.path) as c:
            rows = c.execute("SELECT * FROM federation_outgoing WHERE actor=? ORDER BY created_at DESC LIMIT 100", (subject,)).fetchall()
        return [dict(row, intent=json.loads(row["intent"])) for row in rows]

    def receive(self, identity, body):
        if set(body) != {"request_key", "broker_request_id", "state", "receiver_request_id", "sequence"}:
            raise Refused("A pushed result contains only its request identity, state and sequence")
        _integer(body["sequence"], 1, 2147483647, "sequence")
        if body["state"] not in {"pending", "active", "denied", "retiring", "expired", "failed", "delivery-unknown"}:
            raise Refused("The pushed result state is invalid")
        try:
            if str(uuid.UUID(body["broker_request_id"])) != body["broker_request_id"] or (body["receiver_request_id"] is not None and
                    str(uuid.UUID(body["receiver_request_id"])) != body["receiver_request_id"]):
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise Refused("Pushed request identifiers must be canonical UUIDs") from None
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM federation_outgoing WHERE request_key=? AND actor=?", (body["request_key"], identity["subject"])).fetchone()
            if not row or row["broker_request_id"] not in (None, body["broker_request_id"]) or row["receiver_request_id"] not in (None, body["receiver_request_id"]):
                raise Refused("The result does not belong to this owner's submitted intent")
            if body["sequence"] <= row["sequence"]:
                return
            if row["state"] in {"denied", "expired", "failed"} and body["state"] != row["state"]:
                raise Refused("A terminal lease cannot be revived by a pushed result")
            c.execute("UPDATE federation_outgoing SET broker_request_id=?,receiver_request_id=?,state=?,sequence=? WHERE request_key=?",
                      (body["broker_request_id"], body["receiver_request_id"], body["state"], body["sequence"], row["request_key"]))
            c.execute("INSERT INTO federation_activity(at,actor,action,request_id,detail) VALUES(?,?,'peer-result',?,?)",
                      (int(self.clock()), identity["subject"], row["request_key"], body["state"]))
