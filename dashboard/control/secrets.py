"""The forge tokens, in a store that can be set and never read back (T-1901).

Design 18.3, MEASURED when it was written: `.env` holds four live secrets in
plaintext, and the settings page can write it. Here the control plane keeps
the forge tokens instead. An operator can set one, see that it is set - when,
by whom, and a fingerprint to tell two apart - and nothing more: no route
returns a value, so a token cannot be recovered through the dashboard. Only
the controller itself reads them, to talk to the forges.

**Encrypted at rest**, with a key in its own file beside the database. That
protects the tokens in a copy of the database - a backup, a support bundle -
and not against someone who holds both files; the host's own protection of
the data volume is still what guards the pair.

**Building this changes nothing that runs.** `.env` is not read, written or
moved by this module. Moving the values out of `.env` is T-1901's run step,
which is NEVER-AUTO, and rotating them afterwards is a human decision.
"""
import hashlib
import os
import tempfile
from datetime import datetime, timezone

from store import schema

#: The secrets this store holds: the two forge tokens (design 18.3), and the
#: alarm webhook (GitHub #11) - a Discord or Slack webhook URL is all it
#: takes to post as it, so it is kept the way a token is.
NAMES = ("GH_TOKEN", "FORGEJO_API_TOKEN", "ALARM_WEBHOOK_URL")


class SecretRefused(ValueError):
    pass


def http_url(value):
    """Whether a webhook URL is one alarms may be sent to: http or https,
    with a host. Nothing else - not file:, not a bare host - is a place to
    POST an alarm."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(value):
    """Enough to tell two tokens apart, and nothing that helps recover one."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


class SecretStore:
    def __init__(self, path=None, key_path=None):
        self.path = path or schema.DB_PATH
        self.key_path = key_path or os.path.join(
            os.path.dirname(self.path) or ".", "secrets.key")

    def _fernet(self, create=False):
        from cryptography.fernet import Fernet
        if not os.path.exists(self.key_path):
            if not create:
                return None
            key = Fernet.generate_key()
            fd, pending = tempfile.mkstemp(prefix=".secrets-key-", dir=os.path.dirname(self.key_path) or ".")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    # Publish a complete key without replacing another writer's.
                    os.link(pending, self.key_path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(pending)
        with open(self.key_path, "rb") as fh:
            return Fernet(fh.read().strip())

    # ---- writing: the only thing an operator can do ------------------------

    def set(self, name, value, set_by):
        if name not in NAMES:
            raise SecretRefused(f"{name!r} is not a secret this store holds; "
                                f"it holds {list(NAMES)}")
        if not isinstance(value, str) or not value.strip():
            raise SecretRefused("a secret needs a value; to remove one, "
                                "clear it")
        value = value.strip()
        if any(c in value for c in "\r\n\x00") or len(value) > 512:
            raise SecretRefused("a token is one short line")
        if name == "ALARM_WEBHOOK_URL" and not http_url(value):
            raise SecretRefused("the alarm webhook must be an http:// or https:// URL")
        sealed = self._fernet(create=True).encrypt(value.encode())
        with schema.connect(self.path) as c:
            c.execute(
                "INSERT INTO secrets (name, sealed, fingerprint, set_at,"
                " set_by) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
                " sealed = excluded.sealed, fingerprint ="
                " excluded.fingerprint, set_at = excluded.set_at, set_by ="
                " excluded.set_by",
                (name, sealed, fingerprint(value), _now(), set_by))

    def clear(self, name):
        if name not in NAMES:
            raise SecretRefused(f"{name!r} is not a secret this store holds")
        with schema.connect(self.path) as c:
            c.execute("DELETE FROM secrets WHERE name = ?", (name,))

    # ---- reading what is set, never what it is -------------------------------

    def status(self, env=None):
        """Every secret this store holds, and whether it is set - without
        its value, which no caller of this method ever needs. `in_env` says
        whether the deployment's environment supplies one too: that is what
        is used while the store holds none."""
        with schema.connect(self.path) as c:
            rows = {r["name"]: dict(r) for r in c.execute(
                "SELECT name, fingerprint, set_at, set_by FROM secrets")}
        return [{"name": n, "set": n in rows,
                 "fingerprint": rows.get(n, {}).get("fingerprint"),
                 "set_at": rows.get(n, {}).get("set_at"),
                 "set_by": rows.get(n, {}).get("set_by"),
                 "in_env": bool(str((env or {}).get(n) or "").strip())} for n in NAMES]

    # ---- for the controller's own use ----------------------------------------

    def _values(self):
        """Decrypted, for the controller talking to the forges and for
        masking. Underscored, and reached by no route."""
        fernet = self._fernet()
        if fernet is None:
            return {}
        with schema.connect(self.path) as c:
            rows = c.execute("SELECT name, sealed FROM secrets").fetchall()
        out = {}
        for row in rows:
            try:
                out[row["name"]] = fernet.decrypt(row["sealed"]).decode()
            except Exception:       # noqa: BLE001 - a bad row is not a token
                continue
        return out

    def overlay(self, env):
        """`env` with every token this store holds taking the place of the
        one from the environment file - which is how the store supersedes
        `.env` without anything writing to `.env`."""
        merged = dict(env or {})
        merged.update(self._values())
        return merged
