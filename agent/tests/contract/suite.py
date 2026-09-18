"""Design 19.2's nine scenarios, written to the contract and not to a runtime.

Every scenario takes a `harness` - the platform under test behind one small
interface - and uses only the agent's Runtime and Registrar protocols plus what
the harness exposes about the world: the forge's records, which storage exists,
whether a unit is there. Nothing here knows it is talking to Docker.

The scenarios, in the design's order:

1. create, reach idle, confirm at the forge
2. drain while busy; the job finishes and no new job starts
3. cancel drain; it accepts work again
4. stop, start, restart; the unit and the forge agree
5. clear_cache on an idle instance; bytes freed, idempotent, nothing else
   touched
6. recreate; a new workspace and a fresh registration
7. remove; the forge record and all per-instance storage are gone
8. kill the agent mid-create; no half instance
9. forge unreachable mid-register; no orphaned registration

Two of them, drain and clearing the cache, depend on something a runtime may
honestly not have. Those are gated on the capability that says so; the rest
cannot be declared away.

**No scenario is ever skipped (T-0503).** A runtime must declare each gating
capability as true or false - leaving one out is refused, because omission is
the quietest way to dodge a scenario. Declared true, the scenario runs and
must pass. Declared false, the scenario checks the other direction instead:
that the operation is actually refused, not quietly done or quietly ignored.
So a declaration is a claim the suite verifies either way, and a runtime can
neither over-claim nor under-claim without failing.
"""
import pytest


class NotSupported(Exception):
    """A harness's answer for an operation its platform does not offer."""


#: Capabilities that gate optional scenarios, and which scenarios they gate.
OPTIONAL = {
    "supports_drain": ("drain", "cancel_drain"),
    "clear_cache": ("clear_cache",),
}


def declared(harness, capability):
    """What the runtime says about an optional capability - which it must
    say, as a bool."""
    value = harness.runtime.capabilities().get(capability)
    assert isinstance(value, bool), (
        f"{harness.name} must declare {capability} as true or false; "
        f"leaving it out would skip a scenario without saying so")
    return value


def refused(operation, what):
    """A capability declared false must be refused when asked for - by the
    platform saying so, not by doing nothing."""
    try:
        operation()
    except NotSupported:
        return
    pytest.fail(f"{what} was declared unsupported but went through; a "
                f"declaration the platform does not keep is a false one")


def created(harness, runner_id):
    harness.runtime.create(runner_id, harness.spec())
    return runner_id


def registered(harness, runner_id):
    created(harness, runner_id)
    ids = harness.registrar.register(runner_id, harness.plan(runner_id))
    return ids["registration_id"]


# ---------------------------------------------------------------------------
# 1
# ---------------------------------------------------------------------------

def test_1_create_reaches_idle_and_the_forge_confirms_it(harness):
    rid = harness.new_id()
    registration = registered(harness, rid)

    assert harness.runtime.status(rid)["running"] is True
    records = harness.forge_records(rid)
    assert [r["registration_id"] for r in records] == [registration]
    assert harness.forge_online(registration) is True
    assert harness.storage_present(rid) == harness.expected_areas()


# ---------------------------------------------------------------------------
# 2 and 3 - gated on supports_drain (OPEN-7)
# ---------------------------------------------------------------------------

def test_2_drain_while_busy_finishes_the_job_and_takes_no_other(harness):
    rid = harness.new_id()
    registered(harness, rid)
    if not declared(harness, "supports_drain"):
        refused(lambda: harness.drain(rid), "drain")
        return
    harness.start_job(rid)

    harness.drain(rid)
    assert harness.offer_job(rid) is False, \
        "no new job while it finishes the one it has"
    assert harness.finish_job(rid) is True, "the running job must finish"
    assert harness.offer_job(rid) is False, "and no new one may start"


def test_3_cancel_drain_accepts_work_again(harness):
    rid = harness.new_id()
    registered(harness, rid)
    if not declared(harness, "supports_drain"):
        refused(lambda: harness.cancel_drain(rid), "cancel drain")
        return
    harness.drain(rid)

    harness.cancel_drain(rid)
    assert harness.offer_job(rid) is True


# ---------------------------------------------------------------------------
# 4
# ---------------------------------------------------------------------------

def test_4_stop_start_and_restart_keep_the_unit_and_the_forge_agreeing(
        harness):
    rid = harness.new_id()
    registration = registered(harness, rid)

    harness.runtime.stop(rid)
    assert harness.runtime.status(rid)["running"] is False
    assert harness.forge_online(registration) is False
    assert [r["registration_id"] for r in harness.forge_records(rid)] == \
        [registration], "stopping is not deregistering"

    harness.runtime.start(rid)
    assert harness.runtime.status(rid)["running"] is True
    assert harness.forge_online(registration) is True

    harness.runtime.restart(rid)
    assert harness.runtime.status(rid)["running"] is True
    assert harness.forge_online(registration) is True
    assert [r["registration_id"] for r in harness.forge_records(rid)] == \
        [registration]


# ---------------------------------------------------------------------------
# 5 - gated on clear_cache
# ---------------------------------------------------------------------------

