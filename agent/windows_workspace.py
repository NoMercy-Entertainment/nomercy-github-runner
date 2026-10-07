"""Short Windows workspace aliases keep NMake's generated paths below MAX_PATH.

The alias points to the runner's existing workspace. Its volume, ACL and cache
cleanup ownership remain those of the canonical runner tree.
"""
import base64
import ntpath
import os
from pathlib import Path

from . import naming


def alias_path(base, runner_id):
    base = ntpath.normpath(base)
    drive = ntpath.splitdrive(base)[0]
    if not ntpath.isabs(base) or len(drive) != 2 or drive[1] != ":" or len(base) <= 3:
        raise ValueError("short workspace root must be an absolute drive path")
    return ntpath.join(base, naming.check(runner_id).replace("-", ""))


def ensure_alias(base, runner_id, workspace, run, icacls, powershell):
    root, target = Path(base), Path(workspace)
    alias = Path(alias_path(base, runner_id))
    if not target.is_dir() or os.path.isjunction(target) or target.is_symlink():
        raise RuntimeError("canonical workspace must be an ordinary directory")
    if os.path.isjunction(root) or root.is_symlink():
        raise RuntimeError("short workspace root must not be a link")
    root.mkdir(parents=True, exist_ok=True)
    ok, out, err = run([icacls, str(root), "/inheritance:r", "/grant:r",
                       "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                       "*S-1-5-11:(OI)(CI)RX", "/Q"], timeout=30)
    if not ok:
        raise RuntimeError(err or out or "cannot protect workspace alias root")
    if os.path.lexists(alias):
        if not os.path.isjunction(alias) or alias.resolve() != target.resolve():
            raise RuntimeError("workspace alias points to a different directory")
        return str(alias)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
              + quote(alias) + " -Value " + quote(target) + " | Out-Null")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    ok, out, err = run([powershell, "-NoProfile", "-NonInteractive",
                       "-EncodedCommand", encoded], timeout=30)
    if not ok or not os.path.isjunction(alias) or alias.resolve() != target.resolve():
        raise RuntimeError(err or out or "workspace alias was not confirmed")
    return str(alias)


def remove_alias(base, runner_id, workspace):
    alias, target = Path(alias_path(base, runner_id)), Path(workspace)
    if not os.path.lexists(alias):
        return
    if not os.path.isjunction(alias) or alias.resolve() != target.resolve():
        raise RuntimeError("workspace alias ownership changed; removal held")
    # rmdir removes the junction entry itself without walking its target.
    os.rmdir(alias)
