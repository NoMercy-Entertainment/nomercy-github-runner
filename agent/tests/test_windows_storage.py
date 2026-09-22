"""Storage failure/recovery invariants, with actual PowerShell control flow.

The helper tests stub privileged cmdlets; ordinary manifest/image/mount-folder
IO happens only in pytest's temporary directory. No VHD is attached/formatted.
"""
import json
import ntpath
import os
import re
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import jobhost, naming
from agent.runtimes.windows_process import WindowsProcessRuntime, WindowsRegistrar
from agent.runtimes.windows_storage import DEFAULT_LIMIT, WindowsStorage, verify_volume
from .fake_windows import FakeWindows, TEMPLATE, TOOLS

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
VOLUME = "\\\\?\\Volume{a1234567-1234-1234-1234-123456789abc}\\"
ROOT = Path(__file__).resolve().parents[2]
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def test_helper_uses_fixed_argv_and_json_not_request_text_as_code():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return True, json.dumps({"runner_id": RID, "volume_guid": VOLUME,
                                 "virtual_bytes": DEFAULT_LIMIT,
                                 "capacity_bytes": DEFAULT_LIMIT - 1024**2,
                                 "free_bytes": DEFAULT_LIMIT - 2 * 1024**2}), ""

    backend = WindowsStorage({"root": r"D:\disks with spaces"}, run, PS)
    backend.ensure(RID)
    args, kwargs = calls[0]
    assert args[-2] == "-File"
    assert args[-1].endswith("windows_storage.ps1")
    assert RID not in " ".join(args)
    assert json.loads(kwargs["input"])["limit"] == DEFAULT_LIMIT
    assert "-Command" not in args


@pytest.mark.parametrize("root", [r"D:\runners", r"D:\runners\disk", "D:\\",
                                  r"D:\x:stream", r"\\server\share", "relative"])
def test_invalid_or_overlapping_roots_are_refused(root):
    with pytest.raises(ValueError):
        WindowsStorage({"root": root}, None, PS)


@pytest.mark.parametrize("limit", [True, 0, -1, 1024**3 + 1, "100g"])
def test_invalid_limits_are_refused_before_execution(limit):
    backend = WindowsStorage({}, lambda *a, **k: pytest.fail("must not run"), PS)
    with pytest.raises(ValueError):
        backend.ensure(RID, limit)


class Kernel:
    def __init__(self, result=VOLUME, ok=True):
        self.result, self.ok = result, ok

    def GetVolumeNameForVolumeMountPointW(self, root, buf, count):
        buf.value = self.result
        return self.ok


def test_unprivileged_proof_rejects_plain_and_replaced_mounts():
    assert verify_volume(ntpath.join(r"D:\runners", RID), VOLUME, Kernel())
    with pytest.raises(RuntimeError, match="not mounted"):
        verify_volume("D:/plain", VOLUME, Kernel(ok=False))
    with pytest.raises(RuntimeError, match="does not match"):
        verify_volume("D:/other", VOLUME, Kernel(VOLUME.replace("a123", "b123")))


def test_jobhost_checks_mount_before_unit_or_process_access(monkeypatch):
    from agent.runtimes import windows_storage
    monkeypatch.setattr(windows_storage, "verify_volume",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("wrong volume")))
    monkeypatch.setattr(jobhost, "read_unit", lambda *a: pytest.fail("read unsafe unit"))
    monkeypatch.setattr(jobhost, "packaged", lambda: pytest.fail("passed storage fence"))
    assert jobhost.main(["--root", "D:/missing", "--volume-guid", VOLUME]) == 5


def test_tampered_writable_unit_cannot_raise_protected_job_limits(monkeypatch):
    captured = {}
    monkeypatch.setattr(jobhost, "packaged", lambda: False)
    monkeypatch.setattr(jobhost, "read_unit", lambda *a: {
        "memory_bytes": None, "cpus": None, "cpuset": None, "env": {"MARKER": "kept"}})

    class FakeJob:
        def __init__(self, **limits):
            captured.update(limits)

        def enter(self):
            pass

    class Child:
        _handle = 1

        def wait(self, **kwargs):
            return 0

    def spawn(*args, **kwargs):
        assert kwargs["env"]["MARKER"] == "kept"
        return Child()

    monkeypatch.setattr(jobhost, "Job", FakeJob)
    monkeypatch.setattr(jobhost.subprocess, "Popen", spawn)
    monkeypatch.setattr(jobhost, "in_job", lambda *a: True)
    protected = {"memory_bytes": 1024**3, "cpus": 2, "cpuset": "0-3"}
    assert jobhost.main(["--root", "D:/unused", "--limits-json", json.dumps(protected)]) == 0
    assert captured == protected


