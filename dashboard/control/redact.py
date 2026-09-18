"""Removing secrets from text before it is kept anywhere.

Two ways a secret escapes, and one function for each.

**By name.** A mapping carries a field called `token` or `GH_TOKEN`. That is
caught by the list in `providers.REDACTED_FIELDS`, which is the single source
for which names are secret.

**By value.** An exception's message quotes the token it failed with, or a
command line that contained it. No field name marks it, so only knowing the
value finds it. The provisioning flow knows the token it minted and passes it
here before any message is written to a spec, an operation or a log.

Redacting by value is the one that matters for errors, and it is the one a
name-based list cannot do - which is why the registration token is scrubbed
here explicitly rather than trusted to the list.
"""
import providers

MASK = "[redacted]"

#: Values shorter than this are not treated as secrets to find in text. A
#: two-character "token" would match inside ordinary words and mangle every
#: message, and nothing issued by either forge is that short.
MIN_SECRET_LENGTH = 8


def redact(text, *secrets):
    """`text` with every occurrence of every secret replaced. The longest
    first, so a secret that contains a shorter one is masked whole rather
    than left half readable."""
    if text is None:
        return None
    out = str(text)
    for secret in sorted({str(s) for s in secrets if s}, key=len,
                         reverse=True):
        if secret and len(str(secret)) >= MIN_SECRET_LENGTH:
            out = out.replace(str(secret), MASK)
    return out


def redact_mapping(value):
    """A copy with every field named in REDACTED_FIELDS masked, at any depth.

    Names are compared case-insensitively: `GH_TOKEN` and `gh_token` are the
    same secret, and a list that matched only one spelling would miss the
    other - which is exactly how T-0003's `token` slipped past.
    """
    secret_names = {n.lower() for n in providers.REDACTED_FIELDS}
    if isinstance(value, dict):
        return {k: (MASK if str(k).lower() in secret_names and v
                    else redact_mapping(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_mapping(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# applied centrally (T-1801)
# ---------------------------------------------------------------------------
#
# The two functions above are the rules. What follows is where they are
# applied without anyone having to remember: every JSON response under /api/
# and every websocket frame goes through `redact_payload`, and the process's
# own stdout and stderr go through `RedactingStream`. A route written tomorrow
# that returns a token by accident still returns it masked.

#: Other deployment settings whose values are secret, beside the forge
#: tokens in providers.REDACTED_FIELDS: the dashboard's own sign-in secret.
EXTRA_SECRET_SETTINGS = frozenset({"OIDC_CLIENT_SECRET", "SECRET_KEY"})


def secret_values(env):
    """The values of every secret setting in `env` - the deployment's own
    tokens, which are what a leak would expose - long enough to look for."""
    names = {n.upper() for n in providers.REDACTED_FIELDS} | \
        EXTRA_SECRET_SETTINGS
    return sorted({str(v) for k, v in (env or {}).items()
                   if k.upper() in names and v
                   and len(str(v)) >= MIN_SECRET_LENGTH}, key=len,
                  reverse=True)


def redact_payload(value, secrets=()):
    """A payload with secret fields masked by name and secret values masked
    wherever they appear in a string - both rules, at every depth."""
    value = redact_mapping(value)

    def walk(v):
        if isinstance(v, str):
            return redact(v, *secrets)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, tuple):
            return tuple(walk(x) for x in v)
        return v
    return walk(value) if secrets else value


class RedactingStream:
    """A text stream that masks secret values in everything written to it.

    Wraps the process's stdout and stderr, so a log line - a print in a
    collector, a traceback that quotes a failed call's arguments - cannot
    carry a token out. `secrets` is called for the current values, so a token
    changed in Settings is covered without a restart."""

    def __init__(self, stream, secrets):
        self._stream = stream
        self._secrets = secrets

    def write(self, text):
        try:
            values = self._secrets() or ()
        except Exception:           # noqa: BLE001 - never lose a log line
            values = ()
        return self._stream.write(redact(text, *values) if values else text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_redaction(secrets):
    """Wrap stdout and stderr. Called once, when the dashboard starts."""
    import sys
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout, secrets)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr, secrets)


#: Secret values this process has seen - the deployment's tokens, and each
#: registration token the controller minted - so a value can be masked in a
#: record written by code that never held it. Bounded: the oldest are
#: forgotten, and a registration token is worthless after an hour anyway.
_KNOWN = []
KNOWN_LIMIT = 256


def remember(*values):
    for v in values:
        v = str(v or "")
        if len(v) >= MIN_SECRET_LENGTH and v not in _KNOWN:
            _KNOWN.append(v)
    del _KNOWN[:-KNOWN_LIMIT]


def known():
    return tuple(_KNOWN)
