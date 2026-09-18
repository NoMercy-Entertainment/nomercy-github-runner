"""Deadlines and bounded retries, and the rule that makes them unavoidable.

The table in design 17.2 is parsed out of the spec and compared with the
policies, row for row, so neither can change alone.

The definition of done is the last class: no remote call in dashboard/control/
lacks a timeout. It is checked by reading the package's syntax trees rather
than by testing calls one at a time, because the failure it prevents is the
one call someone adds next month without a deadline - which no test of today's
calls would notice.
"""
import ast
import os
import re
import time

import pytest

from control import retry
from control.retry import (AGENT_FAST, CachedForges, DeadlineExceeded,
                           GaveUp, Policy)

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "docs",
                    "superpowers", "specs",
                    "2026-09-17-uniform-hyperv-runner-platform-design.md")
CONTROL = os.path.join(os.path.dirname(HERE), "control")


def table_rows():
    """The rows of the 17.2 table, as (call, timeout, retries, backoff)."""
    with open(SPEC, encoding="utf-8") as fh:
        text = fh.read()
    section = text.split("### 17.2 Timeouts and retries", 1)[1]
    section = section.split("### 17.3", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("| ") or line.startswith("| Call") \
                or set(line) <= {"|", "-", " "}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        call = re.sub(r"\s*\(.*?\)\s*$", "", cells[0]).strip()
        timeout = float(re.search(r"(\d+)\s*s", cells[1]).group(1))
        retries = int(cells[2])
        backoff = tuple(float(x) for x in re.findall(r"(\d+)\s*s", cells[3])) \
            if retries else ()
        rows.append((call, timeout, retries, backoff))
    return rows


class TestTheTableIsTheSpec:
    def test_the_table_parsed(self):
        """An empty parse would make the comparisons below vacuous."""
        assert len(table_rows()) == 7

    def test_every_row_has_a_policy(self):
        assert {row[0] for row in table_rows()} == set(retry.POLICIES)

    @pytest.mark.parametrize("row", table_rows(), ids=lambda r: r[0])
    def test_each_row_matches_its_policy(self, row):
        call, timeout, retries, backoff = row
        policy = retry.POLICIES[call]
        assert policy.timeout == timeout
        assert policy.retries == retries
        assert policy.backoff == backoff

    def test_a_policy_cannot_be_unbounded(self):
        with pytest.raises(ValueError):
            Policy("forever", 0, 0)

    def test_a_policy_needs_one_pause_per_retry(self):
        with pytest.raises(ValueError):
            Policy("lopsided", 5, 2, (1,))


class TestASlowCallIsCutAtItsDeadline:
    def test_the_caller_is_released_on_time(self):
        """The outer guarantee: whatever the adapter does, the controller is
        not held past the deadline."""
        policy = Policy("quick", 0.2, 0)
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            retry.call(policy, time.sleep, 3)
        assert time.monotonic() - started < 1.5

    def test_the_error_names_the_policy_and_the_deadline(self):
        with pytest.raises(DeadlineExceeded) as caught:
            retry.call(Policy("quick", 0.1, 0), time.sleep, 2)
        assert "quick" in str(caught.value)
        assert "0.1s" in str(caught.value)

    def test_a_fast_call_is_unaffected(self):
        assert retry.call(Policy("quick", 1, 0), lambda: 42) == 42

    def test_arguments_reach_the_call(self):
        assert retry.call(Policy("quick", 1, 0),
                          lambda a, b=0: a + b, 2, b=3) == 5


class TestRetriesAreBounded:
    def flaky(self, failures):
        state = {"calls": 0}

        def fn():
            state["calls"] += 1
            if state["calls"] <= failures:
                raise ConnectionError(f"attempt {state['calls']}")
            return "ok"
        return fn, state

    def test_a_transient_failure_is_retried(self, no_real_backoff):
        fn, state = self.flaky(failures=2)
        assert retry.call(AGENT_FAST, fn) == "ok"
        assert state["calls"] == 3

    def test_the_pauses_are_the_designs(self, no_real_backoff):
        fn, _ = self.flaky(failures=2)
        retry.call(AGENT_FAST, fn)
        assert no_real_backoff == [1, 3]

    def test_it_stops_after_the_last_retry(self, no_real_backoff):
        fn, state = self.flaky(failures=10)
        with pytest.raises(GaveUp) as caught:
            retry.call(AGENT_FAST, fn)
        assert state["calls"] == 3
        assert caught.value.attempts == 3
        assert isinstance(caught.value.cause, ConnectionError)

    def test_giving_up_says_how_many_times_it_tried(self):
        """"Failed" and "failed three times" call for different responses."""
        fn, _ = self.flaky(failures=10)
        with pytest.raises(GaveUp, match="after 3 attempts"):
            retry.call(AGENT_FAST, fn)

    def test_no_retries_means_the_original_error(self):
        """A caller that handles a specific failure keeps handling it."""
        fn, state = self.flaky(failures=1)
        with pytest.raises(ConnectionError):
            retry.call(retry.AGENT_SLOW, fn)
        assert state["calls"] == 1

    def test_a_timeout_is_retried_like_any_failure(self, no_real_backoff):
        policy = Policy("quick", 0.1, 1, (0.5,))
        state = {"calls": 0}

        def slow_then_fast():
            state["calls"] += 1
            if state["calls"] == 1:
                time.sleep(1)
            return "second time"

        assert retry.call(policy, slow_then_fast) == "second time"


class TestAFailedStatusReadIsUnknownNeverStale:
    """Design 17.2's forge status rule."""

    class Forge:
        def __init__(self):
            self.answers = []
            self.calls = 0

        def records(self, provider):
            self.calls += 1
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        def delete(self, provider, registration_id):
            return True

    def clock(self, start=0.0):
        now = {"t": start}
        return now, (lambda: now["t"])

    def test_a_good_answer_is_served_from_the_cache(self):
        forge = self.Forge()
        forge.answers = [[{"id": 1}]]
        now, clock = self.clock()
        cached = CachedForges(forge, ttl=10, clock=clock)

        cached.records("github")
        now["t"] = 5
        assert cached.records("github") == [{"id": 1}]
        assert forge.calls == 1

    def test_after_the_ttl_it_asks_again(self):
        forge = self.Forge()
        forge.answers = [[{"id": 1}], [{"id": 2}]]
        now, clock = self.clock()
        cached = CachedForges(forge, ttl=10, clock=clock)
        cached.records("github")
        now["t"] = 11
        assert cached.records("github") == [{"id": 2}]

    def test_a_failure_replaces_the_good_answer_with_unknown(self):
        """The whole rule. Serving the stale list would report a runner idle
        after the forge had stopped being able to say so - and a cache clear
        acts on idle."""
        forge = self.Forge()
        forge.answers = [[{"id": 1, "busy": False}], ConnectionError("down")]
        now, clock = self.clock()
        cached = CachedForges(forge, ttl=10, clock=clock)

        assert cached.records("github") == [{"id": 1, "busy": False}]
        now["t"] = 11
        assert cached.records("github") is None

    def test_the_unknown_is_cached_too(self):
        """So a forge that is down is not hammered on every read."""
        forge = self.Forge()
        forge.answers = [ConnectionError("down")]
        now, clock = self.clock()
        cached = CachedForges(forge, ttl=10, clock=clock)
        cached.records("github")
        now["t"] = 5
        assert cached.records("github") is None
        assert forge.calls == 1

    def test_the_two_forges_are_cached_apart(self):
        forge = self.Forge()
        forge.answers = [[{"id": 1}], [{"uuid": "u"}]]
        cached = CachedForges(forge, ttl=10, clock=lambda: 0)
        cached.records("github")
        assert cached.records("forgejo") == [{"uuid": "u"}]

    def test_a_delete_is_never_answered_from_a_cache(self):
        forge = self.Forge()
        cached = CachedForges(forge, ttl=10, clock=lambda: 0)
        assert cached.delete("forgejo", "7") is True


class TestTheFlowObeysTheTable:
    """The provisioning flow, through its policies."""

    def test_a_transient_mint_failure_is_retried(self, tmp_path, monkeypatch,
                                                 no_real_backoff):
        import providers as P
        from control.provision import ProvisioningFlow
        from control.service import RunnerService
        from store import schema
        from store.fleets import FleetStore, fleet_id
        from tests.fake_platform import FakeAgent, FakeForges
        from tests.fake_runtime import UnitRuntime

        tries = {"n": 0}

        class Flaky:
            org = "o"

            def registration_token(self):
                tries["n"] += 1
                return None if tries["n"] == 1 else "tok-0123456789"

        monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: Flaky())
        UnitRuntime.reset()
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed()
        service = RunnerService(path, runtimes={
            ("github", "linux"): "tests.fake_runtime:UnitRuntime"})
        service.inventory.register_worker("w", "hyperv-linux")
        service.inventory.heartbeat("w")
        agent = FakeAgent()
        flow = ProvisioningFlow(service, agent, FakeForges(agent),
                                sleep=lambda s: None)
        spec = service.specs.get(service.planned_ids(
            service.plan(fleet_id("github", "linux", "x64"), 1))[0])
        result = flow.provision(spec)
        service.specs.update(spec["runner_id"], 1, actual_state="provisioned",
                             **result)

        flow.register(service.specs.get(spec["runner_id"]))

        assert tries["n"] == 2
        assert no_real_backoff == [2]


