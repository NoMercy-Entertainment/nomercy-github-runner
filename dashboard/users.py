"""Who may use this dashboard, and as what.

Authentication proves who someone is. It grants nothing. This file is the only
thing that grants anything, and it is an explicit list of people.

That separation is the whole point. The rejected alternative was "anyone in
the directory" - for GitHub that was six org members plus an outside
collaborator, every one of whom could then have stopped, removed and recreated
runners. Keycloak has no groups or roles claim for this realm either, so there
is no directory-side rule to lean on even if we wanted one.

Keyed on the provider's `sub`, never on a username or an email. A username can
be changed, which frees the old one for someone else to register; a list keyed
on it would hand the new holder the old holder's access, silently and with no
event to notice.
"""

import json
import os
import threading
import time

# owner may manage users; operator may act; viewer may only read.
ROLES = ("viewer", "operator", "owner")

PATH = os.path.join(os.environ.get("DASH_DATA", "/data"), "users.json")

_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load():
    """The store, always with both sections present.

    A missing or unreadable file reads as empty rather than raising: an empty
    allowlist denies everyone, which is the safe direction to fail in. The
    seeded owner can still bind and rebuild the list from there.
    """
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:      # noqa: BLE001 - absence is not an error
        data = {}
    data.setdefault("users", {})
    data.setdefault("pending", {})
    return data


def _save(data):
    # Written to a sibling and renamed: a crash mid-write would otherwise
    # leave truncated JSON, which reads as an empty allowlist and locks
    # everyone out including the owner.
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, PATH)
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass


def _owner_hint():
    """(claim, value) from DASH_OWNER, or None if unset or malformed.

    Format: `oidc:preferred_username:phill`. A bootstrap only - a person does
    not know their own `sub`, so the first bind has to match on something they
    can type. Consulted only while no owner exists.
    """
    raw = (os.environ.get("DASH_OWNER") or "").strip()
    if not raw:
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3 or parts[0] != "oidc" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _has_owner(data):
    return any(u.get("role") == "owner" for u in data["users"].values())


def sign_in(sub, preferred_username, display_name):
    """Role for this identity after a successful authentication, or None.

    None means authenticated but not allowed: the attempt is recorded as a
    pending request for the owner to decide on, and the caller must not treat
    it as a session.
    """
    with _lock:
        data = _load()

        known = data["users"].get(sub)
        if known:
            return known.get("role")

        hint = _owner_hint()
        if hint and not _has_owner(data):
            claim, value = hint
            offered = {"preferred_username": preferred_username,
                       "sub": sub}.get(claim)
            if offered and offered == value:
                data["users"][sub] = {
                    "role": "owner",
                    "name": display_name or preferred_username or sub,
                    "username": preferred_username or "",
                    "added": _now(),
                }
                # Bound to the sub from here on. The hint is never read again,
                # so renaming the account cannot transfer ownership.
                data["pending"].pop(sub, None)
                _save(data)
                return "owner"

        if sub not in data["pending"]:
            data["pending"][sub] = {
                "name": display_name or preferred_username or sub,
                "username": preferred_username or "",
                "first_seen": _now(),
            }
            _save(data)
        return None


def role_of(sub):
    """The current role, read fresh on every call.

    Deliberately not cached: a revoke has to take effect on the next request,
    not when a fourteen-day session cookie happens to expire.
    """
    if not sub:
        return None
    return _load()["users"].get(sub, {}).get("role")


def approve(sub, role):
    """Grant a role, whether the person is pending or already listed."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    with _lock:
        data = _load()
        req = data["pending"].pop(sub, None) or {}
        cur = data["users"].get(sub, {})
        data["users"][sub] = {
            "role": role,
            "name": cur.get("name") or req.get("name") or sub,
            "username": cur.get("username") or req.get("username") or "",
            "added": cur.get("added") or _now(),
        }
        _save(data)


def deny(sub):
    """Drop a pending request without granting anything."""
    with _lock:
        data = _load()
        if data["pending"].pop(sub, None) is not None:
            _save(data)


def revoke(sub):
    """Remove someone from the allowlist. Takes effect on their next request."""
    with _lock:
        data = _load()
        if data["users"].pop(sub, None) is not None:
            _save(data)


def pending():
    data = _load()
    return [dict(sub=sub, **info)
            for sub, info in sorted(data["pending"].items(),
                                    key=lambda kv: kv[1].get("first_seen", ""))]


def list_users():
    data = _load()
    return [dict(sub=sub, **info)
            for sub, info in sorted(data["users"].items(),
                                    key=lambda kv: (kv[1].get("role", ""),
                                                    kv[1].get("name", "")))]
