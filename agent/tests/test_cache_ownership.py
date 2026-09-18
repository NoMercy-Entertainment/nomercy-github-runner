"""T-1601: every cache scope resolves to a place this one runner owns, for all
three runtimes, and the list of scopes each offers is generated from that.

This is the control that makes `uniform.md` line 322 true: clearing a cache
can only ever delete what belongs to the runner it was asked for, because
every scope is a place derived from that runner's id and nothing else. A
scope that could not be attributed that way is not offered at all.
"""
import ntpath
import posixpath
import uuid

import pytest

from agent import naming, protocol
from agent.runtimes import linux_container as linux
from agent.runtimes import macos_appliance as macos
from agent.runtimes import windows_process as windows

IDS = [str(uuid.UUID(int=i * 7919 + 1, version=4)) for i in range(40)]

RUNTIMES = {
    "linux-container": linux,
    "windows-process": windows,
    "macos-appliance": macos,
}


def locations(kind, rid):
    if kind == "linux-container":
        return linux.scope_locations(rid)
    runtime = {"windows-process": windows.WindowsProcessRuntime,
               "macos-appliance": macos.MacApplianceRuntime}[kind]()
    return runtime.scope_locations(rid)


def place(kind, location):
    """One comparable identity for where a scope's data is."""
    if kind == "linux-container":
        return (location["unit"], location["volume"] or "unit-layer",
                location["path"])
    return location


class TestEveryScopeIsTheRunnersOwn:
    @pytest.mark.parametrize("rid", IDS[:10])
    def test_linux_every_scope_is_inside_its_own_unit_and_volumes(self, rid):
        own = set(naming.names(rid, "linux").values())
        for scope, loc in linux.scope_locations(rid).items():
            assert loc["unit"] == naming.unit_name(rid), scope
            if loc["area"] == "unit":
                assert loc["volume"] is None, scope
            else:
                assert loc["volume"] in own, scope
                assert loc["volume"].startswith(f"rnr-{rid}-")

    @pytest.mark.parametrize("rid", IDS[:10])
    def test_windows_every_scope_is_inside_its_own_tree(self, rid):
        rt = windows.WindowsProcessRuntime()
        root = rt.paths(rid)["root"]
        for scope, path in rt.scope_locations(rid).items():
            assert path.startswith(root + "\\"), scope
            assert ntpath.normpath(path) == path

    @pytest.mark.parametrize("rid", IDS[:10])
    def test_macos_every_scope_is_inside_its_own_tree(self, rid):
        rt = macos.MacApplianceRuntime()
        root = rt.paths(rid)["root"]
        for scope, path in rt.scope_locations(rid).items():
            assert path.startswith(root + "/"), scope
            assert posixpath.normpath(path) == path

    @pytest.mark.parametrize("kind", sorted(RUNTIMES))
    def test_no_two_runners_share_a_place(self, kind):
        # Two scopes of one runner may share a place - both engine scopes
        # live in that runner's own engine. Two runners never may.
        seen = {}
        for rid in IDS:
            for scope, loc in locations(kind, rid).items():
                key = place(kind, loc)
                assert seen.setdefault(key, rid) == rid, (scope, rid,
                                                          seen[key])

    @pytest.mark.parametrize("kind", sorted(RUNTIMES))
    def test_an_id_that_is_not_one_resolves_to_nothing(self, kind):
        with pytest.raises(naming.InvalidRunnerId):
            locations(kind, "../../shared")


class TestTheListIsGenerated:
    @pytest.mark.parametrize("kind", sorted(RUNTIMES))
    def test_what_a_runtime_offers_is_its_ownership_table(self, kind):
        module = RUNTIMES[kind]
        assert module.SUPPORTED_SCOPES == frozenset(module.SCOPE_AREAS)
        runtime = {"linux-container": linux.LinuxContainerRuntime,
                   "windows-process": windows.WindowsProcessRuntime,
                   "macos-appliance": macos.MacApplianceRuntime}[kind]()
        assert runtime.capabilities()["cache_scopes"] == \
            sorted(module.SCOPE_AREAS)

    @pytest.mark.parametrize("kind", sorted(RUNTIMES))
    def test_every_scope_offered_is_one_the_protocol_names(self, kind):
        assert RUNTIMES[kind].SUPPORTED_SCOPES <= protocol.CACHE_SCOPES

    def test_linux_can_clear_exactly_what_it_offers(self):
        assert set(linux.SCOPE_AREAS) == \
            set(linux.SCOPE_PATHS) | set(linux.ENGINE_SCOPES)

    @pytest.mark.parametrize("kind", ["windows-process", "macos-appliance"])
    def test_no_engine_scope_where_there_is_no_engine(self, kind):
        assert not {s for s in RUNTIMES[kind].SUPPORTED_SCOPES
                    if s.startswith("engine-")}

    def test_a_scope_shared_by_every_instance_is_not_offered(self):
        """Xcode's DerivedData, in the user's Library of the appliance."""
        assert not any("derived" in s for s in macos.SUPPORTED_SCOPES)
