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
from tests.test_partial_failure import FJ, GH, passes, the_runner  # noqa: F401
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
    def test_a_composite_asked_of_a_busy_runner_drains_it_first(self, world,
                                                                verb):
        """Rebuilding the runner that is working is what an operator means,
        so it is accepted - and the job still finishes, because the first
        step taken is a drain and the unit is not touched before it ends.
        The rule is about the steps, not the word (2026-09-20)."""
        service, flow, agent, forges, reconciler = world
        spec = working(world)
        getattr(service, verb)(spec["runner_id"])
        before = len(unit_calls())
        passes(service, reconciler)
        assert len(unit_calls()) == before, "the unit was touched"
        assert the_runner(service)["actual_state"] == "draining"

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


# ---------------------------------------------------------------------------
# OPEN-7: where each forge's runner is drained, and what proves it drained
# ---------------------------------------------------------------------------

def serving_forgejo(world):
    service, flow, agent, forges, reconciler = world
    service.scale_up(FJ)
    passes(service, reconciler)
    spec = the_runner(service, FJ)
    assert spec["actual_state"] == "idle"
    return spec


def drain_calls(agent):
    return [c for c in agent.calls if c[0] == "drain"]


class TestEachForgeDrainsWhereItsRunnerLetsIt:
    def test_github_at_the_forge_and_never_on_the_worker(self, world):
        """Its runner cancels the job on SIGTERM; the worker is not asked."""
        service, flow, agent, forges, reconciler = world
        spec = working(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        assert forges.drained == {spec["registration_id"]}
        assert drain_calls(agent) == []

    def test_forgejo_on_its_worker_and_never_at_the_forge(self, world):
        """Its runner finishes the job on SIGTERM; its record is left be."""
        service, flow, agent, forges, reconciler = world
        spec = serving_forgejo(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        assert drain_calls(agent)
        assert forges.drained == set()
        assert the_runner(service, FJ)["actual_state"] == "drained"


class TestDrainedIsProvenNotAssumed:
    def test_an_idle_runner_whose_forge_cannot_be_read_stays_draining(
            self, world, monkeypatch):
        """Idle when asked is not drained: a job can arrive in between, and
        a forge that did not answer has said nothing."""
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        monkeypatch.setattr(forges, "records", lambda provider: None)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 3)
        assert the_runner(service)["actual_state"] == "draining"

        monkeypatch.undo()
        passes(service, reconciler, 2)
        assert the_runner(service)["actual_state"] == "drained"

    def test_a_runner_drained_on_its_worker_must_have_stopped(self, world):
        """The forge alone is not enough: offline is also what a network
        break in the middle of a job looks like."""
        service, flow, agent, forges, reconciler = world
        spec = serving_forgejo(world)
        agent.running_unknown = True
        service.drain(spec["runner_id"])
        passes(service, reconciler, 3)
        assert the_runner(service, FJ)["actual_state"] == "draining"

        agent.running_unknown = False
        passes(service, reconciler, 2)
        assert the_runner(service, FJ)["actual_state"] == "drained"

    def test_a_forgejo_runner_drains_when_its_job_is_done(self, world):
        service, flow, agent, forges, reconciler = world
        spec = serving_forgejo(world)
        forges.busy.add(spec["registration_id"])
        passes(service, reconciler, 1)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 3)
        assert the_runner(service, FJ)["actual_state"] == "draining"
        assert len(unit_calls()) == 0, "nothing ended while it worked"

        forges.busy.discard(spec["registration_id"])    # the job finishes
        passes(service, reconciler, 2)
        assert the_runner(service, FJ)["actual_state"] == "drained"

    def test_a_drain_the_forge_did_not_confirm_is_asked_for_again(self,
                                                                 world):
        """Not waited on for ever, and not called drained either."""
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        forges.drain_ok = False
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        runner = the_runner(service)
        assert runner["actual_state"] == "draining"
        assert "did not confirm" in runner["last_error"]

        forges.drain_ok = True
        passes(service, reconciler, 2)
        assert the_runner(service)["actual_state"] == "drained"
        assert forges.drained == {spec["registration_id"]}


class TestADrainedGitHubRunnerThatIsSentAJob:
    """GitHub will not let anyone take off `self-hosted`, the OS or the
    architecture, so a job asking for nothing else still reaches a runner
    drained by its labels. It is caught before anything ends it."""

    def test_its_removal_waits_for_that_job(self, world):
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        assert the_runner(service)["actual_state"] == "drained"

        forges.busy.add(spec["registration_id"])   # a bare self-hosted job
        service.set_desired(spec["runner_id"], "absent")
        before = len(unit_calls())
        report = reconciler.pass_once()
        assert the_runner(service)["actual_state"] == "draining"
        assert any("took a job" in why for _, why in report.held)
        passes(service, reconciler, 3)
        assert len(unit_calls()) == before
        assert not [c for c in agent.calls if c[0] == "deregister"]

        forges.busy.discard(spec["registration_id"])
        passes(service, reconciler, 6)
        # Gone once the job was done - and replaced, since its fleet still
        # asks for one.
        assert service.specs.get(spec["runner_id"])["actual_state"] ==             "absent"

    def test_a_drained_runner_left_drained_is_kept_looked_at(self, world):
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        forges.busy.add(spec["registration_id"])
        passes(service, reconciler, 1)
        assert the_runner(service)["actual_state"] == "draining"


class TestServingAgainUndoesTheDrain:
    def test_cancel_drain_puts_a_github_runner_back_at_the_forge(self,
                                                                world):
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        service.cancel_drain(spec["runner_id"])
        passes(service, reconciler, 2)
        assert the_runner(service)["actual_state"] == "idle"
        assert forges.drained == set()

    def test_cancel_drain_starts_a_forgejo_runner_again(self, world):
        service, flow, agent, forges, reconciler = world
        spec = serving_forgejo(world)
        service.drain(spec["runner_id"])
        passes(service, reconciler, 2)
        service.cancel_drain(spec["runner_id"])
        passes(service, reconciler, 2)
        assert the_runner(service, FJ)["actual_state"] == "idle"
        assert [c for c in agent.calls if c[0] == "cancel_drain"]
        assert agent.drained == set()

    def test_a_restart_of_a_busy_github_runner_comes_back_taking_jobs(
            self, world):
        """Restart of a busy runner is drain, stop, start. Without the start
        putting it back at the forge, it would come back idle for ever."""
        service, flow, agent, forges, reconciler = world
        spec = serving(world)
        operation_id = service.restart(spec["runner_id"])
        forges.busy.add(spec["registration_id"])
        runner = the_runner(service)
        service.specs.update(runner["runner_id"], runner["spec_version"],
                             actual_state="busy")
        passes(service, reconciler, 2)
        assert forges.drained == {spec["registration_id"]}

        forges.busy.discard(spec["registration_id"])
        passes(service, reconciler, 8)
        assert service.operations.get(operation_id)["state"] == "succeeded"
        assert the_runner(service)["actual_state"] == "idle"
        assert forges.drained == set(), "back in service at the forge"