@pytest.mark.parametrize("change", [{"memory_bytes": True}, {"memory_bytes": 0},
                                   {"cpus": float("nan")}, {"cpus": float("inf")},
                                   {"cpus": -1}, {"cpuset": "0-64"}, {"cpuset": "-1"}])
def test_invalid_protected_limits_fail_before_reading_unit(monkeypatch, change):
    monkeypatch.setattr(jobhost, "read_unit", lambda *a: pytest.fail("read unsafe unit"))
    limits = dict(memory_bytes=1024**3, cpus=1, cpuset=None, **{})
    limits.update(change)
    assert jobhost.main(["--root", "D:/unused", "--limits-json", json.dumps(limits)]) == 6


class Disks:
    def __init__(self):
        self.events = []
        self.error = None

    def _call(self, action, rid):
        self.events.append(action)
        if self.error:
            raise RuntimeError(self.error)
        return {"volume_guid": VOLUME, "capacity_bytes": 1000,
                "virtual_bytes": 1100, "free_bytes": 200}

    def ensure(self, rid, limit):
        return self._call("ensure", rid)

    def mount(self, rid):
        return self._call("mount", rid)

    def verify(self, rid):
        return self._call("verify", rid)

    def remove(self, rid):
        return self._call("remove", rid)


@pytest.fixture
def managed():
    host, disks = FakeWindows(), Disks()
    runtime = WindowsProcessRuntime(run=host, fs=host, tools=TOOLS, storage_backend=disks)
    return runtime, host, disks


def test_mount_failure_prevents_directory_creation_and_service_start(managed):
    runtime, host, disks = managed
    disks.error = "mount unavailable"
    with pytest.raises(RuntimeError):
        runtime.create(RID, {"image": TEMPLATE})
    assert not host.exists(runtime.paths(RID)["root"])
    assert not host.services


def test_service_identity_is_configured_before_executable_files_are_copied(managed):
    runtime, host, disks = managed
    copy = host.copytree

    def guarded_copy(src, dst):
        service = host.services[naming.unit_name(RID)]
        assert service["account"] == f"NT SERVICE\\rnr-{RID}"
        assert service["start"] == "demand"
        return copy(src, dst)

    host.copytree = guarded_copy
    runtime.create(RID, {"image": TEMPLATE})


def test_legacy_runtime_refuses_a_requested_unenforced_disk_limit():
    host = FakeWindows()
    runtime = WindowsProcessRuntime(run=host, fs=host, tools=TOOLS)
    with pytest.raises(ValueError, match="requires configured"):
        runtime.create(RID, {"image": TEMPLATE, "disk_limit": 1024**3})
    assert not host.services


def test_factory_shares_volume_guard_with_registrar():
    from agent.__main__ import runtime_for
    runtime, registrar = runtime_for(SimpleNamespace(
        runtime="windows-process", tools={}, windows_storage={"enabled": True}))
    assert runtime._storage is not None
    assert registrar._storage is runtime._storage


def test_volume_identity_is_protected_in_service_argv_and_start_stays_demand(managed):
    runtime, host, disks = managed
    runtime.create(RID, {"image": TEMPLATE})
    service = host.services[naming.unit_name(RID)]
    assert service["args"][-2:] == ["--volume-guid", VOLUME]
    assert VOLUME in service["settings"]["AppParameters"][0]
    runtime.stop(RID)
    runtime.start(RID)
    assert service["start"] == "demand"
    assert "mount" in disks.events
    unit = json.loads(host.read_text(runtime.paths(RID)["reg"] + r"\unit.json"))
    assert "volume_guid" not in unit
    assert unit["env"]["USERPROFILE"] == runtime.paths(RID)["work"]
    assert unit["env"]["LOCALAPPDATA"] == runtime.paths(RID)["cache"]


def test_missing_volume_keeps_stopped_state_but_blocks_start_and_cache_deletion(managed):
    runtime, host, disks = managed
    runtime.create(RID, {"image": TEMPLATE})
    runtime.stop(RID)
    disks.error = "missing disk"
    assert runtime.status(RID)["running"] is False
    assert runtime.status(RID)["storage_ready"] is False
    with pytest.raises(RuntimeError):
        runtime.start(RID)
    with pytest.raises(RuntimeError):
        runtime.clear_cache(RID, {})
    assert host.services[naming.unit_name(RID)]["state"] == "stopped"
    assert runtime.telemetry(RID)["disk_used_bytes"] is None


