"""Signed forge deliveries become job events; unknown runners are ignored."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from control.job_events import JobEvents, RESULTS
from store import schema

MAX_BODY = 2 * 1024 * 1024
_setup_lock = threading.Lock()


def configure(provider, env, public_url, data_directory):
    """Create or repair only this dashboard's hook, preserving other hooks."""
    if provider not in ("github", "forgejo"):
        raise ValueError("Unknown forge")
    parts = urllib.parse.urlsplit(public_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError("Set an HTTPS DASH_PUBLIC_URL before enabling job deliveries")
    import providers
    client = providers.by_key(provider).forge_client(env)
    if client is None:
        raise ValueError("The forge credential is not configured")
    api = "https://api.github.com" if provider == "github" else client.base
    hook_path = "/orgs/" + urllib.parse.quote(client.org, safe="") + "/hooks" if provider == "github" else "/api/v1/user/hooks"
    headers = client._headers() if provider == "github" else {
        "Authorization": "token " + client.token, "Accept": "application/json"}
    headers["Content-Type"] = "application/json"

    def call(method, path, body=None):
        request = urllib.request.Request(api + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # The forge may echo submitted configuration, including secrets.
            raise ValueError("Webhook management was refused by %s (HTTP %s). The credential needs webhook access." %
                             (provider, error.code)) from None
        except (OSError, ValueError):
            raise ValueError("The forge did not answer webhook management") from None

    url = public_url.rstrip("/") + "/hooks/" + provider + "/jobs"
    with _setup_lock:
        key = Path(data_directory) / ("job-webhook-" + provider + ".key")
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_urlsafe(48).encode())
        secret = key.read_bytes().decode()
        if len(secret) < 32:
            raise ValueError("The webhook signing key is invalid")
        existing = None
        for page in range(1, 11):
            rows = call("GET", hook_path + "?per_page=100&limit=100&page=" + str(page))
            if not isinstance(rows, list):
                raise ValueError("The forge returned an invalid hook list")
            existing = next((row for row in rows if isinstance(row, dict) and
                             (row.get("config") or {}).get("url", row.get("url")) == url), None)
            if existing or len(rows) < 100:
                break
        else:
            raise ValueError("Too many hooks to safely identify this dashboard's hook")
        body = {"active": True, "events": ["workflow_job"],
                "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}}
        if existing:
            if type(existing.get("id")) is not int:
                raise ValueError("The forge returned an invalid hook identity")
            result = call("PATCH", hook_path + "/" + str(existing["id"]), body)
        else:
            body["name" if provider == "github" else "type"] = "web" if provider == "github" else "forgejo"
            result = call("POST", hook_path, body)
        if not isinstance(result, dict) or type(result.get("id")) is not int:
            raise ValueError("The forge returned an invalid created hook")
        if result.get("active") is not True or "workflow_job" not in (result.get("events") or []):
            # Some Forgejo versions silently discard unsupported event names.
            # Disable only our own hook rather than reporting a working feed.
            call("PATCH", hook_path + "/" + str(result["id"]), {"active": False})
            raise ValueError("The forge did not enable workflow_job deliveries; this dashboard's hook was disabled")
        return {"provider": provider, "hook_id": result["id"], "url": url, "active": True}


def verify(provider, raw, signature, data_directory):
    if provider not in ("github", "forgejo") or len(raw) > MAX_BODY:
        return False
    try:
        secret = (Path(data_directory) / ("job-webhook-" + provider + ".key")).read_bytes()
    except OSError:
        return False
    if len(secret) < 32 or not isinstance(signature, str):
        return False
    if not re.fullmatch((r"sha256=" if provider == "github" else "") + r"[0-9a-f]{64}", signature):
        return False
    digest = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest if provider == "github" else digest
    return hmac.compare_digest(signature, expected)


def accept(provider, delivery, payload, database):
    if provider not in ("github", "forgejo"):
        raise ValueError("Unknown forge")
    if not isinstance(payload, dict) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", delivery or ""):
        raise ValueError("Invalid webhook delivery")
    action = payload.get("action")
    if action not in ("in_progress", "completed"):
        return {"ignored": True}
    job = payload.get("workflow_job")
    repo = payload.get("repository")
    if not isinstance(job, dict) or not isinstance(repo, dict):
        raise ValueError("A workflow job and repository are required")
    runner = job.get("runner_id")
    if type(runner) is not int or runner <= 0:
        return {"ignored": True}
    with schema.connect(database) as connection:
        rows = connection.execute("SELECT runner_id,host_id,display_name FROM runner_specs "
                                  "WHERE provider=? AND registration_id=? AND deleted_at IS NULL",
                                  (provider, str(runner))).fetchall()
    if not rows:
        return {"ignored": True}
    if len(rows) != 1 or not rows[0]["host_id"]:
        raise ValueError("Runner ownership is ambiguous")
    row = rows[0]
    name = job.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Job name is required")
    if not isinstance(repo.get("full_name"), str) or not re.fullmatch(r"[^/\s]{1,80}/[^/\s]{1,80}", repo["full_name"]):
        raise ValueError("Repository name is required")
    if not isinstance(job.get("head_sha"), str) or not re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", job["head_sha"]):
        raise ValueError("Commit identity is required")
    if any(type(job.get(key)) is not int or job[key] <= 0 for key in ("id", "run_id")):
        raise ValueError("Job and run identities are required")
    result = job.get("conclusion") if action == "completed" else None
    if action == "completed" and (not isinstance(result, str) or result not in RESULTS):
        raise ValueError("Completed delivery needs an actual job conclusion")
    event = {"kind": "job", "event_id": provider + ":" + delivery,
             "runner_id": row["runner_id"], "state": "finished" if action == "completed" else "started",
             "job": name[:200], "repo": repo.get("full_name"), "commit": job.get("head_sha"),
             "run_id": str(job.get("run_id", "")), "run_attempt": str(job.get("run_attempt", 1)),
             "job_id": str(job.get("id", "")), "result": result,
             "at": job.get("completed_at") if action == "completed" else job.get("started_at")}
    return JobEvents(database).apply(row["host_id"], event, source="forge")