def test_5_clear_cache_frees_space_is_idempotent_and_touches_no_one_else(
        harness):
    rid, other = harness.new_id(), harness.new_id()
    registered(harness, rid)
    registered(harness, other)
    harness.put_cache(rid, 5000)
    harness.put_cache(other, 7000)
    theirs = harness.snapshot(other)

    if not declared(harness, "clear_cache"):
        # Refused, and nothing cleared anywhere - not a success that did
        # nothing, and not a clear it said it could not do.
        mine = harness.snapshot(rid)
        try:
            result = harness.runtime.clear_cache(rid, {})
        except Exception:                   # noqa: BLE001 - refusing is fine
            result = None
        if result is not None:
            assert result.get("total_bytes", 0) == 0
            assert result.get("errors"), \
                "a clear that is not supported must say so, not succeed"
        assert harness.snapshot(rid) == mine
        assert harness.snapshot(other) == theirs
        return

    freed = harness.runtime.clear_cache(rid, {})

    assert freed["total_bytes"] > 0
    assert freed["measured"] is True
    assert harness.snapshot(other) == theirs, \
        "nothing outside this runner may change"

    again = harness.runtime.clear_cache(rid, {})
    assert again["total_bytes"] == 0
    assert again["errors"] == {}


# ---------------------------------------------------------------------------
# 6
# ---------------------------------------------------------------------------

def test_6_recreate_gives_a_new_workspace_and_a_fresh_registration(harness):
    rid = harness.new_id()
    old = registered(harness, rid)
    harness.mark_workspace(rid)
    harness.put_cache(rid, 5000)

    harness.registrar.deregister(rid)
    harness.runtime.remove(rid, keep_data=True)
    new = registered(harness, rid)

    assert not harness.workspace_marked(rid), "the workspace must be new"
    assert new != old, "the registration must be fresh"
    assert [r["registration_id"] for r in harness.forge_records(rid)] == \
        [new], "and the old one gone"
    assert harness.cache_kept(rid), "keeping the data is recreate's point"


# ---------------------------------------------------------------------------
# 7
# ---------------------------------------------------------------------------

def test_7_remove_leaves_no_forge_record_and_no_storage(harness):
    rid = harness.new_id()
    registered(harness, rid)

    harness.registrar.deregister(rid)
    harness.runtime.remove(rid, keep_data=False)

    assert not harness.unit_present(rid)
    assert harness.storage_present(rid) == set()
    assert harness.forge_records(rid) == []


# ---------------------------------------------------------------------------
# 8
# ---------------------------------------------------------------------------

def test_8_a_create_cut_off_by_a_crash_converges_to_a_whole_instance(
        harness):
    """Re-driving the create adopts what the dead attempt left: one unit,
    all its storage."""
    rid = harness.new_id()
    harness.crash_during_create()
    with pytest.raises(BaseException) as caught:
        harness.runtime.create(rid, harness.spec())
    assert not isinstance(caught.value, Exception), "that was a crash"

    harness.runtime.create(rid, harness.spec())

    assert harness.units(rid) == 1
    assert harness.storage_present(rid) == harness.expected_areas()
    # Whole means what a create that was never cut off gives: a running unit
    # that can be registered. One unit and all its storage is not enough - a
    # unit made but never configured or started passes that and is still
    # half an instance.
    assert harness.runtime.status(rid)["running"] is True
    registration = harness.registrar.register(rid, harness.plan(rid))[
        "registration_id"]
    assert harness.forge_online(registration) is True


def test_8_or_to_nothing_at_all(harness):
    """Compensating instead - remove, safe when half of it is absent -
    leaves nothing."""
    rid = harness.new_id()
    harness.crash_during_create()
    with pytest.raises(BaseException):
        harness.runtime.create(rid, harness.spec())

    harness.runtime.remove(rid, keep_data=False)

    assert not harness.unit_present(rid)
    assert harness.storage_present(rid) == set()


# ---------------------------------------------------------------------------
# 9
# ---------------------------------------------------------------------------

def test_9_an_unreachable_forge_mid_register_leaves_no_orphan(harness):
    rid = harness.new_id()
    created(harness, rid)
    harness.forge_reachable(False)

    with pytest.raises(Exception):
        harness.registrar.register(rid, harness.plan(rid))
    assert harness.forge_records(rid) == []

    harness.forge_reachable(True)
    registration = harness.registrar.register(rid, harness.plan(rid))[
        "registration_id"]
    assert [r["registration_id"] for r in harness.forge_records(rid)] == \
        [registration], "a retry registers once, not twice"


# ---------------------------------------------------------------------------
# the suite about itself
# ---------------------------------------------------------------------------

def test_every_gating_capability_is_declared(harness):
    """Omission is refused: it would be a way to skip without saying so."""
    for capability in OPTIONAL:
        declared(harness, capability)


def test_the_suite_skips_nothing():
    """T-0503's definition of done, read from this file's own syntax tree:
    no skip call, no skip or xfail marker, anywhere in it."""
    import ast
    import inspect
    import sys
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Attribute)):
            text = ast.unparse(node.func if isinstance(node, ast.Call)
                               else node)
            if text in ("pytest.skip", "pytest.mark.skip",
                        "pytest.mark.skipif", "pytest.mark.xfail",
                        "pytest.xfail"):
                found.append(f"line {node.lineno}: {text}")
    assert found == []


def test_the_suite_covers_all_nine_scenarios():
    import sys
    numbers = {int(name.split("_")[1]) for name in dir(sys.modules[__name__])
               if name.startswith("test_") and name.split("_")[1].isdigit()}
    assert numbers == set(range(1, 10))
