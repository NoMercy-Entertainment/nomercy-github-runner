"""T-1603: clearing one runner's cache never damages another's, as a test.

Two instances on one worker, every area of both filled, every scope of one
cleared - and the other's storage compared before and after. For all three
runtimes against their fakes, where every command and every file operation
the clear made is also checked to have named only the cleared runner. And
for the two runtimes that keep data in plain directories, once more on this
machine's real disk, in a temporary directory, with every file of the other
runner hashed before and after.
"""
import hashlib
import os

import pytest

from agent import naming
from agent.runtimes import linux_container as linux
from agent.runtimes import macos_appliance as macos
from agent.runtimes import windows_process as windows
from agent.runtimes.localfs import LocalFs

from .fake_docker import FakeDocker
from .fake_macos import TEMPLATE as MAC_TEMPLATE
from .fake_macos import TOOLS as MAC_TOOLS
from .fake_macos import FakeMac
from .fake_windows import TEMPLATE as WIN_TEMPLATE
from .fake_windows import TOOLS as WIN_TOOLS
from .fake_windows import FakeWindows

MINE = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
THEIRS = "550e8400-e29b-41d4-a716-446655440000"


# ---------------------------------------------------------------------------
# against the fakes, all three runtimes
# ---------------------------------------------------------------------------

class TestLinux:
    def test_the_other_runner_is_identical_and_never_named(self):
        docker = FakeDocker()
        rt = linux.LinuxContainerRuntime(run=docker)
        for rid in (MINE, THEIRS):
            rt.create(rid, {"image": "ghcr.io/x/runner:1"})
            vols = naming.names(rid, "linux")
            for area, vol in vols.items():
                docker.put(vol, f"{area}.bin", 1000)
            docker.engine(vols["docker"], build_cache=9000, images=4000)
        theirs = {n: docker.snapshot().get(n)
                  for n in naming.names(THEIRS, "linux").values()}
        docker.calls.clear()

        rt.clear_cache(MINE, {"scopes": sorted(linux.SUPPORTED_SCOPES)})

        assert {n: docker.snapshot().get(n)
                for n in naming.names(THEIRS, "linux").values()} == theirs
        mine = naming.unit_name(MINE)
        for call in docker.calls:
            assert THEIRS not in " ".join(call), call
            if call[0] == "exec":
                assert call[1] == mine or call[1:3] == ["-i", mine], call


class _RecordingFs:
    """Wraps a fake's disk and remembers every path it was asked about."""

    def __init__(self, fs):
        self._fs = fs
        self.paths = []

    def __getattr__(self, name):
        target = getattr(self._fs, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.paths += [a for a in args if isinstance(a, str)]
            return target(*args, **kwargs)
        return call


@pytest.mark.parametrize("kind", ["windows", "macos"])
def test_a_directory_runtime_never_names_the_other_runner(kind):
    if kind == "windows":
        host = FakeWindows()
        rt = windows.WindowsProcessRuntime(run=host, fs=host, tools=WIN_TOOLS)
        spec = {"image": WIN_TEMPLATE}
        sep = "\\"
    else:
        host = FakeMac()
        rt = macos.MacApplianceRuntime(run=host, fs=host,
                                       appliance=host.appliance,
                                       tools=MAC_TOOLS)
        spec = {"image": MAC_TEMPLATE}
        sep = "/"
    for rid in (MINE, THEIRS):
        rt.create(rid, spec)
        for area in ("work", "cache", "reg", "logs", "tmp"):
            host.put(rt.paths(rid)[area] + f"{sep}{area}.bin", 1000)
    theirs = host.snapshot(rt.paths(THEIRS)["root"])
    recording = _RecordingFs(host)
    rt._fs = recording

    freed = rt.clear_cache(MINE, {"scopes": sorted(rt.capabilities()[
        "cache_scopes"])})

    assert freed["total_bytes"] == 3000, "workspace, toolcache and temp"
    assert host.snapshot(rt.paths(THEIRS)["root"]) == theirs
    root = rt.paths(MINE)["root"]
    assert recording.paths and all(p.startswith(root + sep)
                                   for p in recording.paths), recording.paths


# ---------------------------------------------------------------------------
# on a real disk
# ---------------------------------------------------------------------------

def _fill(path, name, size):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, name), "wb") as fh:
        fh.write(os.urandom(size))
    nested = os.path.join(path, "deep", "er")
    os.makedirs(nested, exist_ok=True)
    with open(os.path.join(nested, name + ".2"), "wb") as fh:
        fh.write(os.urandom(size // 2))


def _hashes(root):
    out = {}
    for base, _, files in os.walk(root):
        for f in files:
            full = os.path.join(base, f)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(
                    fh.read()).hexdigest()
    return out


@pytest.mark.parametrize("kind", ["windows", "macos"])
def test_on_a_real_disk_the_other_runner_is_byte_identical(kind, tmp_path,
                                                           monkeypatch):
    if kind == "windows":
        monkeypatch.setattr(naming, "WINDOWS_ROOT", str(tmp_path / "runners"))
        rt = windows.WindowsProcessRuntime(fs=LocalFs())
    else:
        monkeypatch.setattr(naming, "MACOS_ROOT",
                            (tmp_path / "runners").as_posix())
        rt = macos.MacApplianceRuntime(fs=LocalFs())
    for rid in (MINE, THEIRS):
        p = rt.paths(rid)
        for area in ("work", "cache", "reg", "logs", "tmp"):
            _fill(p[area], f"{area}.bin", 4096)
    theirs_before = _hashes(rt.paths(THEIRS)["root"])
    mine_kept = {a: _hashes(rt.paths(MINE)[a]) for a in ("reg", "logs")}

    freed = rt.clear_cache(MINE, {"scopes": sorted(rt.capabilities()[
        "cache_scopes"])})

    assert _hashes(rt.paths(THEIRS)["root"]) == theirs_before
    for area in ("work", "cache", "tmp"):
        assert os.listdir(rt.paths(MINE)[area]) == [], area
    for area in ("reg", "logs"):
        assert _hashes(rt.paths(MINE)[area]) == mine_kept[area], \
            f"{area} is not a cache scope and must survive"
    assert freed["errors"] == {}
    assert freed["total_bytes"] == 3 * (4096 + 2048)
