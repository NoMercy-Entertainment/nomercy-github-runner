"""The agent's tests import it as the `agent` package, from the repository
root, exactly as it will be imported on a worker. Nothing from `dashboard/` is
put on the path: the agent must stand on its own, and a test that could reach
the dashboard would hide the day it started to depend on it."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def isolate_native_event_spool(monkeypatch):
    """Hook subprocess tests must never inherit their CI runner's live spool."""
    for key in ("NOMERCY_RUNNER_ID", "NOMERCY_JOB_EVENT_DIR"):
        monkeypatch.delenv(key, raising=False)
