"""Owned hard disk budgets, safe interruption recovery and Docker binding."""
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import uuid

import pytest

from agent import naming
from agent.runtimes.linux_container import LinuxContainerRuntime
from agent.runtimes.linux_storage import LinuxStorage, StorageError, MIN_BYTES
from agent.tests.fake_docker import FakeDocker

RID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class DiskCommands:
    def __init__(self):
        self.calls, self.filesystems, self.mounts = [], {}, {}
        self.fail = None
        self.foreign_backing = None
        self.children = False

    def __call__(self, argv, timeout=120):
        self.calls.append(argv)
        verb = argv[0]
        if self.fail == verb:
            return 2, "", "injected failure"
        if verb == "mkfs.ext4":
            self.filesystems[argv[-1]] = argv[argv.index("-U") + 1]
            return 0, "", ""
        if verb == "blkid":
            rid = self.filesystems.get(argv[-1])
            return (0, f"TYPE=ext4\nUUID={rid}", "") if rid else (2, "", "not a filesystem")
        if verb == "mount":
            self.mounts[argv[-1]] = argv[-2]
            return 0, "", ""
        if verb == "findmnt":
            path = argv[argv.index("--mountpoint") + 1]
            if path not in self.mounts:
                return 1, "", ""
            index = list(self.mounts).index(path)
            row = {"target": path, "source": f"/dev/loop{index}", "fstype": "ext4", "options": "rw,nodev,nosuid"}
            if "--submounts" in argv and self.children:
                row["children"] = [{"target": str(Path(path) / "foreign")}]
            return 0, json.dumps({"filesystems": [row]}), ""
        if verb == "losetup":
            if "--associated" in argv:
                return 0, json.dumps({"loopdevices": list(self.mounts)}), ""
            index = int(argv[-1].removeprefix("/dev/loop"))
            return 0, json.dumps({"loopdevices": [{"name": argv[-1],
                                 "back-file": self.foreign_backing or list(self.mounts.values())[index],
                                 "offset": 0, "sizelimit": 0, "ro": False}]}), ""
        if verb == "umount":
            self.mounts.pop(argv[-1])
            # Model the empty host mountpoint exposed by unmounting.
            for path in Path(argv[-1]).iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            return 0, "", ""
        if verb == "find":
            for path in Path(argv[1]).iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            return 0, "", ""
        raise AssertionError(argv)


@pytest.fixture
def disk(tmp_path):
    commands = DiskCommands()
    return LinuxStorage(str(tmp_path / "owned"), default_bytes=MIN_BYTES, run=commands), commands


def provision(store, rid=RID, limit=None):
    with store.lock():
        return store.ensure(rid, limit)


def test_all_five_areas_share_one_fixed_uuid_filesystem(disk):
    store, commands = disk
    paths = provision(store)
    assert set(paths) == set(naming.AREAS)
    assert len({Path(path).parent for path in paths.values()}) == 1
    assert store._paths(RID)[2].stat().st_size == MIN_BYTES
    assert commands.filesystems[str(store._paths(RID)[2])] == RID
    Path(paths["work"], "build").write_text("keep me")
    assert provision(store) == paths
    assert Path(paths["work"], "build").read_text() == "keep me"
    assert len([call for call in commands.calls if call[0] == "mkfs.ext4"]) == 1


def test_independent_storage_instances_serialize_mutations(disk):
    store, commands = disk
    other = LinuxStorage(str(store.root), default_bytes=MIN_BYTES, run=commands)
    started, entered = threading.Event(), threading.Event()
    errors = []
    def competing():
        started.set()
        try:
            with other.lock():
                entered.set()
                other.ensure(RID)
        except Exception as error:
            errors.append(error)
    with store.lock():
        worker = threading.Thread(target=competing)
        worker.start()
        assert started.wait(2)
        assert not entered.wait(0.1)
    worker.join(5)
    assert not worker.is_alive()
    assert entered.is_set() and not errors
    assert len([call for call in commands.calls if call[0] == "mkfs.ext4"]) == 1


def test_other_runners_and_changed_limits_cannot_take_over_storage(disk):
    store, commands = disk
    first = provision(store)
    second = provision(store, OTHER)
    assert set(first.values()).isdisjoint(second.values())
    with pytest.raises(StorageError, match="offline migration"):
        provision(store, limit=MIN_BYTES * 2)
    assert len([call for call in commands.calls if call[0] == "mkfs.ext4"]) == 2