def test_keep_data_retains_volume_workspace_cache_and_logs(managed):
    runtime, host, disks = managed
    runtime.create(RID, {"image": TEMPLATE})
    paths = runtime.paths(RID)
    for area in ("work", "cache", "logs"):
        host.put(ntpath.join(paths[area], "retained"), 100)
    runtime.remove(RID, keep_data=True)
    assert "remove" not in disks.events
    for area in ("work", "cache", "logs"):
        assert host.exists(ntpath.join(paths[area], "retained"))
    assert not host.exists(paths["reg"])


def test_full_remove_uses_storage_backend_never_recursive_mount_deletion(managed):
    runtime, host, disks = managed
    runtime.create(RID, {"image": TEMPLATE})
    host.rmtree = lambda *a: pytest.fail("must not recurse through mountpoint")
    runtime.remove(RID, keep_data=False)
    assert "remove" in disks.events
    assert host.services == {}
    runtime.remove(RID, keep_data=False)


def test_registrar_refuses_an_unverified_volume_before_running_template(managed):
    runtime, host, disks = managed
    runtime.create(RID, {"image": TEMPLATE})
    registrar = WindowsRegistrar(run=host, fs=host, tools=TOOLS, storage_backend=disks)
    before = list(host.calls)
    disks.error = "missing volume"
    with pytest.raises(RuntimeError, match="missing volume"):
        registrar.register(RID, {"token": "must not be sent"})
    with pytest.raises(RuntimeError, match="missing volume"):
        registrar.deregister(RID)
    assert host.calls == before


@pytest.fixture
def helper(tmp_path):
    if sys.platform != "win32":
        pytest.skip("actual PowerShell helper runs on Windows")
    model_path = tmp_path / "model.json"
    model = {"type": "Fixed", "limit": 1024**3, "vhd_id": "vhd-id", "disk_id": "disk-id",
             "attached": False, "style": "RAW", "partition": False,
             "partition_id": "partition-id", "volume_guid": VOLUME,
             "fs": "Unknown", "label": "", "access": [VOLUME],
             "host_free": 10 * 1024**3, "calls": [], "reparse_area": ""}
    model_path.write_text(json.dumps(model))
    request = {"action": "ensure", "runner_id": RID,
               "root": str(tmp_path / "images"), "runner_root": str(tmp_path / "runners"),
               "limit": 1024**3, "reserve_bytes": 1024**3}

    def invoke(action="ensure", fail=None, **changes):
        env = dict(os.environ, RNR_STORAGE_MODEL=str(model_path),
                   RNR_STORAGE_HELPER=str(ROOT / "agent/runtimes/windows_storage.ps1"),
                   RNR_STORAGE_FAIL=fail or "")
        return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(Path(__file__).with_name("windows_storage_mock.ps1"))],
                              input=json.dumps(dict(request, action=action, **changes)),
                              capture_output=True, text=True, env=env, timeout=30)

    return invoke, request, model_path


def _model(path, **changes):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if changes:
        value.update(changes)
        path.write_text(json.dumps(value))
    return value


@pytest.mark.parametrize("failure", [None, "create", "attach", "initialize", "partition", "format", "mount", "protect-volume"])
def test_actual_helper_resumes_each_creation_stage_without_reformatting(helper, failure):
    invoke, request, model = helper
    first = invoke(fail=failure)
    assert first.returncode == (1 if failure else 0), first.stderr
    retry = invoke()
    assert retry.returncode == 0, retry.stderr
    result = json.loads(retry.stdout)
    assert result["volume_guid"] == VOLUME
    assert result["capacity_bytes"] < result["virtual_bytes"]
    calls = _model(model)["calls"]
    for operation in ("create", "initialize", "partition", "format"):
        assert calls.count(operation) == 1, calls


def test_actual_helper_refuses_plain_existing_directory_and_low_space(helper):
    invoke, request, model = helper
    mount = Path(request["runner_root"]) / RID
    mount.mkdir(parents=True)
    answer = invoke()
    assert answer.returncode == 1 and "offline migration" in answer.stderr
    assert not _model(model)["calls"]
    mount.rmdir()
    _model(model, host_free=1024**3)
    answer = invoke()
    assert answer.returncode == 1 and "Insufficient physical" in answer.stderr
    assert not (Path(request["root"]) / (RID + ".vhdx")).exists()
    removed = invoke("remove")
    assert removed.returncode == 0, removed.stderr
    assert not (Path(request["root"]) / (RID + ".json")).exists()


