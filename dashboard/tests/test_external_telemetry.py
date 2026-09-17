"""Telemetry for runners this engine cannot see must never stall or lie.

The exporter on BEAST-UNIT is reached over HTTP from the collector sweep, and
that sweep also serves the GitHub fleet. _forge_records() already had to learn
this the hard way: an unreachable endpoint whose failure was cached with the
success TTL, and whose deadline was taken BEFORE the call, retried on every
sweep and paid its full timeout each time. This follows the same rules, so a
dead exporter costs one timeout per backoff window and nothing else.

"Could not ask" is None. It is never zeros, and it is never the previous
sweep's numbers - a stale reading on a status page is worse than a blank one,
because the operator cannot tell it is stale.
"""
import json

import external_telemetry as et


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _reset(monkeypatch, clock):
    monkeypatch.setattr(et.time, "monotonic", clock)
    et.reset_cache()


def test_no_url_configured_is_none_and_costs_no_call(monkeypatch):
    """Absent configuration must disable the feature, not half-enable it."""
    calls = []
    monkeypatch.setattr(et, "_fetch", lambda url: calls.append(url))
    assert et.telemetry({}) is None
    assert calls == []


def test_a_good_answer_is_returned_and_reused(monkeypatch):
    clock = _Clock()
    _reset(monkeypatch, clock)
    calls = []

    def fake(url):
        calls.append(url)
        return {"runners": {"a": {"cpu_percent": 1.0}}}

    monkeypatch.setattr(et, "_fetch", fake)
    env = {"EXTERNAL_EXPORTER_URL": "http://host:9101/metrics"}
    assert et.telemetry(env)["a"]["cpu_percent"] == 1.0
    clock.t += 1
    assert et.telemetry(env)["a"]["cpu_percent"] == 1.0
    assert len(calls) == 1, "second call inside the TTL must reuse the answer"


def test_a_failure_is_not_replayed_as_a_stale_answer(monkeypatch):
    """The hazard this cache exists to avoid."""
    clock = _Clock()
    _reset(monkeypatch, clock)
    answers = [{"runners": {"a": {"cpu_percent": 1.0}}}, None]
    monkeypatch.setattr(et, "_fetch", lambda url: answers.pop(0))
    env = {"EXTERNAL_EXPORTER_URL": "http://host:9101/metrics"}

    assert et.telemetry(env)["a"]["cpu_percent"] == 1.0
    clock.t += et.TTL + 1
    assert et.telemetry(env) is None, "a failed fetch must not replay the last good one"


def test_a_failure_backs_off_further_than_a_success(monkeypatch):
    """Otherwise an unreachable exporter is retried on every sweep."""
    clock = _Clock()
    _reset(monkeypatch, clock)
    calls = []
    monkeypatch.setattr(et, "_fetch", lambda url: calls.append(url))
    env = {"EXTERNAL_EXPORTER_URL": "http://host:9101/metrics"}

    et.telemetry(env)
    clock.t += et.TTL + 1
    et.telemetry(env)
    assert len(calls) == 1, "still inside the failure backoff"
    clock.t += et.FAIL_BACKOFF
    et.telemetry(env)
    assert len(calls) == 2


def test_the_deadline_is_taken_after_the_call_not_before(monkeypatch):
    """A call slower than the TTL used to store a deadline already in the past."""
    clock = _Clock()
    _reset(monkeypatch, clock)
    calls = []

    def slow(url):
        calls.append(url)
        clock.t += et.TTL * 3          # the call itself outlasts the window
        return {"runners": {}}

    monkeypatch.setattr(et, "_fetch", slow)
    env = {"EXTERNAL_EXPORTER_URL": "http://host:9101/metrics"}
    et.telemetry(env)
    et.telemetry(env)
    assert len(calls) == 1, "deadline must start when the call finished"


def test_malformed_payloads_are_none_not_a_crash(monkeypatch):
    clock = _Clock()
    for payload in ("not json", {"no_runners_key": 1}, {"runners": "nope"}, None):
        _reset(monkeypatch, clock)
        monkeypatch.setattr(et, "_fetch", lambda url, p=payload: p)
        env = {"EXTERNAL_EXPORTER_URL": "http://host:9101/metrics"}
        assert et.telemetry(env) is None
        clock.t += et.FAIL_BACKOFF + 1


