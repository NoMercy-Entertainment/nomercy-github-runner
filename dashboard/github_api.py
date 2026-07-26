"""Enrich recorded runs with context the runner logs cannot know.

A runner log says "Running job: jvm-android". It does not say which repo, which
workflow, which branch or commit, or who pushed it - so two failures of the
same job look identical. GitHub's API knows all of that.

Matching strategy: the jobs endpoint reports `runner_name`, which is the
runner's registration (nomercy-xxxxx). So we walk recently-pushed org repos,
list workflow runs near the job's time window, and look for a job whose
runner_name and name match what we recorded.

Everything here is best-effort. A run that cannot be matched keeps its log-only
data and is marked checked so it is not retried forever.
"""

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHub:
    def __init__(self, token, org):
        self.token = token
        self.org = org
        self._repo_cache = None
        self._repo_cache_at = None

    # ----------------------------------------------------------------- http
    def _get(self, path, params=None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nomercy-runner-dashboard",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 404 on a repo without Actions is normal, not worth logging.
            if e.code not in (404,):
                print(f"[github] {e.code} {path}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[github] {path}: {e}")
            return None

    # ---------------------------------------------------------------- repos
    def repos(self, max_age_days=45):
        """Org repos pushed recently, newest first.

        Cached for 10 minutes: this is called per enrichment sweep and the set
        of active repos changes on the order of days, not seconds.
        """
        now = dt.datetime.utcnow()
        if self._repo_cache and self._repo_cache_at and \
                (now - self._repo_cache_at).total_seconds() < 600:
            return self._repo_cache

        data = self._get(f"/orgs/{self.org}/repos",
                         {"sort": "pushed", "direction": "desc",
                          "per_page": 40})
        if data is None:
            return self._repo_cache or []

        cutoff = now - dt.timedelta(days=max_age_days)
        out = []
        for r in data:
            pushed = r.get("pushed_at")
            if not pushed:
                continue
            try:
                if dt.datetime.strptime(pushed, "%Y-%m-%dT%H:%M:%SZ") < cutoff:
                    continue
            except Exception:
                pass
            out.append(r["full_name"])
        self._repo_cache = out
        self._repo_cache_at = now
        return out

    # ----------------------------------------------------------------- jobs
    def find_job(self, registration, job_name, started_at, ended_at):
        """Locate the workflow job that ran on `registration` at that time."""
        if not registration or registration == "-":
            return None

        try:
            t0 = dt.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
        # Generous window: the runner clock and GitHub's can differ, and a job
        # is queued before it starts.
        lo = (t0 - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for repo in self.repos():
            runs = self._get(f"/repos/{repo}/actions/runs",
                             {"per_page": 20, "created": f">={lo[:10]}"})
            if not runs or not runs.get("workflow_runs"):
                continue

            for wr in runs["workflow_runs"]:
                try:
                    created = dt.datetime.strptime(
                        wr["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    continue
                # Skip runs that cannot overlap this job at all.
                if abs((created - t0).total_seconds()) > 6 * 3600:
                    continue

                jobs = self._get(f"/repos/{repo}/actions/runs/{wr['id']}/jobs",
                                 {"per_page": 50})
                if not jobs:
                    continue
                for j in jobs.get("jobs", []):
                    if j.get("runner_name") != registration:
                        continue
                    if job_name and j.get("name") != job_name:
                        continue
                    return {
                        "run_id": wr["id"],
                        "repo": repo,
                        "workflow": wr.get("name") or wr.get("display_title"),
                        "branch": wr.get("head_branch"),
                        "sha": (wr.get("head_sha") or "")[:8],
                        "actor": (wr.get("actor") or {}).get("login"),
                        "url": j.get("html_url") or wr.get("html_url"),
                        "conclusion": j.get("conclusion"),
                    }
        return None
