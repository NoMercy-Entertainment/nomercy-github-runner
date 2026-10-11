"""Compare only guard files against their immutable image, without a filesystem scan."""
import hashlib
import re
import subprocess
import tarfile
import threading
import uuid

MAX_FILE_BYTES = 256 * 1024


def archive_digest(container, path):
    """Read one regular file through Docker; never execute code inside the unit."""
    process = subprocess.Popen(["docker", "cp", f"{container}:{path}", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(30, process.kill)
    timer.daemon = True
    timer.start()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            member = archive.next()
            if (member is None or not member.isfile() or member.islnk()
                    or member.name not in (path.rsplit("/", 1)[-1], "./" + path.rsplit("/", 1)[-1])
                    or not 0 < member.size <= MAX_FILE_BYTES):
                raise ValueError("Guard archive is not one bounded regular file")
            source = archive.extractfile(member)
            content = source.read(member.size + 1)
            if len(content) != member.size:
                raise ValueError("Incomplete guard file")
        if process.wait(timeout=5) != 0:
            raise RuntimeError("Docker could not read the guard file")
        return hashlib.sha256(content).hexdigest()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


class ImageGuardFiles:
    def __init__(self, run, read=archive_digest):
        self.run, self.read = run, read
        self.baselines = {}
        self.lock = threading.Lock()

    def _baseline(self, image, paths):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Guard baseline requires an immutable image ID")
        key = image, tuple(paths)
        with self.lock:
            if key in self.baselines:
                return self.baselines[key]
            # This unit is never started, has no runner registration, and
            # shares only the original image's immutable layers.
            name = "nomercy-guard-baseline-" + uuid.uuid4().hex
            ok, container, error = self.run([
                "create", "--name", name, "--network", "none", "--read-only",
                "--entrypoint", "/bin/true", image], timeout=30)
            if not ok:
                raise RuntimeError("Could not prepare the original guard image")
            container = container.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", container):
                raise RuntimeError("Docker returned an invalid baseline identity")
            try:
                baseline = {path: self.read(container, path) for path in paths}
            finally:
                removed, _, _ = self.run(["rm", container], timeout=30)
                if not removed:
                    raise RuntimeError("Guard baseline cleanup was not confirmed")
            self.baselines[key] = baseline
            return baseline

    def __call__(self, container, image, paths):
        try:
            baseline = self._baseline(image, paths)
            changed = []
            for path in paths:
                try:
                    digest = self.read(container, path)
                except ValueError:
                    changed.append(path)
                    continue
                if digest != baseline[path]:
                    changed.append(path)
            return changed
        except (OSError, RuntimeError, ValueError, tarfile.TarError,
                subprocess.SubprocessError):
            return None
