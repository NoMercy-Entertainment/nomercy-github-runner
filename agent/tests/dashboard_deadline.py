"""The controller's outer deadline for an agent verb (dashboard/control/
agent_runtime.py DEADLINE), read from its source so the agent's own bounds
can be checked against it without importing the dashboard."""
import re
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "dashboard" / "control" / "agent_runtime.py"
CONTROLLER_DEADLINE = int(re.search(r"^DEADLINE = (\d+)$", _SOURCE.read_text(encoding="utf-8"), re.M)[1])
