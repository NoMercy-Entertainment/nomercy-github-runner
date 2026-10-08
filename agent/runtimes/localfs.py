"""The worker's own disk, for the runtimes that keep a runner's data in plain
directories rather than engine volumes - Windows and macOS.

One small interface so the tests can put an in-memory disk in its place: the
fakes in agent/tests implement the same methods. Every method takes a full
path the runtime derived from a runner_id; nothing here decides where
anything goes.
"""
import os
import shutil


def df_figures(text):
    """{"used_bytes", "total_bytes"} of the volume `df -k` (BSD) or `df -Pk`
    (GNU) described, from its last line - or None when it said nothing a
    size can be read from. Both print 1024-byte blocks: total, Used and
    Available in the second to fourth columns, whatever follows them.

    Used is total less Available - everything a runner can no longer write
    to - not df's Used column, which leaves out ext4's reserved blocks and,
    on APFS, everything the Data volume's sibling volumes hold in the
    container they share. `LocalFs.disk_usage` reads it the same way."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    try:
        total, available = int(parts[1]) * 1024, int(parts[3]) * 1024
    except (IndexError, ValueError):
        return None
    if total <= 0 or not 0 <= available <= total:
        return None
    return {"used_bytes": total - available, "total_bytes": total}


class LocalFs:
    def exists(self, path):
        return os.path.exists(path)

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def listdir(self, path):
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def copytree(self, src, dst):
        shutil.copytree(src, dst, dirs_exist_ok=True)

    def write_text(self, path, text, mode=None):
        """Atomically, so a reader never sees half a file. `mode` is applied
        before the content is written, so a private file is never briefly
        readable."""
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     mode if mode is not None else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def read_text(self, path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def tail(self, path, max_bytes):
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            return fh.read().decode("utf-8", errors="replace")

    def mtime(self, path):
        return os.path.getmtime(path)

    def remove(self, path):
        if os.path.lexists(path):
            os.unlink(path)

    def rmtree(self, path):
        if os.path.exists(path):
            shutil.rmtree(path)

    def clear_dir(self, path):
        """Delete what is inside `path`, keeping `path`. Continues past what
        cannot be deleted and raises once, naming how much was left."""
        failed = []
        for name in os.listdir(path) if os.path.isdir(path) else ():
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            except OSError as e:
                failed.append(f"{name}: {e.strerror}")
        if failed:
            raise OSError(f"{len(failed)} entries could not be deleted: "
                          + "; ".join(failed[:3]))

    def du(self, path):
        """Bytes under `path`, or None when it cannot be read - never 0."""
        if not os.path.isdir(path):
            return None
        total = 0
        for base, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        return total

    def disk_usage(self, path):
        """The volume holding `path`: {"used_bytes", "total_bytes"}, or None
        when it cannot be read - never 0. Used is total less what may still
        be written, as `df_figures` reads it."""
        if not os.path.exists(path):
            return None
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        if usage.total <= 0 or not 0 <= usage.free <= usage.total:
            return None
        return {"used_bytes": usage.total - usage.free,
                "total_bytes": usage.total}
