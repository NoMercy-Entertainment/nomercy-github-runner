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
import sys
import threading
import time

# admin may manage users; operator may act; viewer may only read.
ROLES = ("viewer", "operator", "admin")

PATH = os.path.join(os.environ.get("DASH_DATA", "/data"), "users.json")

_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Unreadable(Exception):
    """The list is there but cannot be read: nothing may be granted from it,
    and nothing may be written over it. Restore it by hand."""


def _load():
    """The store, always with both sections present.

    Only a missing file reads as empty, which is the bootstrap: the next
    sign-in becomes admin. A file that is there but cannot be opened, parsed
    or understood raises Unreadable instead. It used to read as empty too,
    and an empty list has no admin, so the next person to sign in was made
    admin and the real list was saved over (audit AUD-9312).
    """
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as error:
        raise Unreadable(f"{PATH}: {error}") from error
    if not isinstance(data, dict) or not all(
            isinstance(data.get(k, {}), dict) for k in ("users", "pending")):
        raise Unreadable(f"{PATH}: not an allowlist")
    data.setdefault("users", {})
    data.setdefault("pending", {})
    return data


def _refused(error):
    print(f"[users] access refused: {error}", file=sys.stderr, flush=True)


def _save(data):
    # Written to a sibling and renamed: a crash mid-write would otherwise
    # leave truncated JSON, which is Unreadable and locks everyone out,
    # the owner included, until it is restored by hand.
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, PATH)
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass


def _has_admin(data):
    return any(u.get("role") == "admin" for u in data["users"].values())


def sign_in(sub, preferred_username, display_name):
    """Role for this identity after a successful authentication, or None.

    None means authenticated but not allowed: the attempt is recorded as a
    pending request for an admin to decide on, and the caller must not treat
    it as a session.

    Bootstrap: while no admin exists, the first identity to sign in becomes
    one. That is a race by design, and it is accepted because the identity
    provider is a private realm - only its accounts can enter the race at
    all - and because the alternative, a seeded owner, was a setting nobody
    wanted to maintain. Once an admin exists this branch is closed until
    every admin has been revoked.

    A list that is there but unreadable answers None to everyone, and is
    left exactly as it was.
    """
    with _lock:
        try:
            data = _load()
        except Unreadable as error:
            _refused(error)
            return None

        known = data["users"].get(sub)
        if known:
            return known.get("role")

        if not _has_admin(data):
            data["users"][sub] = {
                "role": "admin",
                "name": display_name or preferred_username or sub,
                "username": preferred_username or "",
                "added": _now(),
            }
            data["pending"].pop(sub, None)
            _save(data)
            return "admin"

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
    try:
        return _load()["users"].get(sub, {}).get("role")
    except Unreadable as error:
        _refused(error)
        return None


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
