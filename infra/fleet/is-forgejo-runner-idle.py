"""Print one Forgejo runner's status: idle, active, offline - or exit 1.

Used by Retire-LegacyWindowsRunner.ps1 before it stops a runner's service:
"active" means it is running a job, and nothing may be stopped then. Reads
the deployment's own token from .env and writes nothing anywhere.
"""
import json
import sys
import urllib.request

name = sys.argv[1]
env = {}
with open(r"D:\docker-compose\GithubRunners\.env", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')

req = urllib.request.Request(
    env["FORGEJO_INSTANCE_URL"].rstrip("/")
    + "/api/v1/user/actions/runners?limit=100",
    headers={"Authorization": "token " + env["FORGEJO_API_TOKEN"],
             "Accept": "application/json"})
with urllib.request.urlopen(req, timeout=20) as answer:
    runners = json.load(answer)

for runner in runners:
    if runner.get("name") == name:
        print(runner.get("status") or "unknown")
        sys.exit(0)
print(f"no runner named {name} at this forge", file=sys.stderr)
sys.exit(1)
