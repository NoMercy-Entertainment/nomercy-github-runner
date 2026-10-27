"""Copy the image's hosted tool cache into this runner's own tool cache.

The GitHub runner's tool cache is <work>/_tool, on the unit's work volume:
what setup-python and setup-node look in before they download. The image
carries Python and Node in /opt/hostedtoolcache (images/linux/unit/toolcache/);
each `<tool>/<version>` that is complete there and not complete here is copied,
through a temporary name, so a job never sees half a version. A version that
is complete here is never touched - a job may have pip-installed into it -
and nothing a job added is removed. Links are never followed.

    python3 toolcache.py SOURCE DESTINATION
"""
import shutil
import sys
import uuid
from pathlib import Path

ARCH = "x64"


def _complete(version_dir):
    return (version_dir / f"{ARCH}.complete").is_file() and (version_dir / ARCH).is_dir()


def _plain_dir(path):
    return path.is_dir() and not path.is_symlink()


def seed(source, destination):
    """Copy every complete version missing from `destination`; returns what
    was copied as `tool/version`."""
    source, destination = Path(source), Path(destination)
    if not _plain_dir(source):
        return []
    if destination.is_symlink():
        return []
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for tool in sorted(source.iterdir()):
        if not _plain_dir(tool):
            continue
        target_tool = destination / tool.name
        if target_tool.is_symlink() or (target_tool.exists() and not target_tool.is_dir()):
            continue
        for version in sorted(tool.iterdir()):
            if not _plain_dir(version) or not _complete(version):
                continue
            target = target_tool / version.name
            if target.is_symlink():
                continue
            if _plain_dir(target) and _complete(target):
                continue
            target_tool.mkdir(exist_ok=True)
            staging = target_tool / f".{version.name}.seeding-{uuid.uuid4().hex[:8]}"
            try:
                shutil.copytree(version, staging, symlinks=True)
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            copied.append(f"{tool.name}/{version.name}")
    return copied


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 64
    copied = seed(argv[1], argv[2])
    for item in copied:
        print(f"tool cache: {item} added")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
