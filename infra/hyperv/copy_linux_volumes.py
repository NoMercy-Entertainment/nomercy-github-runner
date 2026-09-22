"""Stream stopped WSL runner volumes into prepared, UUID-owned Hyper-V disks.

Uses binary pipes (never a PowerShell text pipeline), retaining numeric owners,
hardlinks, sparse files, ACLs and overlay xattrs. Neither listener is started.
"""
import hashlib
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

STAGE = Path("D:/HyperV/runner-platform/stage/maintenance-20260921")
SSH = ["C:/Windows/System32/OpenSSH/ssh.exe", "-i", "D:/HyperV/runner-platform/ssh/id_ed25519",
       "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=D:/HyperV/runner-platform/ssh/known_hosts",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "rnr-admin@10.77.0.20"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--essential', action='store_true', help='Keep original workspaces as rollback; copy other four areas only')
    essential = parser.parse_args().essential
    rows = json.loads((STAGE / "linux-source-inventory.json").read_text(encoding="utf-8-sig"))
    assert len(rows) == 13 and len({r["runner_id"] for r in rows}) == 13
    paths, transforms = [], []
    for row in rows:
        rid = row["runner_id"]
        assert re.fullmatch(r"[a-f0-9-]{36}", rid)
        for area, target in {"work": "work", "docker": "dind", "cache": "cache", "reg": "reg", "logs": "logs"}.items():
            if essential and area == 'work':
                continue
            relative = f"rnr-{rid}-{area}/_data"
            assert row["areas"][area]["path"] == "/var/lib/docker/volumes/" + relative
            paths.append(relative)
            transforms.append(f"--transform=flags=rh;s,^{relative},{rid}/mnt/{target},")
    guard = """import json,pathlib,subprocess,sys,os
rows=json.loads(sys.argv[1]); expected={'rnr-'+r['runner_id'] for r in rows}; seen=set()
for p in pathlib.Path('/var/lib/docker/containers').glob('*/config.v2.json'):
 c=json.loads(p.read_text()); name=c.get('Name','').lstrip('/')
 if name in expected:
  assert c.get('State',{}).get('Running') is False, name+' is not stopped'
  seen.add(name)
assert seen==expected, 'source runner identities changed'
for pid in subprocess.run(['pgrep','-x','dockerd'],capture_output=True,text=True).stdout.split():
 p=pathlib.Path('/proc')/pid
 own=os.stat('/'); other=(p/'root').stat()
 if (own.st_dev,own.st_ino)!=(other.st_dev,other.st_ino): continue
 args=(p/'cmdline').read_bytes().decode().split('\\0')
 if not args[0]: continue  # a terminated zombie cannot write source volumes
 data=args[args.index('--data-root')+1] if '--data-root' in args else '/var/lib/docker'
 assert data!='/var/lib/docker', 'source Docker engine is still active'
for name in ('Runner.Listener','forgejo-runner'):
 assert subprocess.run(['pgrep','-x',name],stdout=subprocess.DEVNULL).returncode==1, 'source process still active: '+name
"""
    subprocess.run(["wsl.exe", "-d", "github-runners", "-u", "root", "--exec", "python3", "-B", "-c", guard,
                    json.dumps(rows)], check=True)
    target = ["sudo", "-n", "tar", "--extract", "--file=-", "--directory=/var/lib/runner-data/runners",
              "--sparse", "--acls", "--xattrs", "--xattrs-include=*", "--numeric-owner", "--same-owner",
              "--no-overwrite-dir", *transforms]
    source = ["wsl.exe", "-d", "github-runners", "-u", "root", "--exec", "tar", "--create", "--file=-",
              "--directory=/var/lib/docker/volumes", "--sparse", "--acls", "--xattrs", "--xattrs-include=*",
              "--numeric-owner", *paths]
    result = {"runners": len(rows), "essential_only": essential, "complete": False, "bytes": 0}
    started = time.monotonic()
    digest = hashlib.sha256()
    with (STAGE / "linux-copy-source.log").open("wb") as source_log, (STAGE / "linux-copy-target.log").open("wb") as target_log:
        receiver = subprocess.Popen([*SSH, shlex.join(target)], stdin=subprocess.PIPE, stdout=target_log, stderr=target_log)
        sender = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=source_log)
        try:
            progress = 0
            while chunk := sender.stdout.read(4 * 1024 ** 2):
                receiver.stdin.write(chunk)
                digest.update(chunk)
                result["bytes"] += len(chunk)
                if result["bytes"] - progress >= 1024 ** 3:
                    progress = result["bytes"]
                    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
                    (STAGE / "linux-copy-progress.json").write_text(json.dumps(result, indent=2))
                    print(f"Copied {progress / 1024**3:.1f} GiB", flush=True)
            receiver.stdin.close()
            if sender.wait() != 0 or receiver.wait() != 0:
                raise RuntimeError("Archive transfer failed; retained source and logs identify the failure")
            result.update(complete=True, archive_sha256=digest.hexdigest(), elapsed_seconds=round(time.monotonic() - started, 1))
        finally:
            sender.stdout.close()
            if sender.poll() is None:
                sender.terminate()
            if receiver.poll() is None:
                receiver.stdin.close()
                receiver.wait(timeout=60)
            (STAGE / "linux-copy-progress.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
