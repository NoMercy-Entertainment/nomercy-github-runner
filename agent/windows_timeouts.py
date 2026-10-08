"""Registration deadlines for native and emulated Windows workers.

An ARM64 worker under TCG took over two minutes just to start Windows
PowerShell (2026-10-02). Keep the existing x64 bounds, while allowing the
ARM template's 600-second registration process plus shell startup. The
pipe client and its parent must outlive the service-side handler.
"""
import platform


def service_identity_timeout(architecture=None):
    """Keep identity verification mandatory while allowing ARM emulation startup."""
    architecture = (architecture or platform.machine()).lower()
    return 60 if architecture in ("arm64", "aarch64") else 10


def registration_limits(architecture=None):
    architecture = (architecture or platform.machine()).lower()
    if architecture in ("arm64", "aarch64"):
        return {"key_setup": 600, "script": 900, "pipe": 915, "client": 940}
    return {"key_setup": 30, "script": 105, "pipe": 115, "client": 140}
