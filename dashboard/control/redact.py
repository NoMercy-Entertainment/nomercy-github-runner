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
    """`text` with every occurrence of every secret replaced."""
    if text is None:
        return None
    out = str(text)
    for secret in secrets:
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
