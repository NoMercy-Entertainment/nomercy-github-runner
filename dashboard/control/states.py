"""The lifecycle, as a table rather than as scattered conditionals.

One machine for all six provider x platform cells. It is the literal content of
design section 12.2: every edge below appears in that diagram, and a test
parses the diagram out of the spec and asserts the two agree. That test is what
keeps this file honest - a diagram that documents one machine while the code
runs another is worse than no diagram.

**Illegal transitions raise.** Silently ignoring one is how a runner ends up in
a state nobody can explain: something asked for the impossible, nothing
happened, and no trace was left. A raise names both states and what was
attempted.

**Nothing here knows what a runner is.** No import of a provider, a runtime, a
store or the web layer. The machine is the same whether the thing being driven
is a container, a Windows process tree or a macOS appliance, and the cheapest
way to keep it that way is to give it nothing to be specific about.

Two kinds of edge, and the difference is why a transition happens:

  COMMANDED - something asked for it. `stop`, `drain`, `remove`.
  OBSERVED  - the world turned out that way. A job started, a unit came up, an
              error happened. A report, not a request.

The reconciler writes both. It is the only writer of `actual_state` at all -
the service records what is wanted and never what is - so the split is not
about who holds the pen. It is about whether the transition may be asked for:
a route that could ask for `idle -> busy` would be claiming a job started that
nobody saw start.

Two edges were added while building the reconciler, and the design records why:
`planned -> absent` so a runner that exists only on paper can be withdrawn, and
`removing -> provisioning` so `recreate` can keep its storage. See spec 12.2.

`unknown` is deliberately not a state here. It is the database default for a
row whose actual state has never been observed, which is a different statement
from any position in this machine. A spec that has been planned says `planned`.
"""

#: mermaid's `[*]`: a runner that does not exist yet.
START = None

#: Where the machine ends. A row in this state is kept, not deleted, because
#: history still refers to it.
TERMINAL = "absent"

#: Every edge of design section 12.2, with the label the diagram gives it.
#: (from, to) -> trigger
TRANSITIONS = {
    (START, "planned"): "create",
    ("planned", "provisioning"): "reconcile",
    ("planned", "absent"): "withdraw",
    ("provisioning", "provisioned"): "exec unit exists",
    ("provisioning", "failed"): "error",
    ("provisioned", "registering"): "provider.register",
    ("registering", "idle"): "forge confirms",
    ("registering", "failed"): "error",
    ("idle", "busy"): "job accepted",
    ("busy", "idle"): "job finished",
    ("idle", "draining"): "drain",
    ("busy", "draining"): "drain",
    ("draining", "drained"): "job finished",
    ("drained", "idle"): "cancel drain",
    ("drained", "stopping"): "stop",
    ("idle", "stopping"): "stop",
    ("stopping", "stopped"): "exec unit down",
    ("stopped", "starting"): "start",
    ("starting", "idle"): "agent reports ready",
    ("drained", "deregistering"): "remove",
    ("stopped", "deregistering"): "remove",
    ("deregistering", "removing"): "forge confirms",
    ("removing", "absent"): "exec unit and storage gone",
    ("removing", "provisioning"): "exec unit gone, storage kept",
    ("failed", "provisioning"): "repair",
    ("failed", "removing"): "remove",
}

STATES = frozenset(s for edge in TRANSITIONS for s in edge if s is not None)

#: States the machine passes through rather than rests in. A spec sitting in
#: one of these past its operation deadline is what the sweeper looks for: it
#: means something started and did not finish, which is the shape every half
#: instance has.
TRANSITIONAL = frozenset({
    "planned", "provisioning", "provisioned", "registering", "draining",
    "stopping", "starting", "deregistering", "removing",
})

#: States where the runner is alive and answering.
LIVE = frozenset({"idle", "busy", "draining", "drained"})

#: Edges driven by an observation rather than by a request. Nothing may ask for
#: one of these: a route that could would be reporting something it did not
#: witness. (The reconciler writes every edge; see the module docstring.)
OBSERVED = frozenset({
    ("planned", "provisioning"),
    ("provisioning", "provisioned"),
    ("provisioning", "failed"),
    ("registering", "idle"),
    ("registering", "failed"),
    ("idle", "busy"),
    ("busy", "idle"),
    ("draining", "drained"),
    ("stopping", "stopped"),
    ("starting", "idle"),
    ("deregistering", "removing"),
    ("removing", "absent"),
    ("removing", "provisioning"),
})

