"""Where one runner's data lives, derived from its id and nothing else.

Five areas per instance, from design section 15.1: workspace, nested engine
data, cache, registration, logs. Every one of them is named from the
`runner_id` alone.

**This is a security control, not a naming convention.** `clear_cache` deletes
what a runner owns, and "owns" has to mean something a machine can check. It
means this: every path a runner can be told to clear is generated here from its
own id, so a scope that names a shared location cannot be constructed. That is
why `runner_id` is validated as a UUID before it reaches a name - the store
only ever mints UUIDs, but this function is reachable from a request, and an id
of `../../var/lib` would otherwise become a path that escapes the runner it is
supposed to describe.

**Derived from the id, never from the name.** There is no parameter here for a
display name, and that absence is the guarantee: two runners may share a
display name, so storage keyed on one would be storage two runners share. A
`clear_cache` on either would then delete the other's work.

**`docker` is absent where it cannot exist.** A Windows runner has no nested
engine and a macOS appliance has no volume; both report None rather than a
plausible name. Inventing one would produce storage that is asked for, never
found, and reported as an error that has no cause.
"""
import os
import re

import providers

#: The five areas of section 15.1. `docker` exists on Linux only.
AREAS = ("work", "docker", "cache", "reg", "logs")

#: Volume names carry this so a runner's storage is recognisable in
#: `docker volume ls` beside volumes this platform does not own.
PREFIX = "rnr"

#: Where a Windows worker keeps its runners. One directory per runner, ACLed
#: to that runner's account, so the boundary is enforced by the OS and not only
#: by the names generated here.
WINDOWS_ROOT = os.environ.get("WINDOWS_RUNNER_ROOT", r"D:\runners")

#: Inside the macOS guest. Guest-local by design: the appliance is reset as a
#: whole, so nothing here outlives a reset.
MACOS_ROOT = os.environ.get("MACOS_RUNNER_ROOT", "/Users/runner/runners")

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)


class InvalidRunnerId(ValueError):
    """The id is not one this store could have minted.

    Refused rather than sanitised. Sanitising invites the question of whether
    the cleaned-up version still refers to the same runner, and the answer to a
    malformed identity is no identity, not a different one.
    """


def check(runner_id):
    """Validate and return the id. The gate every name passes through."""
    if not isinstance(runner_id, str) or not _UUID.match(runner_id):
        raise InvalidRunnerId(
            f"{runner_id!r} is not a UUID; storage names are only ever "
            f"derived from an id the store minted")
    return runner_id.lower()


def names(runner_id, platform=providers.LINUX):
    """The five storage areas for one runner, on one platform.

    Returns a dict with exactly the keys of AREAS. A value of None means the
    area does not exist on that platform, which is a different statement from
    an empty string and must stay distinguishable.
    """
    rid = check(runner_id)

    if platform == providers.LINUX:
        # Named volumes on the worker's engine. The `-docker` one is what lets
        # the nested engine use overlay2 instead of fuse-overlayfs, which is in
        # turn what makes its usage figures truthful - and a cache ceiling can
        # only be enforced against an honest number.
        return {area: f"{PREFIX}-{rid}-{area}" for area in AREAS}

    if platform == providers.WINDOWS:
        base = f"{WINDOWS_ROOT}\\{rid}"
        out = {area: f"{base}\\{area}" for area in AREAS}
        out["docker"] = None      # no nested engine on the Windows worker
        return out

    if platform == providers.MACOS:
        base = f"{MACOS_ROOT}/{rid}"
        out = {area: f"{base}/{area}" for area in AREAS}
        out["docker"] = None      # the appliance runs no engine
        return out

    raise ValueError(f"unknown platform {platform!r}")


def root(runner_id, platform=providers.LINUX):
    """The prefix every one of this runner's names begins with.

    The containment check `clear_cache` uses: a scope is this runner's only if
    its name starts with this.
    """
    rid = check(runner_id)
    if platform == providers.LINUX:
        return f"{PREFIX}-{rid}-"
    if platform == providers.WINDOWS:
        return f"{WINDOWS_ROOT}\\{rid}\\"
    if platform == providers.MACOS:
        return f"{MACOS_ROOT}/{rid}/"
    raise ValueError(f"unknown platform {platform!r}")


def owns(runner_id, name, platform=providers.LINUX):
    """Whether `name` is storage this runner owns.

    The question `clear_cache` must be able to answer before it deletes
    anything. Answered by construction rather than by inspection: a name is
    this runner's when it is one of the names generated for it.
    """
    if not isinstance(name, str) or not name:
        return False
    return name in set(v for v in names(runner_id, platform).values() if v)


def unit_name(runner_id):
    """The execution unit's own name, derived like its storage.

    Derived rather than chosen at creation, for the same reason the storage
    names are: a controller that crashed after the unit was created but before
    it recorded the handle must be able to find the unit again from the
    runner_id alone. A name picked at create time and lost in the crash would
    leave a unit nothing can attribute - which is what a half instance is.
    """
    return f"{PREFIX}-{check(runner_id)}"