def test_fetch_never_raises_through_to_the_sweep(monkeypatch):
    """Any transport error is an answer of None, not an exception upward."""
    def boom(url, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(et.urllib.request, "urlopen", boom)
    assert et._fetch("http://host:9101/metrics") is None


class TestElsewhereMerge:
    """Merging must be additive: no exporter leaves today's cards untouched."""

    RECORDS = [{"uuid": "u1", "name": "beaststack-windows-runner",
                "status": "idle", "labels": ["windows-2022"], "version": "dev"}]

    def test_without_telemetry_the_payload_is_what_it_was(self):
        """The feature must be invisible when it is not configured."""
        import docker_ops

        got = docker_ops._elsewhere(self.RECORDS, set(), None)
        assert got[0]["name"] == "beaststack-windows-runner"
        assert got[0]["telemetry"] is None

    def test_telemetry_is_matched_by_name(self):
        import docker_ops

        tel = {"beaststack-windows-runner": {"cpu_percent": 12.5}}
        got = docker_ops._elsewhere(self.RECORDS, set(), tel)
        assert got[0]["telemetry"]["cpu_percent"] == 12.5

    def test_a_runner_the_exporter_does_not_know_stays_none(self):
        """An exporter that answers about other machines must not fill this in."""
        import docker_ops

        got = docker_ops._elsewhere(self.RECORDS, set(), {"someone-else": {}})
        assert got[0]["telemetry"] is None

    def test_a_probe_that_failed_stays_none(self):
        """None from the exporter means that probe could not answer."""
        import docker_ops

        got = docker_ops._elsewhere(
            self.RECORDS, set(), {"beaststack-windows-runner": None})
        assert got[0]["telemetry"] is None

    def test_no_forge_records_is_still_an_empty_list(self):
        """Unchanged behaviour: the forge being unreachable wins over telemetry."""
        import docker_ops

        assert docker_ops._elsewhere(None, set(), {"x": {"cpu_percent": 1}}) == []


class TestJobIsGatedOnForgeStatus:
    """A task line outlives its task, so the log alone cannot say "busy".

    The forgejo-runner daemon logs a task starting and never logs it
    finishing. docker_ops._forgejo_job_state() already handles this by taking
    busy/idle from the forge and only the NAME from the log; the Elsewhere
    card showed a day-old job beside an IDLE badge until it did the same.
    """

    def _else_render(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "templates", "index.html"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_the_job_name_is_shown_only_when_the_forge_says_active(self):
        html = self._else_render()
        assert "const eactive = r.status === 'active';" in html
        assert "eactive && t.job ? t.job : 'no active job'" in html

    def test_the_job_style_uses_the_class_the_stylesheet_dims(self):
        """The stylesheet dims .cjob.none. ' idle' matched no rule at all, so
        an idle Elsewhere card kept the bright busy colour and accent bar
        while its text read "no active job" - the regular cards have always
        used ' none' (see the makeCard render path)."""
        html = self._else_render()
        assert "'cjob' + (t && eactive && t.job ? '' : ' none')" in html
        assert ".cjob.none{color:var(--text-faint)" in html,             "the class asserted above must be one the stylesheet actually styles"


class TestTimeoutMargins:
    """The exporter was measured at 3.2-3.9s before it sampled in the
    background. A 5s timeout left roughly a second of headroom, and a failure
    is cached for longer than a success, so one slow sweep blanked both cards
    for fifteen seconds."""

    def test_the_timeout_clears_the_measured_worst_case(self):
        assert et.TIMEOUT >= 8, "less than this and a slow sweep blanks the cards"

    def test_a_failure_still_costs_the_sweep_less_than_one_poll_each_time(self):
        """The collector runs every 5s and also carries the GitHub fleet, so a
        hung exporter must not be retried on every one of them."""
        assert et.FAIL_BACKOFF > et.TIMEOUT
        assert et.FAIL_BACKOFF >= 15
