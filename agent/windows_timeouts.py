"""Registration deadlines for Windows workers.

The pipe client and its parent must outlive the service-side handler.
"""


def service_identity_timeout():
    """Identity verification is mandatory: one short PowerShell call."""
    return 10


def registration_limits():
    # The pipe waits for the job host to start and then for the whole
    # script, so it needs both.
    return {"key_setup": 30, "script": 105, "pipe": 115, "client": 140}


def shell_timeout():
    """One short PowerShell or icacls call."""
    return 30