REMOTE_RECEIVERS = ("agent", "forges", "runtime")
REMOTE_METHODS = {"register", "deregister", "drain", "cancel_drain", "ready",
                  "records", "delete", "create", "remove", "start", "stop",
                  "status", "logs", "telemetry", "clear_cache",
                  "registration"}


def _bounded_local_functions(tree):
    """Nested functions that are handed to retry.call as the thing to run.

    A remote call inside one of these runs under the policy that bounds it, so
    it is not direct. Recognised only when the function's own name is the
    second argument of a `call(...)` in the same enclosing function - a
    nested helper that is merely defined, or called some other way, earns no
    allowance.
    """
    bounded = set()
    for outer in ast.walk(tree):
        if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = {n.name: n for n in outer.body
                 if isinstance(n, ast.FunctionDef)}
        for node in ast.walk(outer):
            if not (isinstance(node, ast.Call) and len(node.args) >= 2):
                continue
            if ast.unparse(node.func) not in ("call", "retry.call"):
                continue
            target = node.args[1]
            if isinstance(target, ast.Name) and target.id in local:
                bounded.add(id(local[target.id]))
    return bounded


def direct_remote_calls(path):
    """Calls to a remote collaborator that run without a deadline.

    A remote call is a method on an agent, a forge client or a runtime, or a
    provider's `registration` - which mints a token over the network. It is
    bounded when it is handed to `retry.call`, or when it sits inside a local
    function that is handed to `retry.call`. Anything else is what this finds.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bounded = _bounded_local_functions(tree)
    inside_bounded = set()
    for node in ast.walk(tree):
        if id(node) in bounded:
            inside_bounded.update(id(n) for n in ast.walk(node))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in inside_bounded:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in REMOTE_METHODS:
            continue
        receiver = ast.unparse(func.value)
        if func.attr == "registration" or any(
                word in receiver for word in REMOTE_RECEIVERS):
            found.append(f"{os.path.basename(path)}:{node.lineno} "
                         f"{receiver}.{func.attr}(...)")
    return found


class TestNoRemoteCallLacksATimeout:
    """T-0307's definition of done."""

    def test_every_remote_call_in_the_controller_goes_through_retry(self):
        offenders = []
        for name in sorted(os.listdir(CONTROL)):
            if name.endswith(".py"):
                offenders += direct_remote_calls(os.path.join(CONTROL, name))
        assert offenders == [], (
            "these call a remote collaborator directly, with no deadline: "
            f"{offenders}; hand the bound method to retry.call with a policy")

    def test_the_check_would_catch_one(self, tmp_path):
        """A check that cannot fail proves nothing."""
        bad = tmp_path / "bad.py"
        bad.write_text("def f(self):\n    self.agent.register(1, 2, 3)\n")
        assert direct_remote_calls(str(bad))

    def test_and_lets_the_right_form_through(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text("def f(self):\n"
                        "    retry.call(P, self.agent.register, 1, 2, 3)\n")
        assert direct_remote_calls(str(good)) == []

    def test_a_local_function_handed_to_retry_is_bounded(self, tmp_path):
        """How the token mint is written: it raises on a refused mint inside
        the retried call, so a transient refusal is retried."""
        good = tmp_path / "nested.py"
        good.write_text("def f(self, provider):\n"
                        "    def mint():\n"
                        "        return provider.registration(1, 2)\n"
                        "    return retry.call(P, mint)\n")
        assert direct_remote_calls(str(good)) == []

    def test_a_local_function_not_handed_to_retry_is_still_caught(
            self, tmp_path):
        """The allowance is earned by being passed to retry.call, not by being
        nested."""
        bad = tmp_path / "nested_bad.py"
        bad.write_text("def f(self, provider):\n"
                       "    def mint():\n"
                       "        return provider.registration(1, 2)\n"
                       "    return mint()\n")
        assert direct_remote_calls(str(bad))
