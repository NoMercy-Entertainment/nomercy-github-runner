"""T-1302: drain lets the job finish and takes no other; nothing destructive
reaches a runner that is working.

"Never abort a running job" (MIG-9) is written once, in `states.aborts_a_job`.
The service refuses a verb that would do it, with the reason; the reconciler
checks the same rule before every step, as a last lock behind `decide`. These
drive whole passes against the fakes and watch what the unit and the forge
were actually asked to do.
"""
import pytest

from control import reconciler as rec
from control import states
from control.service import Refused
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import GH, passes, the_runner  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401


def serving(world):
    service, flow, agent, forges, reconciler = world
    service.scale_up(GH)
    passes(service, reconciler)
    spec = the_runner(service)
    assert spec["actual_state"] == "idle"
    return spec


def working(world):
    """Serving, and the forge shows it running a job."""
    service, flow, agent, forges, reconciler = world
    spec = serving(world)
    forges.busy.add(spec["registration_id"])
    passes(service, reconciler, 1)
    spec = the_runner(service)
    assert spec["actual_state"] == "busy"
    return spec


def unit_calls():
    return [c[0] for c in UnitRuntime.log if c[0] in ("stop", "remove")]


class TestTheRuleIsWrittenOnce:
    @pytest.mark.parametrize("step", sorted(states.ENDS_WORK))
    @pytest.mark.parametrize("state", sorted(states.AT_WORK))
    def test_it_forbids_every_ending_at_work(self, step, state):
        assert states.aborts_a_job(step, state)

    def test_and_nothing_else(self):
        assert not states.aborts_a_job("drain", "busy")
        assert not states.aborts_a_job("stop", "idle")
        assert not states.aborts_a_job("remove", "drained")

    def test_the_reconciler_uses_it(self):
        assert rec.DESTRUCTIVE is states.ENDS_WORK


class TestTheServiceRefusesWithTheReason:
    @pytest.mark.parametrize("verb", ["stop", "remove"])
    def test_a_verb_that_would_abort_the_job(self, world, verb):
        service = world[0]
        spec = working(world)
        with pytest.raises(Refused, match="would abort the job"):
            service.act(spec["runner_id"], verb)
        assert the_runner(service)["current_operation"] is None

    def test_the_reason_says_what_to_do_instead(self, world):
        service = world[0]
        spec = working(world)
        with pytest.raises(Refused, match="drain it first"):
            service.act(spec["runner_id"], "stop")


class TestDrainWhileBusy:
    def test_the_job_finishes_and_nothing_is_stopped_meanwhile(self, world):
        service, flow, agent, forges, reconciler = world
        spec = working(world)
        before = len(unit_calls())
        service.drain(spec["runner_id"])
        passes(service, reconciler)
        assert the_runner(service)["actual_state"] == "draining"
        assert len(unit_calls()) == before, "nothing ended while it worked"

        forges.busy.discard(spec["registration_id"])     # the job finishes
        passes(service, reconciler)
        assert the_runner(service)["actual_state"] == "drained"

    def test_cancel_drain_takes_work_again(self, world):
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler)
        assert the_runner(service)["actual_state"] == "drained"
        service.cancel_drain(spec["runner_id"])
        passes(service, reconciler)
        assert the_runner(service)["actual_state"] == "idle"

    @pytest.mark.parametrize("verb", ["restart", "recreate"])
    def test_a_composite_is_not_asked_of_a_busy_runner(self, world, verb):
        service = world[0]
        spec = working(world)
        with pytest.raises(Refused, match="busy"):
            getattr(service, verb)(spec["runner_id"])

    @pytest.mark.parametrize("verb", ["restart"])
    def test_one_that_became_busy_after_it_was_asked_drains_first(
            self, world, verb):
        """Asked for while idle; a job arrived before the reconciler got to
        it. The unit is not touched until that job has finished. (Recreate
        cannot meet this: it starts only from drained or stopped, which take
        no jobs.)"""
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        getattr(service, verb)(spec["runner_id"])
        forges.busy.add(spec["registration_id"])
        with_observe = the_runner(service)
        service.specs.update(with_observe["runner_id"],
                             with_observe["spec_version"],
                             actual_state="busy")
        before = len(unit_calls())
        passes(service, reconciler)
        assert len(unit_calls()) == before
        assert the_runner(service)["actual_state"] == "draining"

    def test_a_scale_down_waits_for_the_job(self, world):
        service, flow, agent, forges, reconciler = world
        spec = working(world)
        service.scale_down(GH)
        passes(service, reconciler)
        assert the_runner(service)["actual_state"] == "draining"
        assert "remove" not in unit_calls()


class TestTheLastLock:
    def test_a_decision_to_end_a_busy_runner_is_held(self, world,
                                                     monkeypatch):
        """Whatever `decide` says, the reconciler does not carry out an
        ending on a runner at work."""
        service, flow, agent, forges, reconciler = world
        spec = working(world)
        monkeypatch.setattr(rec, "decide", lambda *a, **k: "stop")
        before = len(unit_calls())
        report = reconciler.pass_once()
        assert len(unit_calls()) == before
        assert any("would abort its job" in why for _, why in report.held)
