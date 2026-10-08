"""Opt-in, per-runner ext4 filesystems. Commands are fixed literal argv.

The configured root is private to the agent. Metadata and filesystem UUIDs
bind a sparse image to one runner. Existing images are never formatted.
Call ensure/remove/reset_registration under lock(), including Docker changes.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from contextlib import contextmanager

from .. import naming

DEFAULT_BYTES = 100 * 1024 ** 3
MIN_BYTES = 64 * 1024 ** 2
AREA_DIRS = {"work": "work", "docker": "dind", "cache": "cache",
             "reg": "reg", "logs": "logs"}


def execute(argv, timeout=120):
    result = subprocess.run([*argv], text=True, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class StorageError(RuntimeError):
    pass


class LinuxStorage:
    def __init__(self, root, default_bytes=DEFAULT_BYTES, run=None):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root == self.root.parent:
            raise ValueError("storage root must be an absolute private directory")
        if any(ord(char) < 32 for char in str(root)):
            raise ValueError("storage root contains control characters")
        self.default_bytes = self._size(default_bytes)
        self.run = run or execute

    @staticmethod
    def _size(value):
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_BYTES <= value <= 2 ** 63 - 1:
            raise ValueError("disk_limit must be an integer of at least 64 MiB")
        return value

    @staticmethod
    def _safe(path, directory=False, private=False):
        """No symlinks, unexpected owner, hardlinks or writable control files."""
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (directory and not stat.S_ISDIR(info.st_mode)) or (
                not directory and not stat.S_ISREG(info.st_mode)):
            raise StorageError(f"unexpected storage object: {path}")
        if not directory and info.st_nlink != 1:
            raise StorageError(f"multiply linked storage file: {path}")
        if os.name == "posix" and private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise StorageError(f"storage control path is not privately owned: {path}")

    def _root(self, create=False):
        for parent in reversed((self.root, *self.root.parents)):
            if parent.exists() or parent.is_symlink():
                self._safe(parent, directory=True)
            elif parent != self.root:
                raise StorageError(f"storage root parent does not exist: {parent}")
        if not self.root.exists():
            if not create:
                raise StorageError("configured storage root does not exist")
            self.root.mkdir(mode=0o700)
        self._safe(self.root, directory=True, private=True)

    @contextmanager
    def lock(self, create=True):
        self._root(create=create)
        lock = self.root / ".lock"
        flags = (os.O_CREAT if create else 0) | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock, flags, 0o600)
        try:
            self._safe(lock, private=True)
            if os.name == "nt":  # allows deterministic fake-executor tests
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _paths(self, rid):
        rid = naming.check(rid)
        base = self.root / rid
        return base, base / "metadata.json", base / "disk.ext4", base / "mnt"

    def _command(self, argv, timeout=120):
        code, out, error = self.run(argv, timeout=timeout)
        if code != 0:
            raise StorageError(f"{argv[0]} failed: {error or out}")
        return out

    def _metadata(self, rid):
        self._root()
        base, meta, image, mount = self._paths(rid)
        self._safe(base, directory=True, private=True)
        self._safe(meta, private=True)
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise StorageError(f"invalid storage metadata: {meta}") from error
        if (not isinstance(data, dict) or set(data) != {"version", "runner_id", "bytes", "fs_uuid"}
                or data["version"] != 1 or data["runner_id"] != naming.check(rid)
                or data["fs_uuid"] != naming.check(rid)):
            raise StorageError("storage metadata does not identify this runner")
        self._size(data["bytes"])
        if set(p.name for p in base.iterdir()) - {"metadata.json", "disk.ext4", "mnt"}:
            raise StorageError("unknown files in runner storage directory")
        return data

    def _filesystem(self, rid, data):
        _, _, image, _ = self._paths(rid)
        self._safe(image, private=True)
        if image.stat().st_size != data["bytes"]:
            raise StorageError("existing image size differs from its recorded limit")
        out = self._command(["blkid", "-p", "-o", "export", str(image)])
        fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if fields.get("TYPE") != "ext4" or fields.get("UUID") != data["fs_uuid"]:
            raise StorageError("existing filesystem type or UUID is not owned by this runner")

    def _mounted(self, rid):
        _, _, image, mount = self._paths(rid)
        self._safe(mount, directory=True)
        code, out, error = self.run(["findmnt", "--json", "--mountpoint", str(mount),
                                    "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"], timeout=30)
        if code == 1 and not out and not error:
            return False
        if code:
            raise StorageError(f"mount state could not be read: {error or out}")
        try:
            rows = json.loads(out)["filesystems"]
            row = rows[0]
            if len(rows) != 1 or row["target"] != str(mount) or row["fstype"] != "ext4":
                raise ValueError("unexpected filesystem")
            source = row["source"]
            if not source.startswith("/dev/loop") or not source[len("/dev/loop"):].isdigit():
                raise ValueError("not an owned loop device")
            if "rw" not in row["options"].split(","):
                raise ValueError("filesystem is not writable")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise StorageError("unexpected runner mount") from error
        out = self._command(["losetup", "--json", "--list", "--output",
                             "NAME,BACK-FILE,OFFSET,SIZELIMIT,RO", source])
        try:
            loops = json.loads(out)["loopdevices"]
            loop = loops[0]
            if (len(loops) != 1 or loop["name"] != source or loop["back-file"] != str(image)
                    or int(loop["offset"]) != 0 or int(loop["sizelimit"]) != 0 or loop["ro"] not in (False, 0)):
                raise ValueError("unexpected backing configuration")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise StorageError("loop device is backed by a different image or range") from error
        return True

    def _no_child_mounts(self, rid):
        mount = self._paths(rid)[3]
        out = self._command(["findmnt", "--json", "--submounts", "--mountpoint", str(mount),
                             "--output", "TARGET"])
        try:
            rows = json.loads(out)["filesystems"]
            if len(rows) != 1 or rows[0]["target"] != str(mount) or rows[0].get("children"):
                raise ValueError("unexpected mount tree")
        except (KeyError, ValueError, TypeError) as error:
            raise StorageError("nested mounts prevent safe storage maintenance") from error

    def ensure(self, rid, limit=None):
        rid = naming.check(rid)
        size = self._size(self.default_bytes if limit is None else limit)
        base, meta, image, mount = self._paths(rid)
        if not base.exists() and not base.is_symlink():
            base.mkdir(mode=0o700)
        self._safe(base, directory=True, private=True)
        if not meta.exists():
            if list(base.iterdir()):
                raise StorageError("unidentified existing runner storage; explicit migration required")
            data = {"version": 1, "runner_id": rid, "bytes": size, "fs_uuid": rid}
            fd = os.open(meta, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
        data = self._metadata(rid)
        if data["bytes"] != size:
            raise StorageError("changing an existing disk limit requires an explicit offline migration")
        if not image.exists() and not image.is_symlink():
            fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.truncate(size)
                handle.flush()
                os.fsync(handle.fileno())
            # Only this newly created file is formatted. Never retry mkfs on
            # an existing image, even when the previous format was interrupted.
            self._command(["mkfs.ext4", "-q", "-F", "-m", "0", "-U", rid,
                           "-E", "lazy_itable_init=0,lazy_journal_init=0", str(image)], timeout=600)
        self._filesystem(rid, data)
        if not mount.exists() and not mount.is_symlink():
            mount.mkdir(mode=0o700)
        if not self._mounted(rid):
            if list(mount.iterdir()):
                raise StorageError("unmounted backing directory is not empty; refusing to hide data")
            self._command(["mount", "-t", "ext4", "-o", "loop,rw,nodev,nosuid", str(image), str(mount)])
            if not self._mounted(rid):
                raise StorageError("runner filesystem did not mount")
        paths = {}
        for area, directory in AREA_DIRS.items():
            path = mount / directory
            if not path.exists() and not path.is_symlink():
                path.mkdir(mode=0o755)
            self._safe(path, directory=True)
            if path.stat().st_dev != mount.stat().st_dev or os.path.ismount(path):
                raise StorageError("runner area is not on its owned filesystem")
            paths[area] = str(path)
        return paths

    def has_disk(self, rid):
        """Whether this runner has a filesystem of its own here - its
        directory exists - without validating or mounting anything."""
        base = self._paths(rid)[0]
        return base.exists() or base.is_symlink()

    def existing(self, rid):
        base, _, _, _ = self._paths(rid)
        if not base.exists() and not base.is_symlink():
            return None
        data = self._metadata(rid)
        self._filesystem(rid, data)
        if not self._mounted(rid):
            raise StorageError("runner filesystem is not mounted")
        return data

    def reset_registration(self, rid):
        self.existing(rid)
        self._no_child_mounts(rid)
        path = self._paths(rid)[3] / AREA_DIRS["reg"]
        self._safe(path, directory=True)
        # No recursive host deletion: fixed find stays on the owned fs, and
        # does not follow symlinks. The container and reg volume are gone.
        self._command(["find", str(path), "-xdev", "-mindepth", "1", "-delete"])

    def remove(self, rid):
        base, meta, image, mount = self._paths(rid)
        if not base.exists() and not base.is_symlink():
            return
        data = self._metadata(rid)
        if image.exists() or image.is_symlink():
            self._filesystem(rid, data)
        if mount.exists() or mount.is_symlink():
            if self._mounted(rid):
                self._no_child_mounts(rid)
                self._command(["umount", str(mount)])
                if self._mounted(rid):
                    raise StorageError("runner filesystem is still mounted")
            mount.rmdir()
        if image.exists():
            # A lazy unmount or a mount elsewhere must not turn removal into
            # an unlinked but still active backing file.
            out = self._command(["losetup", "--json", "--list", "--associated", str(image),
                                 "--output", "NAME,BACK-FILE"])
            try:
                if json.loads(out).get("loopdevices"):
                    raise StorageError("runner image is still attached to a loop device")
            except (ValueError, TypeError) as error:
                raise StorageError("loop ownership could not be checked") from error
        # Only these three known paths are deleted; never recurse on root.
        if image.exists():
            self._safe(image, private=True)
            image.unlink()
        self._safe(meta, private=True)
        meta.unlink()
        base.rmdir()

    def telemetry(self, rid):
        with self.lock(create=False):
            return self._telemetry(rid)

    def _telemetry(self, rid):
        data = self.existing(rid)
        if data is None:
            return {"disk_limit_enforced": False}
        usage = shutil.disk_usage(self._paths(rid)[3])
        return {"disk_limit_enforced": True, "disk_limit_bytes": data["bytes"],
                "disk_used_bytes": usage.used, "disk_free_bytes": usage.free,
                "disk_usable_bytes": usage.total}

    def mount_all(self):
        with self.lock():
            for child in sorted(self.root.iterdir()):
                if child.name == ".lock":
                    continue
                rid = naming.check(child.name)
                data = self._metadata(rid)
                # Boot only resumes existing images; it never formats one.
                self._filesystem(rid, data)
                self.ensure(rid, data["bytes"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and mount runner filesystems before Docker starts")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    LinuxStorage(args.root).mount_all()


if __name__ == "__main__":
    main()
