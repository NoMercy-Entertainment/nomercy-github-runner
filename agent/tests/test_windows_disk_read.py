import json
import ntpath
import struct
import uuid

import pytest

from agent.runtimes import windows_disk_read as read
from agent.runtimes.windows_storage import WindowsStorage

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
ROOT = r"D:\runner-disks"
PARTITION = "a1234567-1234-1234-1234-123456789abc"


def manifest():
    return dict(version=1, runner_id=RID, image=ntpath.join(ROOT, RID + ".vhdx"),
                mount=ntpath.join(read.naming.WINDOWS_ROOT, RID) + "\\",
                stage="ready", type="Fixed", volume_acl=True, limit=100 * 1024**3,
                disk_id="60022480D782524FC5D7C81291B1F0C7", partition_id=PARTITION,
                volume_guid="\\\\?\\Volume{" + PARTITION + "}\\")


@pytest.mark.parametrize("change", [dict(version=True), dict(runner_id="other"),
    dict(image=r"C:\host.vhdx"), dict(mount="C:\\host\\"), dict(stage="creating"),
    dict(type="Dynamic"), dict(volume_acl=False), dict(limit=True), dict(limit=1024**3+1),
    dict(disk_id=""), dict(disk_id="not-an-identity"), dict(volume_guid="C:\\"),
    dict(partition_id="bad"), dict(partition_id=None)])
def test_inconsistent_manifests_cannot_authorize_a_measurement(change):
    state = manifest()
    state.update(change)
    with pytest.raises(RuntimeError):
        read._validate(state, ROOT, RID)


@pytest.mark.parametrize("state", [None, [], 1, True, "manifest"])
def test_wrong_manifest_type_is_refused(state):
    with pytest.raises(RuntimeError):
        read._validate(state, ROOT, RID)


def test_valid_manifest_and_duplicate_fields():
    state = manifest()
    assert read._validate(state, ROOT, RID) is state
    with pytest.raises(RuntimeError, match="duplicate"):
        json.loads('{"version":1,"version":1}', object_pairs_hook=read._unique)


def test_device_identity_descriptor_is_bounded_and_device_associated():
    identity = bytes.fromhex(manifest()["disk_id"])
    descriptor = struct.pack("<III", 1, 28 + len(identity), 1)
    descriptor += struct.pack("<IIHHI", 1, 3, len(identity), 0, 0) + identity
    assert read._identifiers(descriptor) == {identity.hex().upper()}
    port_identity = bytearray(descriptor)
    struct.pack_into("<I", port_identity, 24, 1)
    assert read._identifiers(port_identity) == set()
    for bad in (descriptor[:11], descriptor[:-1], struct.pack("<III", 1, 12, 65)):
        with pytest.raises(RuntimeError):
            read._identifiers(bad)


def test_gpt_partition_must_be_unique_and_match_the_manifest():
    layout = bytearray(48 + 144)
    struct.pack_into("<II", layout, 0, 1, 1)
    struct.pack_into("<I", layout, 48, 1)
    struct.pack_into("<qq", layout, 56, 1048576, 100 * 1024**3 - 1048576)
    layout[96:112] = uuid.UUID(PARTITION).bytes_le
    assert read._partition(layout, PARTITION)[0] == 1048576
    with pytest.raises(RuntimeError, match="missing"):
        read._partition(layout, str(uuid.uuid4()))
    duplicate = bytearray(layout + layout[48:])
    struct.pack_into("<I", duplicate, 4, 2)
    with pytest.raises(RuntimeError, match="ambiguous"):
        read._partition(duplicate, PARTITION)


def test_each_telemetry_measurement_is_fresh_and_never_launches_powershell(monkeypatch):
    values = iter([200, 300])
    monkeypatch.setattr(read, "measure", lambda root, rid: {"free_bytes": next(values)})
    storage = WindowsStorage({}, lambda *a, **kw: pytest.fail("no shell for measurement"), "pwsh")
    assert storage.measure(RID)["free_bytes"] == 200
    assert storage.measure(RID)["free_bytes"] == 300
