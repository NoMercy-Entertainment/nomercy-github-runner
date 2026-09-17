"""The runtime seam must stay closed, and stay ignorant of the forges.

Three properties are load-bearing and none of them is obvious from reading the
module, so each is asserted here.

The verb set is closed. The control agent is specified to accept only
predefined runner operations and to offer no endpoint for arbitrary shell,
PowerShell or SSH. `Probe` being an enum is the type-level form of that rule:
a probe cannot carry a command because there is nowhere to put one. A test is
the only thing that stops a later change quietly adding a free-text member.

The seam is ignorant in both directions. A runtime adapter must not learn which
forge a runner belongs to, and a provider adapter must not learn how a runner
is executed. Import isolation is what makes that checkable rather than a
convention people remember for a while.

Nothing here is collapsed into a zero. "Not measured" and "measured as zero"
are different answers; a runner whose disk could not be read is not a runner
with an empty disk, and the optional fields exist to keep that distinction.
"""
import ast
import inspect
import os

from runtime import base


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_PY = os.path.join(HERE, "runtime", "base.py")


class TestTheVerbSetIsClosed:
    def test_the_protocol_carries_exactly_the_declared_verbs(self):
        declared = set(base.RUNTIME_VERBS)
        actual = {
            name for name, _ in inspect.getmembers(
                base.RuntimeAdapter, inspect.isfunction)
            if not name.startswith("_")
        }
        assert actual == declared, (
            "the runtime contract changed; widening it is a deliberate act, "
            f"missing={declared - actual} unexpected={actual - declared}")

    def test_there_are_ten_of_them(self):
        assert len(base.RUNTIME_VERBS) == 10

    def test_probe_is_an_enum_with_no_free_text_member(self):
        """A probe that could carry a string would be a remote shell."""
        for member in base.Probe:
            assert isinstance(member.value, str)
            assert member.value.replace("_", "").isalnum(), member
        assert {m.name for m in base.Probe} == {
            "DISK_USAGE", "JOB_STATE", "AGENT_VERSION", "CACHE_SIZE"}

    def test_exec_probe_takes_a_probe_not_a_string(self):
        """Resolved, not read as text: `from __future__ import annotations`
        makes every annotation a string, so comparing the raw annotation would
        pass for a parameter annotated with the *word* Probe and no such type."""
        import typing

        hints = typing.get_type_hints(base.RuntimeAdapter.exec_probe)
        assert hints["probe"] is base.Probe
        assert hints["return"] is base.ProbeResult


class TestTheSeamIsIgnorant:
    def _imports(self):
        with open(BASE_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_the_contract_imports_nothing_from_the_application(self):
        forbidden = {"docker_ops", "providers", "app", "github_api",
                     "forgejo_api", "history", "users", "oidc",
                     "runner_detail", "external_telemetry"}
        assert not (self._imports() & forbidden), self._imports()

    def test_it_imports_only_the_standard_library(self):
        allowed = {"__future__", "enum", "dataclasses", "typing"}
        assert self._imports() <= allowed, self._imports()


class TestAbsenceIsNotZero:
    def test_telemetry_fields_default_to_none(self):
        """Every field, so an unmeasured value can never render as 0."""
        t = base.Telemetry()
        for f in ("cpu_percent", "cpu_cores", "mem_used_bytes",
                  "mem_limit_bytes", "disk_used_bytes", "disk_total_bytes",
                  "cache_bytes", "sampled_at"):
            assert getattr(t, f) is None, f

    def test_a_failed_probe_carries_no_value(self):
        r = base.ProbeResult(probe=base.Probe.DISK_USAGE, ok=False,
                             error="could not read")
        assert r.value is None
        assert r.ok is False

    def test_clear_cache_reports_partial_failure(self):
        """A scope that failed must not disappear behind the ones that worked."""
        f = base.Freed(per_scope={"workspace": 10}, errors={"toolcache": "busy"},
                       total_bytes=10)
        assert f.errors["toolcache"]
        assert f.total_bytes == 10


class TestVocabulary:
    def test_a_guest_is_never_called_a_container(self):
        """Calling a VM a container is how a design claims uniformity it does
        not have. The appliance kind is named for what it is."""
        assert base.ExecUnitKind.MACOS_APPLIANCE.value == "macos-appliance"
        assert "container" not in base.ExecUnitKind.MACOS_APPLIANCE.value

    def test_the_handle_is_opaque(self):
        """It must not become a second identity beside runner_id."""
        ref = base.ExecUnitRef(kind=base.ExecUnitKind.LINUX_CONTAINER,
                               handle="deadbeef")
        assert ref.handle == "deadbeef"
        with __import__("pytest").raises(Exception):
            ref.handle = "other"  # frozen
