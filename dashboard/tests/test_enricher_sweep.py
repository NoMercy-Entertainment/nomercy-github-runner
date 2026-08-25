"""_enrich_pending(): one sweep of the enricher loop.

Split out of app._enricher() (which is only a `while True: ... time.sleep(90)`
wrapper around it) specifically so these two invariants can be exercised
directly instead of through an infinite loop:

  - Forgejo closes a run before it enriches it, so a crash between the two
    leaves a *closed* run waiting to be enriched next sweep, not an *enriched*
    run that never ends.
  - A Forgejo run that does not match is retried while it is young and given
    up on only once it is older than the staleness cutoff - the common case
    on a miss is "not finished yet", not "will never finish".

Each test below exists to catch one specific way of breaking these that would
otherwise pass unnoticed, because test_forgejo_history.py only exercises the
parser and none of the pre-existing tests touch the enricher at all.
"""
import history
import providers
import app as dash


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "h.db"))
    history.init()


class FakeProvider:
    """Stands in for a providers.Provider: forge_client() hands back whatever
    fake client the test wired up, ignoring env entirely."""

    def __init__(self, client):
        self._client = client

    def forge_client(self, env):
        return self._client


class FakeForgejoClient:
    def __init__(self, result):
        self._result = result

    def find_task(self, repo, task_id, started_at):
        return self._result


class FakeGithubClient:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    def find_job(self, registration, job_name, started_at, ended_at):
        self.calls += 1
        return self._result


def _wire(monkeypatch, **clients):
    """providers.by_key(key) -> a FakeProvider wrapping clients[key]."""
    by_key = {k: FakeProvider(c) for k, c in clients.items()}
    monkeypatch.setattr(providers, "by_key", lambda k: by_key.get(k))


def test_forgejo_close_happens_before_enrich(tmp_path, monkeypatch):
    """If apply_enrichment ever ran before apply_close, a crash between the
    two calls would leave a run that is enriched but never ends - exactly
    the failure the ordering exists to prevent. Both having happened is not
    enough; the order has to be asserted."""
    _fresh_db(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:00:00Z",
                     provider="forgejo", forge_task_id=830)
    run_id = history.list_runs()[0]["id"]

    order = []
    real_close, real_enrich = history.apply_close, history.apply_enrichment

    def spy_close(*a, **kw):
        order.append("close")
        return real_close(*a, **kw)

    def spy_enrich(*a, **kw):
        order.append("enrich")
        return real_enrich(*a, **kw)

    monkeypatch.setattr(history, "apply_close", spy_close)
    monkeypatch.setattr(history, "apply_enrichment", spy_enrich)

    found = {"run_id": 830, "repo": "FiLL/q", "workflow": "ci",
             "branch": "main", "sha": "abc", "actor": "fill",
             "url": "http://x", "conclusion": "success",
             "ended_at": "2026-08-25T14:05:00Z"}
    _wire(monkeypatch, forgejo=FakeForgejoClient(found))

    dash._enrich_pending({}, "2020-01-01T00:00:00Z")

    assert order == ["close", "enrich"]
    row = history.get_run(run_id)
    assert row["ended_at"] == "2026-08-25T14:05:00Z"
    assert row["gh_conclusion"] == "success"
    assert row["gh_checked"] == 1


def test_forgejo_match_with_no_end_is_left_pending_not_enriched(tmp_path, monkeypatch):
    """A "finished" task whose ended_at is empty must not be enriched
    without being closed - that is the enriched-but-never-closes failure the
    close-before-enrich ordering exists to prevent, reached by a different
    route (forgejo_api reads ended_at from Forgejo's updated_at, which can be
    missing). Leaving the run pending lets a later sweep, or eventually the
    staleness cutoff, resolve it instead of enriching it prematurely."""
    _fresh_db(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:00:00Z",
                     provider="forgejo", forge_task_id=830)
    found = {"run_id": 830, "conclusion": "success", "ended_at": None}
    _wire(monkeypatch, forgejo=FakeForgejoClient(found))

    dash._enrich_pending({}, "2020-01-01T00:00:00Z")

    row = history.list_runs()[0]
    assert row["ended_at"] is None
    assert row["gh_checked"] == 0


def test_recent_forgejo_miss_is_not_given_up_on(tmp_path, monkeypatch):
    """A task that simply has not finished yet also looks like a miss to
    find_task. Marking it unmatched here would close off the only route to
    its end time for good - it must still be open and still pending so the
    next sweep tries again."""
    _fresh_db(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-25T14:00:00Z",
                     provider="forgejo", forge_task_id=830)
    _wire(monkeypatch, forgejo=FakeForgejoClient(None))

    # Cutoff well before the run's start: nowhere near stale.
    dash._enrich_pending({}, "2020-01-01T00:00:00Z")

    row = history.list_runs()[0]
    assert row["ended_at"] is None
    assert row["gh_checked"] == 0
    assert len(history.pending_enrichment()) == 1


def test_stale_forgejo_miss_is_closed_and_given_up_on(tmp_path, monkeypatch):
    """Invert the staleness comparison and either every run is abandoned on
    its first sweep, or none ever are - and every other test in this file
    would still pass. Only this test would catch it."""
    _fresh_db(tmp_path, monkeypatch)
    history.open_run("forgejo-runner-1", "r", "FiLL/q",
                     "2026-08-24T00:00:00Z",
                     provider="forgejo", forge_task_id=830)
    _wire(monkeypatch, forgejo=FakeForgejoClient(None))

    # Cutoff after the run's start: the run is stale.
    dash._enrich_pending({}, "2026-08-25T00:00:00Z")

    row = history.list_runs()[0]
    assert row["ended_at"] == "2026-08-24T00:00:00Z"
    assert row["result"] == "unknown"
    assert row["gh_checked"] == 1
    assert history.pending_enrichment() == []


def test_github_path_uses_find_job_and_gives_up_on_first_miss(tmp_path, monkeypatch):
    """The GitHub rule is the opposite of Forgejo's, on purpose: a GitHub run
    only becomes pending once it has already ended (find_job matches inside a
    known start/end window), so there is no "not finished yet" case to
    protect against - a miss really is a miss. If the two branches were ever
    "simplified" into one, this is what would start silently retrying dead
    GitHub runs forever instead of giving up on them."""
    _fresh_db(tmp_path, monkeypatch)
    history.open_run("github-runner-1", "nomercy-x", "build",
                     "2026-08-25T10:00:00Z")
    run_id = history.list_runs()[0]["id"]
    history.close_run("github-runner-1", "build",
                      "2026-08-25T10:05:00Z", "Succeeded")

    client = FakeGithubClient(None)
    _wire(monkeypatch, github=client)

    dash._enrich_pending({}, "2020-01-01T00:00:00Z")

    assert client.calls == 1
    row = history.get_run(run_id)
    assert row["gh_checked"] == 1
