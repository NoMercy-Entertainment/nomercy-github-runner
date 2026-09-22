"""Initialize only the new, blank, size-verified maintenance data disk.

Run as root inside the selected guest. Existing filesystems are never formatted.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess


def run(*argv):
    return subprocess.check_output(argv, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("linux", "macos"))
    args = parser.parse_args()
    size, target = {
        "linux": (2304, "/var/lib/runner-data"),
        "macos": (768, "/var/lib/runner-appliances"),
    }[args.role]
    device = "/dev/sdb"
    rows = json.loads(run("lsblk", "--json", "--bytes", "--output",
                          "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS", device))["blockdevices"]
    if len(rows) != 1:
        raise RuntimeError("Unexpected device topology")
    row = rows[0]
    if (row["type"] != "disk" or int(row["size"]) != size * 1024 ** 3
            or row.get("children") or any(row.get("mountpoints") or [])):
        raise RuntimeError("Device is not the expected unmounted data disk")
    mount = Path(target)
    if mount.is_symlink() or (mount.exists() and any(mount.iterdir())):
        raise RuntimeError("Mount directory must be empty and not a symlink")
    fstab = Path("/etc/fstab")
    previous = fstab.read_text()
    if any(target in line.split() for line in previous.splitlines() if not line.startswith("#")):
        raise RuntimeError("Data mount is already configured; inspect it instead of reinitializing")
    signatures = json.loads(run("wipefs", "--no-act", "--json", device))["signatures"]
    if row.get("fstype") or signatures:
        raise RuntimeError("Data disk contains a signature; refusing to format")
    backup = Path("/etc/fstab.before-runner-data-20260921")
    if backup.exists():
        raise RuntimeError("A previous initialization exists; inspect it before retry")
    shutil.copy2(fstab, backup)
    run("mkfs.ext4", "-m", "0", "-L", "runner-" + args.role, device)
    uuid = run("blkid", "-s", "UUID", "-o", "value", device)
    mount.mkdir(mode=0o700, parents=True, exist_ok=True)
    fstab.write_text(previous.rstrip() + f"\nUUID={uuid} {target} ext4 defaults 0 2\n")
    run("systemctl", "daemon-reload")
    run("mount", target)
    mount.chmod(0o700)
    print(run("findmnt", "--json", "--mountpoint", target))


if __name__ == "__main__":
    main()
