"""Read-only introspection of a single runner.

Separate from docker_ops, which owns fleet lifecycle and fleet telemetry. This
module only looks at one runner and never changes anything, so a bug here can
misreport but cannot break a runner.

Owns mask() and SECRET_KEYS because app.py imports this module; importing them
back from app.py would be a circular import.
"""

SECRET_KEYS = {"GH_TOKEN"}


def mask(value):
    """Show enough to recognise a token, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "•" * 8 + value[-4:]
