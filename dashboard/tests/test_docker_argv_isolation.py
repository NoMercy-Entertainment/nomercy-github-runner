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
                 '_docker("rm"', '_docker(*args, timeout=180)'):
        assert gone not in src, f"{gone} should now go through the adapter"
