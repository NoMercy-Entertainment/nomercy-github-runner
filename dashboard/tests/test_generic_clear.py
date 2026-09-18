"""T-1602: one clear-cache operation, per-runtime adapters, and FR-17's six
properties each asserted.

FR-17: a cache clear (1) skips or drains an active runner, (2) measures
before and after, (3) reports the space freed, (4) reports partial failures,
(5) is idempotent, (6) never damages another runner. Each has a test below,
driven through the service, the reconciler and the provisioning flow - the
same path every runtime is reached by - against a runtime whose units each
hold a cache.
"""
import json

import pytest

from control.service import Refused
from store import storage
from tests.fake_runtime import CacheUnitRuntime
from tests.test_partial_failure import GH, passes, the_runner  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401

RUNTIME = "tests.fake_runtime:CacheUnitRuntime"


@pytest.fixture
def cached(world):
    """The world, with its GitHub Linux runners holding caches."""
    service = world[0]
    CacheUnitRuntime.reset_caches()
    service.runtimes = dict(service.runtimes)
    service.runtimes[("github", "linux")] = RUNTIME
    return world


def serving(world, n=1):
    service, flow, agent, forges, reconciler = world
    service.set_capacity(GH, n)
    passes(service, reconciler)
    specs = service.specs.list(fleet_id=GH)
    assert [s["actual_state"] for s in specs] == ["idle"] * n
    for s in specs:
        CacheUnitRuntime.caches[storage.unit_name(s["runner_id"])] = {
            "engine-build-cache": 5000, "workspace": 700}
    return specs


def result(service, op):
    got = service.operations.get(op)
    return got["state"], json.loads(got["result"] or "null")


def clear(world, spec, key=None):
    service, flow, agent, forges, reconciler = world
    op = service.clear_cache(spec["runner_id"], idempotency_key=key)
    passes(service, reconciler, 3)
    return op


class TestOneOperation:
    def test_2_and_3_it_measures_and_reports_each_scope(self, cached):
        service = cached[0]
        (spec,) = serving(cached)
        state, freed = result(service, clear(cached, spec))
        assert state == "succeeded"
        assert freed["per_scope"] == {"engine-build-cache": 5000,
                                      "workspace": 700}
        assert freed["total_bytes"] == 5700
        assert freed["before"] == {"engine-build-cache": 5000,
                                   "workspace": 700}
        assert freed["after"] == {"engine-build-cache": 0, "workspace": 0}
        assert freed["measured"] is True and freed["partial"] is False

    def test_4_a_failing_scope_is_reported_and_does_not_hide_the_rest(
            self, cached):
        service = cached[0]
        (spec,) = serving(cached)
        CacheUnitRuntime.fail_scopes = {"workspace"}
        state, freed = result(service, clear(cached, spec))
        assert state == "succeeded", "the part that worked still counts"
        assert freed["per_scope"] == {"engine-build-cache": 5000}
        assert "workspace" in freed["errors"]
        assert freed["partial"] is True

    def test_5_a_second_clear_frees_nothing_and_succeeds(self, cached):
        service = cached[0]
        (spec,) = serving(cached)
        clear(cached, spec)
        state, freed = result(service, clear(cached, spec))
        assert state == "succeeded"
        assert freed["total_bytes"] == 0 and freed["errors"] == {}

    def test_5_a_repeated_request_is_the_same_clear(self, cached):
        service = cached[0]
        (spec,) = serving(cached)
        first = clear(cached, spec, key="once")
        again = service.clear_cache(spec["runner_id"], idempotency_key="once")
        assert first == again
        assert len(CacheUnitRuntime.cleared) == 1

    def test_unmeasured_is_reported_as_unknown_not_zero(self, cached):
        service = cached[0]
        (spec,) = serving(cached)
        CacheUnitRuntime.measure_fails = True
        state, freed = result(service, clear(cached, spec))
        assert freed["measured"] is False
        assert freed["total_bytes"] is None


class TestActiveRunners:
    def test_1_a_busy_runner_is_skipped_with_the_reason(self, cached):
        service, flow, agent, forges, reconciler = cached
        (spec,) = serving(cached)
        forges.busy.add(spec["registration_id"])
        passes(service, reconciler, 1)
        with pytest.raises(Refused, match="busy"):
            service.clear_cache(spec["runner_id"])
        assert CacheUnitRuntime.cleared == []

    def test_1_or_drained_first_when_its_policy_says_so(self, cached):
        """Drained, cleared once its job has finished - never under it -
        and then serving again."""
        service, flow, agent, forges, reconciler = cached
        (spec,) = serving(cached)
        service.specs.update(spec["runner_id"],
                             service.specs.get(spec["runner_id"])[
                                 "spec_version"],
                             cache_policy={"on_clear": "drain-first"})
        forges.busy.add(spec["registration_id"])
        passes(service, reconciler, 1)
        assert the_runner(service)["actual_state"] == "busy"

        op = service.clear_cache(spec["runner_id"])
        passes(service, reconciler, 3)
        assert the_runner(service)["actual_state"] == "draining"
        assert CacheUnitRuntime.cleared == [], "not while the job runs"

        forges.busy.discard(spec["registration_id"])     # the job finishes
        passes(service, reconciler)
        assert CacheUnitRuntime.cleared == [storage.unit_name(
            spec["runner_id"])]
        assert result(service, op)[0] == "succeeded"
        assert the_runner(service)["actual_state"] == "idle"


class TestOtherRunners:
    def test_6_it_never_touches_another_runner(self, cached):
        service = cached[0]
        mine, theirs = serving(cached, 2)
        before = dict(CacheUnitRuntime.caches[storage.unit_name(
            theirs["runner_id"])])
        clear(cached, mine)
        assert CacheUnitRuntime.cleared == [storage.unit_name(
            mine["runner_id"])]
        assert CacheUnitRuntime.caches[storage.unit_name(
            theirs["runner_id"])] == before
