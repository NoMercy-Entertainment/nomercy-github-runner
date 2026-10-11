"""Revocable API credentials, separate from people's browser sessions."""
import hashlib
import hmac
import re
import secrets
import time
import uuid

import users

SCOPES = ("fleet:read", "jobs:read", "logs:read", "runners:operate")
_RID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_READ_RUNNER = re.compile(r"^/api/v2/runners/" + _RID + r"(?:/(?:status|resources))?$")
_JOBS = re.compile(r"^/api/v2/runners/" + _RID + r"/history(?:/[0-9]+)?$")
_LOGS = re.compile(r"^/api/v2/runners/" + _RID + r"/logs$")
_OPERATE = re.compile(r"^/api/v2/runners/" + _RID +
                      r"/actions/(?:start|stop|restart|drain|cancel_drain|clear_cache)$")


def _public(identity, row):
    return {"id": identity, **{k: row[k] for k in
            ("name", "scopes", "created_at", "expires_at", "created_by")}}


def create(name, scopes, days, actor):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("A name of 1 to 80 characters is required.")
    if not isinstance(scopes, list) or not scopes or not all(
            isinstance(s, str) and s in SCOPES for s in scopes):
        raise ValueError("Choose at least one supported scope.")
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("Expiry must be between 1 and 365 days.")
    identity = str(uuid.uuid4())
    token = "nmr_" + identity + "." + secrets.token_urlsafe(32)
    row = {"name": name.strip(), "scopes": sorted(set(scopes)),
           "created_at": int(time.time()), "expires_at": int(time.time()) + days * 86400,
           "created_by": actor, "digest": hashlib.sha256(token.encode()).hexdigest()}
    with users._lock:
        data = users._load()
        data.setdefault("machines", {})[identity] = row
        users._save(data)
    return _public(identity, row), token


def listing():
    return [_public(identity, row) for identity, row in
            users._load().get("machines", {}).items()]


def revoke(identity):
    with users._lock:
        data = users._load()
        data.get("machines", {}).pop(identity, None)
        users._save(data)


def authenticate(token):
    if not isinstance(token, str) or not re.fullmatch(
            r"nmr_" + _RID + r"\.[A-Za-z0-9_-]{43}", token):
        return None
    identity = token[4:].split(".", 1)[0]
    try:
        row = users._load().get("machines", {}).get(identity)
        if not isinstance(row, dict) or type(row.get("expires_at")) is not int:
            return None
        if row["expires_at"] <= time.time():
            return None
        if not hmac.compare_digest(row.get("digest", ""), hashlib.sha256(token.encode()).hexdigest()):
            return None
        return _public(identity, row)
    except (users.Unreadable, KeyError, TypeError):
        return None


def permits(account, method, path):
    scopes = account["scopes"]
    if method in ("GET", "HEAD"):
        if "fleet:read" in scopes and (path in
                ("/api/v2/fleet", "/api/v2/fleets", "/api/v2/runners", "/api/version")
                or _READ_RUNNER.fullmatch(path)):
            return True
        if "jobs:read" in scopes and (path == "/api/v2/events/jobs" or _JOBS.fullmatch(path)):
            return True
        if "logs:read" in scopes and _LOGS.fullmatch(path):
            return True
    return bool(method == "POST" and "runners:operate" in scopes and _OPERATE.fullmatch(path))
