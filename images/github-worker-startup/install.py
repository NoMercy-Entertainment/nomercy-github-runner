"""Atomically replace only the managed Linux Workers' assembly, keeping live jobs.

Linux keeps the old inode for already loaded assemblies. A newly started Worker
loads the replacement; no Listener, container, daemon or VM is restarted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory():
    ids = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=nomercy.runner_id"],
                                  text=True, timeout=60).split()
    units = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True, timeout=60)) if ids else []
    found = []
    for unit in units:
        rid = unit["Config"]["Labels"]["nomercy.runner_id"]
        if str(uuid.UUID(rid)) != rid:
            raise ValueError("Unexpected runner identity")
        reg = next(m for m in unit["Mounts"] if m["Destination"] == "/runner/reg")
        expected = Path("/var/lib/docker/volumes") / ("rnr-" + rid + "-reg") / "_data"
        if reg["Type"] != "volume" or Path(reg["Source"]) != expected:
            raise ValueError("Unexpected registration volume")
        registration = json.loads((expected / ".runner").read_text(encoding="utf-8-sig"))
        name = registration.get("agentName", "")
        if not name.startswith("github-linux-"):
            continue
        target = expected / "actions-runner/bin/Runner.Worker.dll"
        if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError("Worker assembly is not an owned regular file")
        found.append({"runner_id": rid, "name": name, "target": str(target),
                      "sha256": digest(target.read_bytes())})
    if not found:
        raise ValueError("No managed GitHub Linux Workers found")
    return sorted(found, key=lambda row: row["name"])


def atomic_replace(target, data):
    target = Path(target)
    info = target.stat()
    fd, temporary = tempfile.mkstemp(prefix=".worker-startup-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), info.st_mode & 0o777)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(bundle, apply=False):
    manifest = json.loads((bundle / "worker-startup.json").read_text())
    data = (bundle / "Runner.Worker.dll").read_bytes()
    original = "5b70067b6cbf706bcccdc73ef4a3ce0b118a86ad5cdf82b54dcca96724c67a0f"
    if (manifest.get("original_sha256") != original or manifest.get("patch") != "worker-startup"
            or manifest.get("upstream_commit") != "197ca027b90de199464dee3bb9ff198a1db60967"
            or digest(data) != manifest.get("worker_sha256")):
        raise ValueError("Patch bundle does not match the verified build")
    plan = inventory()
    for row in plan:
        if row["sha256"] not in (original, manifest["worker_sha256"]):
            raise ValueError("Unexpected Worker version: " + row["name"])
    print(json.dumps({"apply": apply, "worker_sha256": manifest["worker_sha256"], "runners": plan}), flush=True)
    pending = [row for row in plan if row["sha256"] == original]
    if not apply or not pending:
        return
    parent = Path("/var/lib/runner-agent/maintenance")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="worker-startup-", dir=parent))
    shutil.copyfile(pending[0]["target"], backup / "Runner.Worker.original.dll")
    (backup / "deployment.json").write_text(json.dumps({"manifest": manifest, "runners": plan}, indent=2))
    print("BACKUP " + str(backup), flush=True)
    for row in pending:
        # Refuse a concurrent update instead of overwriting another repair.
        if digest(Path(row["target"]).read_bytes()) != original:
            raise ValueError("Worker changed during installation: " + row["name"])
        atomic_replace(row["target"], data)
        if digest(Path(row["target"]).read_bytes()) != manifest["worker_sha256"]:
            raise RuntimeError("Worker verification failed: " + row["name"])
        print("INSTALLED " + row["name"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("Run as root on the Linux worker VM")
    install(args.bundle, args.apply)
