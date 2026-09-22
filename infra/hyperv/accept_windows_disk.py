"""Elevated, disposable 1 GiB VHDX acceptance; never touches a production UUID."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent.runtimes.windows_storage import WindowsStorage, verify_volume

RID = "ab28eb07-3d5b-43ed-b75b-28d39b99a430"
REPORT = Path("D:/HyperV/runner-platform/stage/maintenance-20260921/windows-disk-acceptance.json")


def run(argv, input=None, timeout=120):
    p = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    return p.returncode == 0, p.stdout, p.stderr


def main():
    report = {"runner_id": RID, "passed": False}
    storage = WindowsStorage({"root": "D:/runner-disks-acceptance", "default_limit": 1024 ** 3},
                             run, "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    mounted = Path("D:/runners") / RID
    if mounted.exists():
        raise RuntimeError("Acceptance path already exists; inspect before retry")
    try:
        state = storage.ensure(RID)
        report["created"] = state
        verify_volume(mounted, state["volume_guid"])
        report["mount_verified"] = True
        written = 0
        try:
            with (mounted / "fill-proof.bin").open("wb", buffering=0) as stream:
                for _ in range(80):
                    written += stream.write(b"x" * (16 * 1024 ** 2))
        except OSError as error:
            if error.errno != 28 and getattr(error, "winerror", None) != 112:
                raise
            report["full_error"] = str(error)
        else:
            raise RuntimeError("Writing beyond disk capacity unexpectedly succeeded")
        report["written_bytes"] = written
        proof = REPORT.with_suffix(".host-proof")
        proof.write_text("host writable while runner filesystem is full\n")
        proof.unlink()
        (mounted / "fill-proof.bin").unlink()
        report["mounted_again"] = storage.mount(RID)
        report["verified_again"] = storage.verify(RID)
        storage.remove(RID)
        if mounted.exists():
            raise RuntimeError("Acceptance mount remains after removal")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        REPORT.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
