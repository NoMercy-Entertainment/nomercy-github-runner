"""A Worker already using an assembly must retain its bytes during deployment."""
import importlib.util
import mmap
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load_installer():
    source = Path(__file__).resolve().parents[2] / "images/github-worker-startup/install.py"
    spec = importlib.util.spec_from_file_location("worker_startup_install", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerStartupInstallTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Linux replacement of a mapped assembly")
    def test_loaded_worker_keeps_original_bytes_and_new_worker_reads_replacement(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            binary = path / "Runner.Worker.dll"
            binary.write_bytes(b"original worker assembly")
            binary.chmod(0o640)
            before = binary.stat()
            with binary.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as loaded:
                installer.atomic_replace(binary, b"corrected worker assembly")
                self.assertEqual(loaded[:], b"original worker assembly")
                self.assertEqual(stream.read(), b"original worker assembly")
                self.assertEqual(binary.read_bytes(), b"corrected worker assembly")
            after = binary.stat()
            self.assertEqual((after.st_uid, after.st_gid, after.st_mode & 0o777),
                             (before.st_uid, before.st_gid, 0o640))
            self.assertFalse(list(path.glob(".worker-startup-*")))

    def test_invalid_bundle_cannot_touch_any_registration(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "worker-startup.json").write_text('{"patch":"worker-startup"}')
            (path / "Runner.Worker.dll").write_bytes(b"unverified payload")
            with patch.object(installer, "inventory", side_effect=AssertionError("Accessed registrations")):
                with self.assertRaisesRegex(ValueError, "verified build"):
                    installer.install(path, apply=True)
