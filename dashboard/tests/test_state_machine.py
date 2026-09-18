"""The lifecycle machine, and whether it is the one the design draws.

The test that matters most here is the last class. A design document whose
diagram describes one machine while the code runs another is worse than having
no diagram, because everyone trusts it. So the diagram is parsed out of the
spec and compared to the table, edge for edge and label for label. Change
either without the other and this fails.

The rest asserts the two properties the machine exists to provide: every edge
the design draws actually works, and every edge it does not draw is refused
loudly rather than ignored.
"""
import os
import re

import pytest

from control import states
from control.states import IllegalTransition

SPEC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs", "superpowers", "specs",
    "2026-09-17-uniform-hyperv-runner-platform-design.md")


def diagram_edges():
    """(from, to) -> label, read out of the state diagram in spec 12.2.

    `[*]` becomes None on the way in and is dropped on the way out, matching
    how the table spells the start and the end.
    """
    with open(SPEC, encoding="utf-8") as fh:
        text = fh.read()

    block = re.search(r"stateDiagram-v2\n(.*?)```", text, re.S)
    assert block, "spec 12.2 no longer contains a state diagram"

    edges = {}
    for line in block.group(1).splitlines():
        m = re.match(r"\s*(\S+)\s*-->\s*(\S+?)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        frm, to, label = m.groups()
        frm = None if frm == "[*]" else frm
        if to == "[*]":
            continue
        edges[(frm, to)] = label
    return edges


class TestEveryEdgeTheDesignDrawsWorks:
    def test_each_one_is_permitted(self):
        for (frm, to) in diagram_edges():
            assert states.can(frm, to), (frm, to)

    def test_check_returns_what_drives_the_edge(self):
        assert states.check(None, "planned") == "create"
        assert states.check("busy", "draining") == "drain"

    def test_a_runner_can_be_driven_from_nothing_to_serving_a_job(self):
        """The happy path, walked end to end, because a machine where every
        edge exists but no route through them does is still broken."""
        path = [None, "planned", "provisioning", "provisioned", "registering",
                "idle", "busy"]
        for frm, to in zip(path, path[1:]):
            states.check(frm, to)

    def test_a_runner_can_be_driven_from_serving_to_gone(self):
        path = ["busy", "draining", "drained", "deregistering", "removing",
                "absent"]
        for frm, to in zip(path, path[1:]):
            states.check(frm, to)

    def test_a_failure_can_be_repaired_or_removed(self):
        """Both exits from failed exist, or a failed runner is a dead end that
        has to be cleaned up by hand."""
        states.check("failed", "provisioning")
        states.check("failed", "removing")


class TestEveryNonEdgeIsRefused:
    def test_an_illegal_transition_raises(self):
        """Ignoring one silently is how a runner reaches a state nobody can
        explain: something asked for the impossible and nothing was recorded."""
        with pytest.raises(IllegalTransition):
            states.check("idle", "absent")

    def test_the_refusal_names_both_states_and_the_way_out(self):
        with pytest.raises(IllegalTransition) as caught:
            states.check("idle", "absent", verb="remove")
        message = str(caught.value)
        assert "idle" in message and "absent" in message
        assert "remove" in message
        assert "busy" in message, "it must say what IS allowed from here"

    def test_every_pair_the_diagram_omits_is_refused(self):
        """Exhaustive over the whole product, so a stray edge cannot be added
        to the table without the diagram gaining it too."""
        drawn = set(diagram_edges())
        every = {(a, b)
                 for a in list(states.STATES) + [None]
                 for b in states.STATES}
        for pair in every - drawn:
            assert not states.can(*pair), f"{pair} is not in the design"

    def test_a_terminal_runner_goes_nowhere(self):
        assert states.next_states("absent") == frozenset()

    def test_a_runner_cannot_skip_registration(self):
        """The edge that would produce a runner the forge has never heard of."""
        with pytest.raises(IllegalTransition):
            states.check("provisioned", "idle")

    def test_a_busy_runner_cannot_be_stopped(self):
        """MIG-9: never abort a running job. Drain first is the only route."""
        with pytest.raises(IllegalTransition):
            states.check("busy", "stopping")
        assert states.can("busy", "draining")


class TestTheEighteenVerbs:
    """`uniform.md` 162-181, each one accounted for."""

    EXPECTED = {
        "create", "provision", "register", "start", "stop", "restart",
        "drain", "cancel_drain", "recreate", "remove", "deregister",
        "scale_up", "scale_down", "fetch_status", "fetch_logs",
        "inspect_resources", "clear_cache", "repair",
    }

    def test_there_are_eighteen(self):
        assert states.VERBS == self.EXPECTED
        assert len(states.VERBS) == 18

    def test_each_is_an_edge_a_read_a_fleet_action_or_a_composite(self):
        """A verb fitting none of these would be the platform-specific special
        case the design forbids."""
        for verb in states.VERBS:
            kinds = [verb in states.VERB_EDGES, verb in states.READS,
                     verb in states.FLEET_VERBS, verb in states.COMPOSITE,
                     verb in states.GUARDED]
            assert sum(kinds) == 1, f"{verb} is {sum(kinds)} kinds of thing"

    def test_every_verb_edge_is_a_real_edge(self):
        for verb, edges in states.VERB_EDGES.items():
            for frm, to in edges:
                assert states.can(frm, to), (verb, frm, to)

    def test_a_read_is_allowed_in_any_state(self):
        for verb in states.READS:
            for state in states.STATES:
                assert states.allows(verb, state)

    def test_a_verb_says_where_it_leads(self):
        assert states.verb_target("stop", "idle") == "stopping"
        assert states.verb_target("stop", "drained") == "stopping"
        assert states.verb_target("remove", "failed") == "removing"

    def test_a_verb_from_an_impossible_state_raises(self):
        """Rather than returning None, which a caller will one day forget to
        check and write into the database."""
        with pytest.raises(IllegalTransition):
            states.verb_target("start", "idle")

    def test_restart_is_stop_then_start_once(self):
        """One definition, or each platform grows its own."""
        assert states.COMPOSITE["restart"] == ("stop", "start")

    def test_recreate_keeps_the_data_and_that_is_its_whole_point(self):
        assert states.COMPOSITE["recreate"] == ("remove", "create")

    def test_clear_cache_needs_an_idle_or_drained_runner(self):
        assert states.allows("clear_cache", "idle")
        assert states.allows("clear_cache", "drained")
        assert not states.allows("clear_cache", "busy")

    def test_an_unknown_verb_is_an_error_not_a_false(self):
        with pytest.raises(ValueError):
            states.allows("delete_everything", "idle")


class TestObservedEdgesAreNotCommands:
    """Who may write which edge.

    An edge like `idle -> busy` is a report that a job started. A route that
    wrote it would be asserting something it did not witness, and the runner's
    state would then disagree with the runner.
    """

    def test_the_two_sets_cover_every_edge_exactly_once(self):
        assert states.OBSERVED | states.COMMANDED == frozenset(
            states.TRANSITIONS)
        assert states.OBSERVED & states.COMMANDED == frozenset()

    def test_taking_a_job_is_observed(self):
        assert ("idle", "busy") in states.OBSERVED

    def test_stopping_is_commanded(self):
        assert ("idle", "stopping") in states.COMMANDED

    def test_every_commanded_edge_belongs_to_a_verb(self):
        """A commanded edge nothing can ask for is unreachable."""
        by_verb = {e for edges in states.VERB_EDGES.values() for e in edges}
        assert states.COMMANDED <= by_verb


class TestTransitionalStates:
    def test_they_are_the_ones_a_sweeper_watches(self):
        """A spec sitting in one of these past its deadline is the shape every
        half instance has: something started and did not finish."""
        assert "provisioning" in states.TRANSITIONAL
        assert "registering" in states.TRANSITIONAL
        assert "idle" not in states.TRANSITIONAL

    def test_none_of_them_is_a_resting_state(self):
        for state in states.TRANSITIONAL:
            assert states.next_states(state), f"{state} leads nowhere"

    def test_every_state_is_transitional_live_terminal_or_failed(self):
        """No state falls between the categories the reconciler reasons in."""
        accounted = (states.TRANSITIONAL | states.LIVE
                     | {"stopped", "absent", "failed"})
        assert states.STATES - accounted == frozenset()


class TestTheTableIsTheDiagram:
    """T-0303's definition of done.

    The diagram is parsed out of the spec rather than copied here, so the two
    cannot drift. This is the test that makes design section 12.2 trustworthy.
    """

    def test_the_edges_are_the_same_set(self):
        assert set(states.TRANSITIONS) == set(diagram_edges())

    def test_every_label_matches(self):
        drawn = diagram_edges()
        for edge, trigger in states.TRANSITIONS.items():
            assert trigger == drawn[edge], edge

    def test_the_diagram_actually_parsed(self):
        """Guards against a silent pass if the spec moves or the block is
        renamed: an empty parse would make every comparison above vacuous."""
        assert len(diagram_edges()) == 24

    def test_no_state_exists_only_in_the_code(self):
        drawn = {s for edge in diagram_edges() for s in edge if s}
        assert states.STATES == drawn