@pytest.mark.parametrize("rid", ["../../etc", "/etc", "not-a-uuid", RID + "/../../etc"])
def test_invalid_runner_ids_cannot_address_host_paths(disk, rid):
    store, commands = disk
    with pytest.raises(ValueError):
        provision(store, rid)
    assert not commands.calls


def test_preexisting_unidentified_data_is_never_formatted(disk):
    store, commands = disk
    with store.lock():
        base = store._paths(RID)[0]
        base.mkdir(mode=0o700)
        (base / "previous-data").write_text("keep")
        with pytest.raises(StorageError, match="unidentified"):
            store.ensure(RID)
        assert (base / "previous-data").read_text() == "keep"
    assert not commands.calls


@pytest.mark.parametrize("failure", ["mkfs.ext4", "mount"])
def test_partial_create_never_reformats_an_existing_image(disk, failure):
    store, commands = disk
    commands.fail = failure
    with pytest.raises(StorageError):
        provision(store)
    commands.fail = None
    if failure == "mount":
        provision(store)
    else:
        with pytest.raises(StorageError, match="blkid"):
            provision(store)
    assert len([call for call in commands.calls if call[0] == "mkfs.ext4"]) == 1


def test_wrong_filesystem_or_loop_backing_fails_closed(disk):
    store, commands = disk
    provision(store)
    commands.foreign_backing = "/someone/elses/image"
    with pytest.raises(StorageError, match="different image"):
        provision(store)
    commands.foreign_backing = None
    commands.filesystems[str(store._paths(RID)[2])] = OTHER
    with pytest.raises(StorageError, match="UUID"):
        provision(store)


def test_registration_reset_preserves_previous_work_and_cache(disk):
    store, commands = disk
    paths = provision(store)
    for area, path in paths.items():
        Path(path, "previous").write_text(area)
    with store.lock():
        store.reset_registration(RID)
    assert not list(Path(paths["reg"]).iterdir())
    assert all(Path(paths[area], "previous").read_text() == area for area in ("work", "cache", "docker", "logs"))


def test_nested_mounts_and_failed_unmount_prevent_deletion(disk):
    store, commands = disk
    provision(store)
    commands.children = True
    with store.lock(), pytest.raises(StorageError, match="nested mounts"):
        store.remove(RID)
    commands.children = False
    commands.fail = "umount"
    with store.lock(), pytest.raises(StorageError, match="umount"):
        store.remove(RID)
    assert store._paths(RID)[2].exists()


def test_remove_is_idempotent_and_only_deletes_the_owned_files(disk):
    store, commands = disk
    provision(store)
    outside = store.root.parent / "unrelated"
    outside.write_text("keep")
    with store.lock():
        store.remove(RID)
        store.remove(RID)
    assert not store._paths(RID)[0].exists()
    assert outside.read_text() == "keep"


def test_cleanup_resumes_after_successful_unmount_before_image_deletion(disk):
    store, commands = disk
    provision(store)
    commands(["umount", str(store._paths(RID)[3])])
    with store.lock():
        store.remove(RID)
    assert not store._paths(RID)[0].exists()


def test_telemetry_reads_whole_filesystem_without_creating_storage(disk, monkeypatch):
    from types import SimpleNamespace
    store, commands = disk
    with pytest.raises(StorageError):
        store.telemetry(RID)
    assert not store.root.exists()
    provision(store)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(total=MIN_BYTES - 1024, used=8192, free=MIN_BYTES - 9216))
    result = store.telemetry(RID)
    assert result["disk_limit_enforced"]
    assert result["disk_limit_bytes"] == MIN_BYTES
    assert result["disk_used_bytes"] == 8192
    assert result["disk_usable_bytes"] == MIN_BYTES - 1024


def test_boot_validates_every_existing_image_and_never_formats_unknown_data(disk):
    store, commands = disk
    provision(store)
    count = len(commands.calls)
    store.mount_all()
    assert not any(call[0] == "mkfs.ext4" for call in commands.calls[count:])
    (store.root / "unidentified").mkdir()
    with pytest.raises(ValueError):
        store.mount_all()


@pytest.mark.skipif(os.name == "nt", reason="creating test symlinks requires Windows privilege")
@pytest.mark.parametrize("part", ["root", "runner", "image", "area"])
def test_symlinks_cannot_redirect_owned_storage(disk, part, tmp_path):
    store, commands = disk
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_text("keep")
    if part == "root":
        store.root.symlink_to(outside, target_is_directory=True)
    elif part == "runner":
        with store.lock():
            store._paths(RID)[0].symlink_to(outside, target_is_directory=True)
    else:
        paths = provision(store)
        path = store._paths(RID)[2] if part == "image" else Path(paths["reg"])
        path.unlink() if path.is_file() else path.rmdir()
        path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        provision(store)
    assert (outside / "important").read_text() == "keep"


class BoundDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.volume_info = {}
        self.readonly_compatible = True

    def _image(self, args, input):
        # None models an image that never declared the label at all - the
        # legacy images a writable-root worker is allowed to keep using.
        labels = ({} if self.readonly_compatible is None else
                 {"nomercy.readonly_root": str(self.readonly_compatible).lower()})
        return True, json.dumps({"Id": "sha256:" + "a" * 64,
                                "Config": {"Labels": labels}}), ""

    def _run(self, args, input):
        args = list(args)
        readonly = "--read-only" in args
        if readonly:
            args.remove("--read-only")
        tmpfs = {}
        if "--tmpfs" in args:
            index = args.index("--tmpfs")
            key, value = args[index + 1].split(":", 1)
            tmpfs[key] = value
            del args[index:index + 2]
        result = super()._run(args, input)
        if result[0]:
            self.containers[args[args.index("--name") + 1]]["hostconfig"] = {
                "ReadonlyRootfs": readonly, "Tmpfs": tmpfs}
        return result

    def _inspect(self, args, input):
        if "{{json .}}" in args and args[-1] in self.containers:
            unit = self.containers[args[-1]]
            return True, json.dumps({"Mounts": [{"Type": "volume", "Name": volume, "Destination": path}
                                                for volume, path in unit["mounts"].items()],
                                     "HostConfig": unit["hostconfig"]}), ""
        return super()._inspect(args, input)

    def _volume(self, args, input):
        if args[0] == "inspect":
            name = args[-1]
            if name not in self.volumes:
                return False, "", "No such volume"
            return True, json.dumps([self.volume_info.get(name, {"Name": name, "Driver": "local"})]), ""
        result = super()._volume(args, input)
        if result[0] and args[0] == "create":
            options, labels = {}, {}
            for index, value in enumerate(args[:-1]):
                if value in ("--opt", "--label"):
                    key, val = args[index + 1].split("=", 1)
                    (options if value == "--opt" else labels)[key] = val
            self.volume_info[args[-1]] = {"Name": args[-1], "Driver": "local", "Options": options, "Labels": labels}
        return result


def test_runtime_binds_volumes_and_recreate_preserves_work(disk):
    store, commands = disk
    docker = BoundDocker()
    runtime = LinuxContainerRuntime(run=docker, storage=store)
    runtime.create(RID, {"image": "unit:v1", "disk_limit": MIN_BYTES})
    paths = {area: Path(info["Options"]["device"]) for area, info in
             ((area, docker.volume_info[name]) for area, name in naming.names(RID).items())}
    for area, path in paths.items():
        (path / "old").write_text(area)
    runtime.remove(RID, keep_data=True)
    assert (paths["work"] / "old").read_text() == "work"
    assert (paths["cache"] / "old").read_text() == "cache"
    assert not list(paths["reg"].iterdir())
    runtime.create(RID, {"image": "unit:v2", "disk_limit": MIN_BYTES})
    assert (paths["work"] / "old").read_text() == "work"
    runtime.remove(RID, keep_data=False)
    assert not docker.volumes
    assert not store._paths(RID)[0].exists()


def test_legacy_volume_refuses_automatic_migration(disk):
    store, commands = disk
    docker = BoundDocker()
    docker.put(naming.names(RID)["work"], "important", 100)
    runtime = LinuxContainerRuntime(run=docker, storage=store)
    with pytest.raises(RuntimeError, match="migration"):
        runtime.create(RID, {"image": "unit:v1"})
    assert docker.volume_bytes(naming.names(RID)["work"]) == 100
    assert not commands.calls


def test_failed_volume_removal_keeps_the_mounted_filesystem(disk, monkeypatch):
    store, commands = disk
    docker = BoundDocker()
    runtime = LinuxContainerRuntime(run=docker, storage=store)
    runtime.create(RID, {"image": "unit:v1"})
    volume = docker._volume
    def cannot_remove(args, input):
        if args[0] == "rm":
            return False, "", "volume still in use"
        return volume(args, input)
    monkeypatch.setattr(docker, "_volume", cannot_remove)
    with pytest.raises(RuntimeError, match="storage left behind"):
        runtime.remove(RID, keep_data=False)
    assert store._paths(RID)[2].exists()
    assert str(store._paths(RID)[3]) in commands.mounts
    assert not any(call[0] == "umount" for call in commands.calls)


