"""Locally authorised capacity intents. Peer credentials never grant runner verbs.

The optional provisioner owns the platform-specific allocation boundary. Without
one, requests can be reviewed, but cannot reserve or create capacity. The normal
fleets and their runner groups are never mutated by this module.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from typing import Protocol

from store import schema
from .secrets import SecretStore

PROFILE = {"cpus": 8, "memory_bytes": 16 * 1024**3,
           "disk_bytes": 100 * 1024**3, "cache_bytes": 40 * 1024**3}
LIVE = ("approved", "activating", "active", "retiring")
TABLES = """
CREATE TABLE IF NOT EXISTS federation_peers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
 transport TEXT NOT NULL DEFAULT 'direct',
 enabled INTEGER NOT NULL DEFAULT 1, policy TEXT NOT NULL,
 credential_digest TEXT NOT NULL, credential_expires_at INTEGER NOT NULL,
 created_by TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_requests (
 id TEXT PRIMARY KEY, peer_id TEXT NOT NULL REFERENCES federation_peers(id),
 request_key TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER,
 decided_by TEXT, reason TEXT, runner_ids TEXT NOT NULL DEFAULT '[]',
 sealed_credential BLOB, credential_expires_at INTEGER,
 retry_at INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0,
 UNIQUE(peer_id, request_key)
);
CREATE TABLE IF NOT EXISTS federation_activity (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL,
 peer_id TEXT, request_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL,
 detail TEXT NOT NULL
);
"""


class Refused(ValueError):
    pass


class AllocationPending(Exception):
    """Owned specs exist but their authenticated online proof is not ready."""


class Provisioner(Protocol):
    def available(self, profile: dict) -> bool: ...
    def provision(self, request_id: str, profile: dict, organization: str,
                  count: int, lease_expires_at: int, credential: dict) -> list[str]: ...
    def retire(self, request_id: str, runner_ids: list[str]) -> bool: ...
    def running_job(self, runner_ids: list[str]) -> bool: ...


def _integer(value, lo, hi, name):
    if type(value) is not int or not lo <= value <= hi:
        raise Refused(f"{name} must be an integer between {lo} and {hi}")
    return value


def _names(value, name, pattern, maximum=50):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum or not all(
            isinstance(item, str) and re.fullmatch(pattern, item) for item in value):
        raise Refused(f"{name} must contain supported names")
    return sorted(set(value))


def policy(value):
    keys = {"max_runners", "max_lease_minutes", "profiles", "organizations",
            "automatic", "quiet_hours", "runner_groups"}
    if not isinstance(value, dict) or set(value) - keys:
        raise Refused("Only local limits, approved profiles and organisations may be set")
    automatic = value.get("automatic", False)
    if type(automatic) is not bool:
        raise Refused("automatic must be true or false")
    hours = value.get("quiet_hours", [])
    if not isinstance(hours, list) or len(hours) > 24 or any(
            type(hour) is not int or not 0 <= hour < 24 for hour in hours):
        raise Refused("quiet_hours must be UTC hours between 0 and 23")
    groups = value.get("runner_groups", [])
    if groups:
        groups = _names(groups, "runner_groups", r"[A-Za-z0-9][A-Za-z0-9_. -]{0,99}")
    elif groups != []:
        raise Refused("runner_groups must be a list of locally allowed restricted groups")
    if any(group.casefold() in {"fillz", "default", "stoney"} for group in groups):
        raise Refused("Ordinary runner groups cannot be used for lending")
    return {"max_runners": _integer(value.get("max_runners"), 0, 100, "max_runners"),
            "max_lease_minutes": _integer(value.get("max_lease_minutes"), 1, 7200,
                                          "max_lease_minutes"),
            "profiles": _names(value.get("profiles"), "profiles", r"github-(linux|windows|macos)-(x64|arm64)"),
            "organizations": _names(value.get("organizations"), "organizations", r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"),
            "automatic": automatic, "quiet_hours": sorted(set(hours)), "runner_groups": groups}


class Federation:
    def __init__(self, path, adapter=None, clock=time.time):
        self.path, self.adapter, self.clock = path, adapter, clock
        with schema.connect(path) as c:
            c.executescript(TABLES)
            peers = {row[1] for row in c.execute("PRAGMA table_info(federation_peers)")}
            if "transport" not in peers:
                c.execute("ALTER TABLE federation_peers ADD COLUMN transport TEXT NOT NULL DEFAULT 'direct'")
            have = {row[1] for row in c.execute("PRAGMA table_info(federation_requests)")}
            for column, kind in (("retry_at", "INTEGER"), ("attempt_count", "INTEGER NOT NULL DEFAULT 0")):
                if column not in have:
                    c.execute(f"ALTER TABLE federation_requests ADD COLUMN {column} {kind}")

    def _now(self):
        return int(self.clock())

    def _event(self, c, peer, rid, actor, action, detail=""):
        c.execute("INSERT INTO federation_activity(at,peer_id,request_id,actor,action,detail) "
                  "VALUES(?,?,?,?,?,?)", (self._now(), peer, rid, actor, action, detail))

    @staticmethod
    def _peer(row):
        return {key: row[key] for key in ("id", "name", "subject", "transport", "enabled",
                                         "created_by", "created_at")} | {
            "policy": json.loads(row["policy"])}

    @staticmethod
    def _request(row):
        return {key: row[key] for key in ("id", "peer_id", "request_key", "state",
                                         "created_at", "expires_at", "decided_by", "reason", "retry_at")} | {
            "intent": json.loads(row["intent"]), "runner_ids": json.loads(row["runner_ids"])}

    def peers(self):
        with schema.connect(self.path) as c:
            return [self._peer(row) for row in c.execute("SELECT * FROM federation_peers ORDER BY name,id")]

    def add_peer(self, name, subject, limits, actor, transport="direct"):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise Refused("A peer name of 1 to 80 characters is required")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 200 or any(
                ord(ch) < 32 for ch in subject):
            raise Refused("A stable peer identity is required")
        limits = policy(limits)
        if transport not in ("direct", "broker"):
            raise Refused("A supported peer transport is required")
        identity = str(uuid.uuid4())
        credential = f"nrf_{identity}.{secrets.token_urlsafe(32)}"
        with schema.connect(self.path) as c:
            if transport == "broker" and c.execute("SELECT 1 FROM federation_peers WHERE subject=? AND transport='broker' AND enabled=1", (subject,)).fetchone():
                raise Refused("This person already has an enabled local peer permission")
            c.execute("INSERT INTO federation_peers(id,name,subject,transport,enabled,policy,credential_digest,"
                      "credential_expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (identity, name.strip(), subject, transport, 1, json.dumps(limits),
                       hashlib.sha256(credential.encode()).hexdigest(), self._now() + 90 * 86400,
                       actor, self._now()))
            self._event(c, identity, None, actor, "peer-added")
        return identity, credential if transport == "direct" else None

    def authenticate(self, credential):
        if not isinstance(credential, str) or not re.fullmatch(
                r"nrf_[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}", credential):
            return None
        identity = credential[4:].split(".", 1)[0]
        with schema.connect(self.path) as c:
            row = c.execute("SELECT * FROM federation_peers WHERE id=?", (identity,)).fetchone()
        if not row or row["transport"] != "direct" or not row["enabled"] or row["credential_expires_at"] <= self._now():
            return None
        if not hmac.compare_digest(row["credential_digest"], hashlib.sha256(credential.encode()).hexdigest()):
            return None
        return self._peer(row)

    def set_policy(self, identity, limits, actor):
        limits = policy(limits)
        with schema.connect(self.path) as c:
            changed = c.execute("UPDATE federation_peers SET policy=? WHERE id=? AND enabled=1",
                                (json.dumps(limits), identity)).rowcount
            if not changed:
                raise Refused("Peer is not enabled")
            self._event(c, identity, None, actor, "limits-updated")

    def revoke(self, identity, actor):
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("UPDATE federation_peers SET enabled=0 WHERE id=?", (identity,)).rowcount:
                raise Refused("Peer does not exist")
            c.execute("UPDATE federation_requests SET state='denied',reason='Peer revoked',"
                      "sealed_credential=NULL,decided_by=? WHERE peer_id=? AND state='pending'",
                      (actor, identity))
            c.execute("UPDATE federation_requests SET state='retiring',reason='Peer revoked',"
                      "sealed_credential=NULL WHERE peer_id=? AND state IN ('approved','activating','active')",
                      (identity,))
            self._event(c, identity, None, actor, "peer-revoked")

    def _validate_intent(self, value, limits):
        if not isinstance(value, dict) or set(value) != {"profile", "organization", "count", "lease_minutes"}:
            raise Refused("An intent contains only profile, organization, count and lease_minutes")
        profile = value["profile"]
        if profile not in limits["profiles"]:
            raise Refused("Profile is not allowed by the local owner")
        if not isinstance(value["organization"], str) or value["organization"].casefold() not in {
                org.casefold() for org in limits["organizations"]}:
            raise Refused("Organisation is not allowed by the local owner")
        return {"profile": profile, "organization": value["organization"],
                "count": _integer(value["count"], 1, limits["max_runners"], "count"),
                "lease_minutes": _integer(value["lease_minutes"], 1, limits["max_lease_minutes"],
                                           "lease_minutes")}

    def _seal(self, value):
        if not isinstance(value, dict) or set(value) != {"registration_token", "expires_at", "runner_group"}:
            raise Refused("A short-lived registration token, expiry and restricted runner group are required")
        token = value["registration_token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Z0-9]{20,128}", token):
            raise Refused("Only a GitHub registration token is accepted; personal access tokens are refused")
        expires = _integer(value["expires_at"], self._now() + 1, self._now() + 3600, "credential expiry")
        group = value["runner_group"]
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,99}", group) or group.casefold() in {"fillz", "default", "stoney"}:
            raise Refused("Lent runners require a separate restricted runner group")
        from .redact import remember
        remember(token)
        return SecretStore(self.path)._fernet(create=True).encrypt(json.dumps(value).encode()), expires

    def submit(self, peer_id, request_key, intent, credential, receipt=None):
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", request_key):
            raise Refused("A request idempotency key of 16 to 100 characters is required")
        direct = None
        if receipt is not None:
            from .federation_direct import Direct
            direct = Direct(self.path, clock=self.clock)
            direct.validate_receipt(request_key, receipt)
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (peer_id,)).fetchone()
            if not peer:
                raise Refused("Peer is not enabled")
            limits = json.loads(peer["policy"])
            clean = self._validate_intent(intent, limits)
            old = c.execute("SELECT * FROM federation_requests WHERE peer_id=? AND request_key=?",
                            (peer_id, request_key)).fetchone()
            if old:
                if json.loads(old["intent"]) != clean:
                    raise Refused("Idempotency key was already used for a different intent")
                if direct:
                    direct.receipt(self._request(old), receipt, connection=c)
                return self._request(old)
            sealed, expires = self._seal(credential)
            identity = str(uuid.uuid4())
            c.execute("INSERT INTO federation_requests(id,peer_id,request_key,intent,state,created_at,"
                      "sealed_credential,credential_expires_at) VALUES(?,?,?,?,'pending',?,?,?)",
                      (identity, peer_id, request_key, json.dumps(clean), self._now(), sealed, expires))
            if direct:
                direct.receipt({"id": identity, "request_key": request_key}, receipt, connection=c)
            self._event(c, peer_id, identity, peer["subject"], "requested")
        if limits["automatic"]:
            try:
                return self.decide(identity, True, "local-policy")
            except Refused:
                # An unavailable provisioner or quota keeps the request reviewable.
                pass
        return self.get(identity)

    def get(self, identity):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT * FROM federation_requests WHERE id=?", (identity,)).fetchone()
        if not row:
            raise Refused("Request does not exist")
        return self._request(row)

    def requests(self, peer_id=None):
        with schema.connect(self.path) as c:
            rows = c.execute("SELECT * FROM federation_requests" + (" WHERE peer_id=?" if peer_id else "") +
                             " ORDER BY created_at DESC,id", (peer_id,) if peer_id else ()).fetchall()
        return [self._request(row) for row in rows]

    def decide(self, identity, approve, actor, reason=""):
        if type(approve) is not bool or not isinstance(reason, str) or len(reason) > 300:
            raise Refused("A decision and a reason of at most 300 characters are required")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM federation_requests WHERE id=?", (identity,)).fetchone()
            if not row or row["state"] != "pending":
                raise Refused("Only a pending request can be decided")
            if approve:
                peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (row["peer_id"],)).fetchone()
                if not peer:
                    raise Refused("Peer is not enabled")
                limits = json.loads(peer["policy"])
                intent = self._validate_intent(json.loads(row["intent"]), limits)
                if time.gmtime(self._now()).tm_hour in limits["quiet_hours"]:
                    raise Refused("The local owner's quiet hours are active")
                if row["credential_expires_at"] <= self._now():
                    raise Refused("Registration credential expired; requester must submit a new request")
                profile = {"fleet_id": intent["profile"], **PROFILE}
                if not self._available(profile):
                    raise Refused("Safe lent-runner provisioning is not available on this dashboard")
                occupied = sum(json.loads(other[0])["count"] for other in c.execute(
                    "SELECT intent FROM federation_requests WHERE peer_id=? AND state IN (?,?,?,?)",
                    (row["peer_id"], *LIVE)))
                if occupied + intent["count"] > limits["max_runners"]:
                    raise Refused("The local per-peer capacity limit has been reached")
                expires = self._now() + intent["lease_minutes"] * 60
            else:
                expires = None
            c.execute("UPDATE federation_requests SET state=?,decided_by=?,reason=?,expires_at=?,"
                      "sealed_credential=CASE WHEN ? THEN sealed_credential ELSE NULL END WHERE id=?",
                      ("approved" if approve else "denied", actor, reason, expires, int(approve), identity))
            self._event(c, row["peer_id"], identity, actor, "approved" if approve else "denied", reason)
        return self.get(identity)

    def activity(self, after=0):
        with schema.connect(self.path) as c:
            return [dict(row) for row in c.execute("SELECT * FROM federation_activity WHERE sequence>? ORDER BY sequence LIMIT 500", (after,))]

    def capacity(self, peer_id):
        peer = next((row for row in self.peers() if row["id"] == peer_id and row["enabled"]), None)
        if not peer:
            raise Refused("Peer is not enabled")
        used = sum(row["intent"]["count"] for row in self.requests(peer_id) if row["state"] in LIVE)
        supported = [profile for profile in peer["policy"]["profiles"] if
                     self._available({"fleet_id": profile, **PROFILE})]
        return {"peer_id": peer_id, "profile": PROFILE, "profiles": peer["policy"]["profiles"],
                "available_profiles": supported,
                "reserved": used, "headroom": max(0, peer["policy"]["max_runners"] - used),
                "max_lease_minutes": peer["policy"]["max_lease_minutes"],
                "provisioning_available": bool(supported)}

    def _available(self, profile):
        try:
            return self.adapter is not None and self.adapter.available(profile) is True
        except Exception:
            return False

    @contextmanager
    def _convergence_lock(self):
        """An OS lock fences threads and separate controller processes alike."""
        with open(self.path + ".federation.lock", "a+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                lock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                lock()
            except OSError as error:
                import errno
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                unlock()

    def _retry_retirement(self, row):
        attempts = row["attempt_count"] + 1
        retry = self._now() + min(300, 5 * 2 ** min(attempts - 1, 6))
        with schema.connect(self.path) as c:
            c.execute("UPDATE federation_requests SET retry_at=?,attempt_count=? WHERE id=? AND state='retiring'",
                      (retry, attempts, row["id"]))
            self._event(c, row["peer_id"], row["id"], "local-provisioner", "retirement-retry", "Retirement temporarily unavailable")

    def tick(self):
        """One event/expiry-triggered convergence. Adapter methods are idempotent.

        No network request runs inside a database transaction. A crash after a
        provision call repeats that request id, allowing the adapter to adopt
        only its existing allocations. Retiring reservations remain occupied
        until the adapter proves owned units absent.
        """
        with self._convergence_lock() as owned:
            if owned:
                self._tick()
                from .federation_broker import Broker
                Broker(self.path, self.clock).publish()
                from .federation_direct import Direct
                Direct(self.path, clock=self.clock).publish()

    def _tick(self):
        now = self._now()
        with schema.connect(self.path) as c:
            pending = c.execute("SELECT id,peer_id FROM federation_requests WHERE state='pending' "
                                "AND credential_expires_at<=?", (now,)).fetchall()
            c.execute("UPDATE federation_requests SET state='denied',reason='Registration credential expired',"
                      "sealed_credential=NULL WHERE state='pending' AND credential_expires_at<=?", (now,))
            for row in pending:
                self._event(c, row["peer_id"], row["id"], "local-lease", "credential-expired")
            expired = c.execute("SELECT id,peer_id FROM federation_requests WHERE state IN "
                                "('approved','activating','active') AND expires_at<=?", (now,)).fetchall()
            c.execute("UPDATE federation_requests SET state='retiring',reason='Lease expired' "
                      "WHERE state IN ('approved','activating','active') AND expires_at<=?", (now,))
            for row in expired:
                self._event(c, row["peer_id"], row["id"], "local-lease", "lease-expired")
            live = c.execute("SELECT request.*,peer.enabled,peer.policy FROM federation_requests request JOIN federation_peers peer "
                             "ON peer.id=request.peer_id WHERE request.state IN ('approved','activating','active')").fetchall()
            totals = {row[0]: row[1] for row in c.execute("SELECT peer_id,SUM(json_extract(intent,'$.count')) FROM federation_requests "
                       "WHERE state IN ('approved','activating','active','retiring') GROUP BY peer_id")}
            for row in live:
                limits = json.loads(row["policy"])
                try:
                    if not row["enabled"] or totals[row["peer_id"]] > limits["max_runners"] or time.gmtime(now).tm_hour in limits["quiet_hours"]:
                        raise Refused("Current owner policy requires retirement")
                    self._validate_intent(json.loads(row["intent"]), limits)
                    allocation_table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone()
                    groups = c.execute("SELECT DISTINCT runner_group FROM federation_allocations WHERE request_id=?", (row["id"],)).fetchall() if allocation_table else []
                    if any(group[0] not in limits.get("runner_groups", []) for group in groups):
                        raise Refused("The receiving owner removed this lent group permission")
                except Refused:
                    c.execute("UPDATE federation_requests SET state='retiring',reason='Current owner policy requires retirement',sealed_credential=NULL "
                              "WHERE id=?", (row["id"],))
                    self._event(c, row["peer_id"], row["id"], "local-policy", "policy-retirement")
            c.execute("UPDATE federation_requests SET sealed_credential=NULL WHERE credential_expires_at<=? AND sealed_credential IS NOT NULL", (now,))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone():
                c.execute("UPDATE federation_allocations SET sealed_registration=NULL WHERE credential_expires_at<=? AND sealed_registration IS NOT NULL", (now,))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_identities'").fetchone():
                c.execute("UPDATE federation_identities SET sealed_access=NULL WHERE expires_at<=? AND sealed_access IS NOT NULL", (now,))
            rows = c.execute("SELECT * FROM federation_requests WHERE state IN ('approved','activating','retiring') "
                             "AND (retry_at IS NULL OR retry_at<=?)", (now,)).fetchall()
        if not self.adapter:
            return
        for row in rows:
            identity, intent = row["id"], json.loads(row["intent"])
            if row["state"] == "retiring":
                try:
                    absent = self.adapter.retire(identity, json.loads(row["runner_ids"]))
                except Exception:
                    self._retry_retirement(row)
                    continue
                if absent is not True:
                    with schema.connect(self.path) as c:
                        c.execute("UPDATE federation_requests SET retry_at=NULL WHERE id=?", (identity,))
                    continue
                with schema.connect(self.path) as c:
                    c.execute("UPDATE federation_requests SET state='expired',sealed_credential=NULL,retry_at=NULL WHERE id=? AND state='retiring'", (identity,))
                    self._event(c, row["peer_id"], identity, "local-lease", "retired", row["reason"] or "")
                continue
            with schema.connect(self.path) as c:
                c.execute("BEGIN IMMEDIATE")
                current = c.execute("SELECT state FROM federation_requests WHERE id=?", (identity,)).fetchone()
                if not current or current["state"] not in ("approved", "activating"):
                    continue
                peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (row["peer_id"],)).fetchone()
                try:
                    if not peer:
                        raise Refused("Peer revoked")
                    limits = json.loads(peer["policy"])
                    self._validate_intent(intent, limits)
                    occupied = sum(json.loads(other[0])["count"] for other in c.execute(
                        "SELECT intent FROM federation_requests WHERE peer_id=? AND state IN (?,?,?,?)",
                        (row["peer_id"], *LIVE)))
                    if occupied > limits["max_runners"]:
                        raise Refused("Owner reduced concurrent capacity")
                    if time.gmtime(now).tm_hour in limits["quiet_hours"]:
                        raise Refused("Quiet hours started before allocation")
                except Refused:
                    c.execute("UPDATE federation_requests SET state='retiring',reason='Local policy changed',sealed_credential=NULL WHERE id=?", (identity,))
                    self._event(c, row["peer_id"], identity, "local-policy", "activation-refused", "Local policy changed")
                    continue
                c.execute("UPDATE federation_requests SET state='activating' WHERE id=? AND state='approved'", (identity,))
            if not row["sealed_credential"] or row["credential_expires_at"] <= now:
                with schema.connect(self.path) as c:
                    c.execute("UPDATE federation_requests SET state='retiring',reason='Registration credential expired',sealed_credential=NULL WHERE id=?", (identity,))
                    self._event(c, row["peer_id"], identity, "local-lease", "activation-refused", "Registration credential expired")
                continue
            try:
                credential = json.loads(SecretStore(self.path)._fernet().decrypt(row["sealed_credential"]))
                runners = self.adapter.provision(identity, {"fleet_id": intent["profile"], **PROFILE},
                                                 intent["organization"], intent["count"], row["expires_at"], credential)
            except AllocationPending:
                continue
            except Exception:
                # The adapter may have allocated before the transport failed.
                # Its retirement must discover ownership by request_id too.
                # Exception text can contain a credential: keep it out of audit.
                with schema.connect(self.path) as c:
                    c.execute("UPDATE federation_requests SET state='retiring',reason='Allocation failed; retiring owned capacity',sealed_credential=NULL WHERE id=?", (identity,))
                    self._event(c, row["peer_id"], identity, "local-provisioner", "allocation-failed")
                continue
            with schema.connect(self.path) as c:
                # Revocation during provisioning remains retiring; ownership is
                # recorded so it can be drained, never revived by the response.
                c.execute("UPDATE federation_requests SET runner_ids=?,sealed_credential=NULL,"
                          "state=CASE WHEN state='activating' AND expires_at>? THEN 'active' ELSE 'retiring' END,"
                          "reason=CASE WHEN expires_at<=? THEN 'Lease expired during allocation' ELSE reason END WHERE id=?",
                          (json.dumps(runners), self._now(), self._now(), identity))
                self._event(c, row["peer_id"], identity, "local-provisioner", "allocated")

    def extend(self, identity, peer_id, minutes):
        row = self.get(identity)
        if row["peer_id"] != peer_id or row["state"] != "active" or row["expires_at"] <= self._now():
            raise Refused("Only this peer's active unexpired lease may be extended")
        peer = next((p for p in self.peers() if p["id"] == peer_id and p["enabled"]), None)
        try:
            busy = self.adapter is not None and self.adapter.running_job(row["runner_ids"]) is True
        except Exception:
            busy = False
        if not peer or not busy:
            raise Refused("An extension requires a proven running job and enabled peer")
        minutes = _integer(minutes, 1, peer["policy"]["max_lease_minutes"], "extension")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            current_peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (peer_id,)).fetchone()
            if not current_peer:
                raise Refused("Peer changed before the extension")
            current_limits = json.loads(current_peer["policy"])
            self._validate_intent({**row["intent"], "lease_minutes": minutes}, current_limits)
            # A concurrent revocation or expiry cannot be undone by an extension.
            changed = c.execute("UPDATE federation_requests SET expires_at=? WHERE id=? AND peer_id=? "
                                "AND state='active' AND expires_at>? AND EXISTS(SELECT 1 FROM federation_peers WHERE id=? AND enabled=1)",
                                (self._now() + minutes * 60, identity, peer_id, self._now(), peer_id)).rowcount
            if not changed:
                raise Refused("Lease changed before the extension")
            self._event(c, peer_id, identity, peer["subject"], "extended")
        return self.get(identity)

    def next_deadline(self):
        from .federation_direct import Direct
        direct_deadline = Direct(self.path, clock=self.clock).next_deadline()
        with schema.connect(self.path) as c:
            row = c.execute("SELECT MIN(deadline) FROM (SELECT expires_at AS deadline FROM federation_requests "
                            "WHERE state IN ('approved','activating','active') UNION ALL "
                            "SELECT credential_expires_at FROM federation_requests WHERE sealed_credential IS NOT NULL UNION ALL "
                            "SELECT retry_at FROM federation_requests WHERE state='retiring' AND retry_at IS NOT NULL)").fetchone()
            receipt_table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_receipts'").fetchone()
            receipt = c.execute("SELECT MIN(deadline) FROM (SELECT next_retry_at AS deadline FROM federation_receipts WHERE sealed_credential IS NOT NULL "
                                "UNION ALL SELECT expires_at FROM federation_receipts WHERE sealed_credential IS NOT NULL)").fetchone() if receipt_table else None
            allocation_table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone()
            allocation = c.execute("SELECT MIN(credential_expires_at) FROM federation_allocations WHERE sealed_registration IS NOT NULL").fetchone() if allocation_table else None
            identity_table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_identities'").fetchone()
            identity = c.execute("SELECT MIN(expires_at) FROM federation_identities WHERE sealed_access IS NOT NULL").fetchone() if identity_table else None
            quiet = [json.loads(value[0])["quiet_hours"] for value in c.execute("SELECT DISTINCT peer.policy FROM federation_peers peer "
                     "JOIN federation_requests request ON request.peer_id=peer.id WHERE request.state IN ('approved','activating','active')")]
        deadlines = [value for value in (row[0] if row else None, receipt[0] if receipt else None, allocation[0] if allocation else None, identity[0] if identity else None, direct_deadline) if value is not None]
        now = self._now()
        for hours in quiet:
            if time.gmtime(now).tm_hour in hours:
                deadlines.append(now)
            else:
                boundary = (now // 3600 + 1) * 3600
                starts = [boundary + offset * 3600 for offset in range(24) if time.gmtime(boundary + offset * 3600).tm_hour in hours]
                if starts:
                    deadlines.append(min(starts))
        return min(deadlines) if deadlines else None


def run_events(service, stop, ready=None):
    """Use native database events plus the next exact lease/credential deadline.

    Run once in the controller, with its process lifecycle. No peer or forge is
    polled. An adapter awaiting a drained job is retried when observed state
    arrives in the existing control database, rather than on a periodic timer.
    """
    from dbwatch import DatabaseChanges
    watch = DatabaseChanges(service.path)
    try:
        if ready:
            ready()
        changed, deadline = True, None
        while not stop.is_set():
            if changed or (deadline is not None and deadline <= service.clock()):
                service.tick()
                deadline = service.next_deadline()
            # Stop responsiveness and SSE keepalives are not transport polling.
            timeout = 30 if deadline is None else min(30, max(0, deadline - service.clock()))
            changed = watch.wait(timeout)
    finally:
        watch.close()
