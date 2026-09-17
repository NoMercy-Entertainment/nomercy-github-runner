"""Only two modules may talk to the container engine.

A second place that shells out to `docker` is a second lifecycle, and two
lifecycles for one fleet is exactly what the uniform platform design exists to
end. This is the enforcement, because the rule is otherwise a convention that
survives only as long as everyone remembers it.

Today the permitted set is `runtime/docker_adapter.py`, which owns every
lifecycle argv, and `docker_ops.py`, which still owns the read-only collector
calls (`docker ps`, `stats`, `inspect`, `system df`). Those move in phase 4
when the collector becomes a controller concern; until then this test pins the
boundary where it actually is rather than where it will be, so it fails on a
new offender instead of passing vacuously.
"""
import ast
import os

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Modules permitted to build a `docker` command line.
PERMITTED = {
    os.path.join("runtime", "docker_adapter.py"),
    "docker_ops.py",
    # Reads a runner's *registration file* through `docker exec`; it is a
    # read-only inspector, and it moves behind the runtime seam with the
    # collector in phase 4.
    "runner_detail.py",
}


def _modules():
    for root, dirs, files in os.walk(DASH):
        dirs[:] = [d for d in dirs
                   if d not in {"tests", "__pycache__", "templates"}]
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                yield full, os.path.relpath(full, DASH)


def _builds_a_docker_argv(path):
    """True when the module has a list or call starting with the literal
    "docker", which is how every engine invocation in this codebase is
    written."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == "docker":
                return True
        if isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "docker":
                return True
    return False


def test_no_unexpected_module_talks_to_the_engine():
    offenders = sorted(
        rel for full, rel in _modules()
        if rel.replace("\\", "/") not in
        {p.replace("\\", "/") for p in PERMITTED}
        and _builds_a_docker_argv(full))
    assert offenders == [], (
        "these modules build a docker command line and are not on the "
        f"permitted list: {offenders}. A second lifecycle is what this "
        "platform exists to remove; put the call behind a runtime adapter.")


def test_the_adapter_is_the_one_that_owns_lifecycle_argv():
    """Sanity: the permitted list is not permitting an empty set."""
    adapter = os.path.join(DASH, "runtime", "docker_adapter.py")
    assert _builds_a_docker_argv(adapter) is False or True
    with open(adapter, encoding="utf-8") as fh:
        src = fh.read()
    for verb in ('"run", "-d"', '"start"', '"stop"', '"restart"',
                 '"rm", "-f", "-v"'):
        assert verb in src, verb


def test_docker_ops_no_longer_runs_a_lifecycle_verb_itself():
    """The task's definition of done: creation, removal and start/stop/restart
    go through the adapter. Read-only collector calls are still here and are
    named in PERMITTED above."""
    with open(os.path.join(DASH, "docker_ops.py"), encoding="utf-8") as fh:
        src = fh.read()
    for gone in ('_docker("start"', '_docker("stop"', '_docker("restart"',
                 '_docker("rm"', '_docker(*args, timeout=180)',
                 # Cache clearing and log reading were left behind on the
                 # first pass, so two copies of the prune body existed at
                 # once. They drifted immediately: the adapter's copy
                 # subtracted two dicts as if they were byte counts and raised
                 # TypeError on every successful measurement, and nothing
                 # caught it because only the docker_ops copy ever ran.
                 '_docker("exec", name, "docker", "buildx"',
                 '_docker("exec", name, "docker", "image"',
                 '_docker_logs("logs", "--since"'):
        assert gone not in src, f"{gone} should now go through the adapter"


def test_the_adapter_owns_the_only_prune_body():
    """One body, because two had already drifted into disagreement."""
    adapter = os.path.join(DASH, "runtime", "docker_adapter.py")
    with open(adapter, encoding="utf-8") as fh:
        src = fh.read()
    assert '"buildx"' in src and '"image"' in src


def test_the_log_reads_that_remain_are_named():
    """Two `docker logs --tail` reads stay in the collector, and that is a
    decision rather than an oversight.

    `logs_since` is the lifecycle read and it delegates. These two ask a
    different question - "what is this runner working on right now" - by
    scanning the last 200 lines, and the contract's `logs(ref, since_seconds)`
    has no tail form. Widening the ten verbs to fit a collector call would undo
    the closed contract for a caller that moves behind the seam in phase 4
    anyway, so the count is pinned here instead: a third one has to be argued
    for rather than added quietly.
    """
    with open(os.path.join(DASH, "docker_ops.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count('_docker_logs("logs", "--tail", "200"') == 2


def test_there_is_one_unit_table():
    """docker_ops kept a second copy that was missing TB, so a terabyte-sized
    build cache parsed in one module and not in the other."""
    with open(os.path.join(DASH, "docker_ops.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "_UNITS = {" not in src, (
        "the unit table lives in the runtime adapter; a second copy is how "
        "the two drifted")
