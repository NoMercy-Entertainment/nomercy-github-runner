"""Forgejo's account of its own runners and the jobs they ran.

Forgejo answers two questions the runner's log cannot. Whether a runner is
busy: `status` on the admin runners endpoint is enumerated offline/idle/active,
which beats inferring it from a log line. And how a job ended: the
forgejo-runner daemon logs a task starting and never logs it finishing, so
without this the history would have no end times and no results at all.

Everything is best-effort and returns None on failure. A run that cannot be
matched keeps its log-only data rather than being dropped.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Terminal task states. Anything else - running, waiting, blocked - means the
# task has not finished, and reporting an end time for it would be a lie.
FINISHED = {"success", "failure", "cancelled", "skipped"}


class Forgejo:
    def __init__(self, base_url, token):
        self.base = (base_url or "").rstrip("/")
        self.token = token

    # ----------------------------------------------------------------- http
    def _request(self, path, method="GET", params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers={
            # Forgejo's own scheme. Not "Bearer": that is GitHub's, and
            # Forgejo answers 401 to it on some deployments.
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
            "User-Agent": "nomercy-runner-dashboard",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode()
                return json.loads(body) if body.strip() else True
        except urllib.error.HTTPError as e:
            print(f"[forgejo] {e.code} {method} {path}")
            return None
        except Exception as e:  # noqa: BLE001 - surfaced, not raised
            print(f"[forgejo] {method} {path}: {e}")
            return None

    def _get(self, path, params=None):
        return self._request(path, "GET", params)

    # -------------------------------------------------------------- runners
    def runner_statuses(self):
        """{uuid: status}, or None if the call failed.

        None and {} are deliberately different. {} means Forgejo answered and
        knows of no runners; None means we could not ask, and the caller must
        report "unknown" rather than treating every runner as idle - prune and
        drain act on that answer.

        The endpoint returns a BARE ARRAY, not an object with a "runners" key.
        """
        data = self._get("/api/v1/admin/actions/runners",
                         {"limit": 100})
        if not isinstance(data, list):
            return None
        return {r["uuid"]: (r.get("status") or "")
                for r in data if isinstance(r, dict) and r.get("uuid")}

    def runner_ids(self):
        """{uuid: id}, for deregistration. None if the call failed."""
        data = self._get("/api/v1/admin/actions/runners", {"limit": 100})
        if not isinstance(data, list):
            return None
        return {r["uuid"]: r.get("id")
                for r in data if isinstance(r, dict) and r.get("uuid")}

    def registration_token(self):
        data = self._get("/api/v1/admin/actions/runners/registration-token")
        if not isinstance(data, dict):
            return None
        return data.get("token") or None

    def delete_runner(self, runner_id):
        if not runner_id:
            return False
        got = self._request(
            f"/api/v1/admin/actions/runners/{runner_id}", "DELETE")
        return got is not None

    # ---------------------------------------------------------------- tasks
    def find_task(self, repo, task_id, started_at):
        """The finished task a recorded run corresponds to, or None.

        Matched on id first. Whether ActionTask.id is the same number the
        runner logs as "task 830" is the one thing this design could not
        settle from the swagger alone, so the start timestamp - which the log
        gives exactly - is a second, independent way in.

        An unfinished task returns None rather than a row with no end: it will
        be picked up on a later sweep, when it has actually finished.
        """
        data = self._get(f"/api/v1/repos/{repo}/actions/tasks",
                         {"limit": 50})
        # The tasks live under "workflow_runs". The name is Forgejo's, not a
        # mistake here - reading "tasks" gets an empty list and no error.
        tasks = (data or {}).get("workflow_runs") or []

        match = None
        for t in tasks:
            if task_id and t.get("id") == task_id:
                match = t
                break
        if match is None:
            for t in tasks:
                if started_at and t.get("run_started_at") == started_at:
                    match = t
                    break
        if match is None:
            return None

        status = (match.get("status") or "").lower()
        if status not in FINISHED:
            return None

        return {
            "run_id": match.get("id"),
            "repo": repo,
            "workflow": match.get("workflow_id"),
            "branch": match.get("head_branch"),
            "sha": (match.get("head_sha") or "")[:8],
            # Forgejo's task payload carries no actor. Left explicit rather
            # than omitted so the column is written as NULL instead of
            # keeping a stale value from an earlier partial enrichment.
            "actor": None,
            "url": match.get("url"),
            "conclusion": status,
            "ended_at": match.get("updated_at"),
        }
