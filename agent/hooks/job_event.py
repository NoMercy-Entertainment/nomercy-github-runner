"""GitHub hook observation. Results come only from the signed forge webhook."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid


def emit(stage, env=None):
    env = os.environ if env is None else env
    rid, directory = env.get("NOMERCY_RUNNER_ID", ""), env.get("NOMERCY_JOB_EVENT_DIR", "")
    if not rid or not directory:
        return False
    if stage not in ("started", "finished") or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid):
        raise ValueError("invalid owned event identity")
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError("event spool must be absolute")
    directory.mkdir(mode=0o700, exist_ok=True)
    for component in reversed((directory, *directory.parents)):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("unsafe event spool")
    with os.scandir(directory) as files:
        if sum(1 for _ in zip(range(256), files)) >= 256:
            raise ValueError("event spool is full")
    def field(key, maximum=150):
        value = env.get(key)
        return "".join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127)[:maximum] if value else None
    identity = uuid.uuid4().hex
    context = {"run_id": field("GITHUB_RUN_ID"), "run_attempt": field("GITHUB_RUN_ATTEMPT"), "job": field("GITHUB_JOB", 200)}
    active = directory / ".active-job"
    if stage == "started":
        context["job_id"] = "hook:" + uuid.uuid4().hex
        active_fd, active_temporary = tempfile.mkstemp(prefix=".active-", dir=directory)
        try:
            with os.fdopen(active_fd, "w", encoding="utf-8") as output:
                json.dump(context, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(active_temporary, active)
        finally:
            try:
                os.unlink(active_temporary)
            except FileNotFoundError:
                pass
    else:
        job_id = None
        try:
            active_fd = os.open(active, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(active_fd, "rb") as input:
                info = os.fstat(input.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                    raise ValueError("unsafe local hook identity")
                previous = json.loads(input.read(4097))
            if all(previous.get(key) == value for key, value in context.items()) and re.fullmatch(
                    r"hook:[0-9a-f]{32}", previous.get("job_id") or ""):
                job_id = previous["job_id"]
        except (OSError, ValueError, TypeError):
            pass
        context["job_id"] = job_id
    event = {"event_id": "github:" + identity, "runner_id": rid, "state": stage,
             "job": field("GITHUB_JOB", 200), "repo": field("GITHUB_REPOSITORY"),
             "commit": field("GITHUB_SHA"), "run_id": field("GITHUB_RUN_ID"),
             "run_attempt": field("GITHUB_RUN_ATTEMPT"), "job_id": context["job_id"],
             "at": datetime.now(timezone.utc).isoformat(), "result": None}
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > 4096:
        raise ValueError("event exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".event-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / (identity + ".json"))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


if __name__ == "__main__":
    try:
        emit(sys.argv[1] if len(sys.argv) == 2 else "")
    except Exception:
        print("::warning title=Runner events::local job observation could not be stored")
