"""The agent's tests import it as the `agent` package, from the repository
root, exactly as it will be imported on a worker. Nothing from `dashboard/` is
put on the path: the agent must stand on its own, and a test that could reach
the dashboard would hide the day it started to depend on it."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
