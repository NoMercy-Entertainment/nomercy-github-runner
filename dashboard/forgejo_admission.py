"""Validate the protected Forgejo owner-ID policy before provisioning a unit."""
import json
from urllib.parse import urlsplit

ENVIRONMENT = "NOMERCY_FORGEJO_ADMISSION_POLICY"
FIELDS = {"version", "instance", "allowed_owner_ids", "trusted_source_owner_ids"}


def _origin(value):
    if not isinstance(value, str) or not value or len(value) > 512 or value.strip() != value:
        raise ValueError("Forgejo admission instance must be an HTTPS origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ValueError("Forgejo admission instance must be an HTTPS origin")
    return parsed.hostname.lower()


def _fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Forgejo admission policy has duplicate fields")
        result[name] = value
    return result


def policy(raw, instance):
    """Canonical single-line snapshot; credentials and task data are never read."""
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 16384:
        raise ValueError("Forgejo admission policy is missing or oversized")
    try:
        document = json.loads(raw, object_pairs_hook=_fields)
        if not isinstance(document, dict) or set(document) != FIELDS:
            raise ValueError
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError
        if _origin(document["instance"]) != _origin(instance):
            raise ValueError
        for name in ("allowed_owner_ids", "trusted_source_owner_ids"):
            ids = document[name]
            if not isinstance(ids, list) or len(ids) > 100 or (name == "allowed_owner_ids" and not ids):
                raise ValueError
            if any(type(item) is not int or not 0 < item < 2**53 for item in ids):
                raise ValueError
            if len(set(ids)) != len(ids):
                raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Forgejo admission policy is invalid or names another instance") from None
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True)


def configured_policy(env):
    raw = (env or {}).get(ENVIRONMENT)
    if not raw and (env or {}).get("FORGEJO_RUNNER_SCOPE", "user") == "user":
        return ""
    return policy(raw, (env or {}).get("FORGEJO_INSTANCE_URL"))