COMMANDED = frozenset(TRANSITIONS) - OBSERVED


class IllegalTransition(Exception):
    """A transition the machine does not have.

    Carries both states and, when there is one, what the caller was trying to
    do, because "illegal transition" on its own tells an operator nothing about
    which button not to press.
    """

    def __init__(self, frm, to, verb=None):
        allowed = sorted(x for x in next_states(frm))
        super().__init__(
            f"cannot go from {frm!r} to {to!r}"
            + (f" ({verb})" if verb else "")
            + (f"; from {frm!r} the machine allows {allowed}" if allowed
               else f"; {frm!r} is terminal"))
        self.frm = frm
        self.to = to
        self.verb = verb


def next_states(frm):
    """Every state reachable from `frm` in one step."""
    return frozenset(to for (f, to) in TRANSITIONS if f == frm)


def can(frm, to):
    return (frm, to) in TRANSITIONS


def check(frm, to, verb=None):
    """Assert the edge exists; return the trigger that labels it."""
    if (frm, to) not in TRANSITIONS:
        raise IllegalTransition(frm, to, verb)
    return TRANSITIONS[(frm, to)]


# ---------------------------------------------------------------------------
# the eighteen verbs
# ---------------------------------------------------------------------------
#
# Every verb of `uniform.md` 162-181 is one of four things, and saying which is
# how "one generic service" stops being a claim. A verb that fitted none of
# these would be the platform-specific special case the whole design forbids.

#: Reads. No transition, allowed in any state, never an operation.
READS = frozenset({"fetch_status", "fetch_logs", "inspect_resources"})

#: Act on a fleet's desired capacity, not on one runner's state.
FLEET_VERBS = frozenset({"scale_up", "scale_down"})

#: Built from other verbs. Written here so there is one definition of what
#: `restart` means rather than one per platform. `recreate` keeps the data
#: volume, which is the whole difference between it and remove-then-create.
COMPOSITE = {
    "restart": ("stop", "start"),
    "recreate": ("remove", "create"),
}

#: Verbs that move the machine, and the edges each may take. A verb with
#: several edges is one action from several starting points, not several
#: actions - `stop` from `idle` and from `drained` is the same stop.
VERB_EDGES = {
    "create": {(START, "planned")},
    "provision": {("planned", "provisioning")},
    "register": {("provisioned", "registering")},
    "start": {("stopped", "starting")},
    "stop": {("idle", "stopping"), ("drained", "stopping")},
    "drain": {("idle", "draining"), ("busy", "draining")},
    "cancel_drain": {("drained", "idle")},
    # From `planned` nothing exists yet, so removal is a withdrawal with no
    # deregistration and nothing to delete.
    "remove": {("planned", "absent"), ("drained", "deregistering"),
               ("stopped", "deregistering"), ("failed", "removing")},
    "deregister": {("deregistering", "removing")},
    "repair": {("failed", "provisioning")},
}

#: Not a transition, but not a read either: it changes the runner's storage and
#: so must be refused unless the runner is provably not using it. `drained`
#: counts because a drained runner has finished its job and will not take
#: another.
GUARDED = {
    "clear_cache": frozenset({"idle", "drained"}),
}

VERBS = (frozenset(VERB_EDGES) | READS | FLEET_VERBS | frozenset(COMPOSITE)
         | frozenset(GUARDED))


def verb_target(verb, frm):
    """Where `verb` takes a runner that is in state `frm`.

    Raises rather than returning None, because a caller that has to remember to
    check a None is a caller that will one day forget and write it into the
    database.
    """
    if verb not in VERB_EDGES:
        raise ValueError(f"{verb!r} is not a state-changing verb")
    targets = {to for (f, to) in VERB_EDGES[verb] if f == frm}
    if not targets:
        raise IllegalTransition(frm, "?", verb)
    if len(targets) > 1:                    # pragma: no cover - table invariant
        raise ValueError(f"{verb!r} is ambiguous from {frm!r}: {targets}")
    return targets.pop()


def allows(verb, frm):
    """Whether `verb` can be applied to a runner in state `frm`."""
    if verb in READS or verb in FLEET_VERBS:
        return True
    if verb in GUARDED:
        return frm in GUARDED[verb]
    if verb in COMPOSITE:
        first, _ = COMPOSITE[verb]
        return allows(first, frm)
    if verb in VERB_EDGES:
        return any(f == frm for (f, _) in VERB_EDGES[verb])
    raise ValueError(f"unknown verb {verb!r}")