def test_default_runtime_does_not_claim_disk_enforcement():
    assert LinuxContainerRuntime(run=BoundDocker()).capabilities()["disk_limit_enforced"] is False


def test_incompatible_image_is_refused_before_allocating_storage(disk):
    store, commands = disk
    docker = BoundDocker()
    docker.readonly_compatible = False
    runtime = LinuxContainerRuntime(run=docker, storage=store)
    with pytest.raises(RuntimeError, match="readonly_root=true"):
        runtime.create(RID, {"image": "legacy:v1"})
    assert not docker.volumes
    assert not commands.calls


def test_managed_unit_and_stopped_maintenance_have_readonly_roots(disk):
    store, commands = disk
    docker = BoundDocker()
    runtime = LinuxContainerRuntime(run=docker, storage=store)
    runtime.create(RID, {"image": "unit:v1", "disk_limit": MIN_BYTES})
    name = naming.unit_name(RID)
    assert docker.containers[name]["hostconfig"]["ReadonlyRootfs"]
    assert docker.containers[name]["hostconfig"]["Tmpfs"]["/run"].startswith("rw,nosuid,nodev,size=64m")
    runtime.stop(RID)
    runtime.clear_cache(RID, {"scopes": ["temp"]})
    helper = [call for call in docker.calls if call[0] == "run" and "--entrypoint" in call]
    assert helper and "--read-only" in helper[0] and "--tmpfs" in helper[0]
    assert docker.containers[name]["state"] == "exited"


def test_readonly_root_defaults_true_and_is_read_from_a_storage_config_dict(tmp_path):
    """Production wires a config dict (agent/config.py's `storage` mapping)
    straight into the runtime, which must pop `readonly_root` out of it
    before handing the rest to LinuxStorage - LinuxStorage takes no such
    keyword, so leaving it in would blow up every writable-root worker."""
    root = str(tmp_path / "owned")
    default = LinuxContainerRuntime(run=FakeDocker(),
                                    storage={"root": root, "default_bytes": MIN_BYTES})
    assert default._readonly_root is True
    assert default._storage.root == Path(root)

    writable = LinuxContainerRuntime(run=FakeDocker(), storage={
        "root": root, "default_bytes": MIN_BYTES, "readonly_root": False})
    assert writable._readonly_root is False
    assert writable._storage.root == Path(root)


def test_writable_root_create_has_no_readonly_argv_and_accepts_an_undeclared_image(disk):
    store, commands = disk
    docker = BoundDocker()
    docker.readonly_compatible = None  # the image never declared the label
    runtime = LinuxContainerRuntime(run=docker, storage=store, readonly_root=False)
    runtime.create(RID, {"image": "unit:v1", "disk_limit": MIN_BYTES})
    name = naming.unit_name(RID)
    run_call = next(call for call in docker.calls
                    if call[0] == "run" and "--name" in call)
    assert "--read-only" not in run_call
    assert "--tmpfs" not in run_call
    assert docker.containers[name]["hostconfig"]["ReadonlyRootfs"] is False
    assert docker.containers[name]["hostconfig"]["Tmpfs"] == {}
    # The existing-container check (start/restart/maintenance) accepts it too.
    runtime.start(RID)


def test_writable_root_maintenance_helper_has_no_readonly_argv(disk):
    store, commands = disk
    docker = BoundDocker()
    docker.readonly_compatible = None
    runtime = LinuxContainerRuntime(run=docker, storage=store, readonly_root=False)
    runtime.create(RID, {"image": "unit:v1", "disk_limit": MIN_BYTES})
    runtime.stop(RID)
    runtime.clear_cache(RID, {"scopes": ["temp"]})
    helper = [call for call in docker.calls if call[0] == "run" and "--entrypoint" in call]
    assert helper and "--read-only" not in helper[0] and "--tmpfs" not in helper[0]


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("RUNNER_LOOP_TEST") != "1",
                    reason="opt-in Linux root loop-device integration")
def test_real_small_loop_filesystem_limit(tmp_path):
    store = LinuxStorage(str(tmp_path / "real"), default_bytes=MIN_BYTES)
    rid = str(uuid.uuid4())
    try:
        paths = provision(store, rid)
        usage = store.telemetry(rid)
        assert usage["disk_limit_enforced"]
        assert usage["disk_usable_bytes"] <= MIN_BYTES
        with pytest.raises(OSError):
            with open(Path(paths["work"]) / "fill", "wb") as handle:
                for _ in range(80):
                    handle.write(b"x" * 1024 ** 2)
        assert store.telemetry(rid)["disk_used_bytes"] > 0
    finally:
        with store.lock():
            store.remove(rid)