@pytest.mark.parametrize("changed", [{"vhd_id": "other-image"}, {"disk_id": "other-disk"},
                                      {"partition_id": "other-partition"},
                                      {"volume_guid": VOLUME.replace("a123", "b123")}])
def test_actual_helper_refuses_identity_substitution_before_mutation(helper, changed):
    invoke, request, model = helper
    assert invoke().returncode == 0
    before = _model(model)["calls"]
    _model(model, **changed)
    for action in ("verify", "mount", "remove"):
        answer = invoke(action)
        assert answer.returncode == 1, answer.stdout
    assert _model(model)["calls"] == before


def test_actual_helper_retains_size_and_remounts_without_format(helper):
    invoke, request, model = helper
    assert invoke().returncode == 0
    resized = invoke(limit=2 * 1024**3)
    assert resized.returncode == 1 and "offline resize" in resized.stderr
    _model(model, attached=False, access=[VOLUME])
    assert invoke("verify").returncode == 1
    mounted = invoke("mount", limit=None)
    assert mounted.returncode == 0, mounted.stderr
    assert _model(model)["calls"].count("format") == 1


def test_actual_helper_refuses_an_additional_volume_mount_before_deleting(helper):
    invoke, request, model = helper
    assert invoke().returncode == 0
    before = _model(model)
    _model(model, access=before["access"] + ["Q:\\"])
    for action in ("verify", "mount", "remove"):
        answer = invoke(action)
        assert answer.returncode == 1 and "unexpected access paths" in answer.stderr
    assert _model(model)["calls"] == before["calls"]


def test_actual_helper_refuses_a_redirected_cache_area(helper):
    invoke, request, model = helper
    assert invoke().returncode == 0
    cache = Path(request["runner_root"]) / RID / "cache"
    cache.mkdir()
    _model(model, reparse_area="cache")
    answer = invoke("verify")
    assert answer.returncode == 1 and "must not redirect" in answer.stderr


@pytest.mark.parametrize("failure", [None, "unmount", "detach"])
def test_actual_helper_removal_resumes_and_is_idempotent(helper, failure):
    invoke, request, model = helper
    assert invoke().returncode == 0
    answer = invoke("remove", fail=failure)
    assert answer.returncode == (1 if failure else 0), answer.stderr
    retry = invoke("remove")
    assert retry.returncode == 0, retry.stderr
    assert not (Path(request["root"]) / (RID + ".vhdx")).exists()
    assert not (Path(request["root"]) / (RID + ".json")).exists()


def test_actual_helper_lets_runner_accounts_list_the_mount_parent_only(helper):
    """The GitHub runner refuses to start unless it can list every directory
    above its own (IOUtil.ValidateExecutePermission). With D:\runners open
    to SYSTEM and Administrators only, its config.cmd failed with "Access to
    the path 'D:\runners' is denied" (2026-09-22). Service accounts may list
    and traverse that one directory - not inherited, so no runner reaches
    into another's volume, which keeps an ACL of its own."""
    invoke, request, model = helper
    assert invoke().returncode == 0
    acls = _model(model)["acls"]
    parent = acls[str(Path(request["runner_root"]))]
    mount = acls[str(Path(request["runner_root"]) / RID)]
    service_aces = [a for a in parent.split("(") if "S-1-5-80-0" in a]
    assert len(service_aces) == 1, parent
    ace = service_aces[0]
    assert ace.startswith("A;;"), ace          # allow, and no inheritance flags
    assert "S-1-5-80-0" not in mount, mount


def test_a_verify_waits_long_enough_for_another_short_check():
    """The heartbeat verifies every runner's disk; with a one-second wait a
    registration's own verify lost that race and failed (2026-09-22). It
    must still give up well before a disk creation, which holds the lock
    for minutes, and within the caller's own deadline."""
    text = (ROOT / "agent/runtimes/windows_storage.ps1").read_text(encoding="utf-8")
    wait = re.search(r"-eq 'verify'\) \{ (\d+) \}", text)
    assert wait and 5000 <= int(wait.group(1)) <= 15000, wait
    source = (ROOT / "agent/runtimes/windows_storage.py").read_text(encoding="utf-8")
    deadline = re.search(r'timeout=(\d+) if action == "verify"', source)
    assert deadline and int(deadline.group(1)) * 1000 >= int(wait.group(1)) + 10000
