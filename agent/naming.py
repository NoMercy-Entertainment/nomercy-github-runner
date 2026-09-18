"""Where one runner's unit and data live, derived from its id alone.

The agent's copy of `dashboard/store/storage.py`. The agent cannot import the
dashboard, and both sides must agree exactly: the controller computes these
names to reason about ownership and to find a unit after a crash, and the
agent uses them to create and remove what is actually there. A dashboard test
generates ids and compares every name from both copies, so they cannot drift.

The same two properties as the original, for the same reasons. Every name
comes from the runner_id and nothing else, so no two runners can share
storage and no request can point a runner at a path that is not its own. And
the id is validated as a UUID first, because this is reachable from a request
and an id like `../../var` would otherwise become a path.
"""
import re

AREAS = ("work", "docker", "cache", "reg", "logs")
PREFIX = "rnr"
WINDOWS_ROOT = r"D:\runners"
MACOS_ROOT = "/Users/runner/runners"

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class InvalidRunnerId(ValueError):
    pass


def check(runner_id):
    if not isinstance(runner_id, str) or not _UUID.match(runner_id):
        raise InvalidRunnerId(f"{runner_id!r} is not a UUID")
    return runner_id.lower()


def names(runner_id, platform="linux"):
    rid = check(runner_id)
    if platform == "linux":
        return {area: f"{PREFIX}-{rid}-{area}" for area in AREAS}
    if platform == "windows":
        out = {area: f"{WINDOWS_ROOT}\\{rid}\\{area}" for area in AREAS}
        out["docker"] = None
        return out
    if platform == "macos":
        out = {area: f"{MACOS_ROOT}/{rid}/{area}" for area in AREAS}
        out["docker"] = None
        return out
    raise ValueError(f"unknown platform {platform!r}")


def unit_name(runner_id):
    return f"{PREFIX}-{check(runner_id)}"
