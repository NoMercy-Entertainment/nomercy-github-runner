"""Fresh-create trust, launch ordering and real descriptor installer boundaries."""
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from agent.runtimes import macos_native as native
from agent.runtimes import macos_pool
from .test_macos_pool import host, pool, native_fixture, RID, SPEC

KEY = "ssh-ed25519 " + base64.b64encode(struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"k" * 32).decode()


def test_missing_trusted_base_evidence_refuses_before_any_owned_allocation(pool, host, monkeypatch):
    def missing(*args):
        raise RuntimeError("protected base attestation missing")
    monkeypatch.setattr(macos_pool, "NativeProvisioner", missing)
    before = list(host.calls)
    with pytest.raises(RuntimeError, match="attestation missing"):
        pool.create(RID, SPEC)
    assert host.calls == before and not pool._directory(RID).exists()


def test_installer_failure_keeps_first_listener_disabled_and_retry_installs_before_start(pool, host, monkeypatch):
    calls = []
    fail = [True]
    class Installer:
        def __init__(self, *args):
            pass
        def pin(self, record, directory):
            calls.append("pin")
        def install(self, guest, record, paths, runner_user):
            mac = host.guests[record["runner_id"]]
            assert not pool._context(record)[1].status(RID).get("running")
            assert pool._read(RID)["listener_disabled"] is True
            calls.append("install")
            if fail[0]:
                raise RuntimeError("native payload unavailable")
            mac.files[paths["reg"] + "/forgejo-runner"] = "verified-v2"
            return {paths["reg"] + "/forgejo-runner": "v2"}
    monkeypatch.setattr(macos_pool, "NativeProvisioner", Installer)
    before = pool.base_disk.read_bytes()
    with pytest.raises(RuntimeError, match="payload unavailable"):
        pool.create(RID, SPEC)
    assert pool._read(RID)["initialized"] is False
    assert pool._read(RID)["listener_disabled"] is True
    assert pool.status(RID)["running"] is False
    fail[0] = False
    pool.create(RID, SPEC)
    assert calls == ["pin", "install", "pin", "install"]
    assert pool.status(RID)["running"] is True
    assert pool.base_disk.read_bytes() == before
    assert pool._read(RID)["native_files"]
    before_files = dict(host.guests[RID].files)
    before_calls = list(calls)
    pool.create(RID, SPEC)
    assert host.guests[RID].files == before_files and calls == before_calls


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"configured immutable base")
    artifact_root = tmp_path / "artifacts"
    binary = b"reviewed-v2-binary"
    stream = b"management-owned-stream"
    monkeypatch.setattr(native, "base_identity", lambda p: [Path(p).stat().st_size, str(Path(p))])
    value = {"version": 1, "image": "sha256:" + "a" * 64, "base_disk": str(base),
             "base_identity": native.base_identity(base), "architecture": "amd64",
             "host_key": KEY, "artifact_root": str(artifact_root)}
    def read(path, maximum):
        if str(path).endswith(".native.json"):
            return json.dumps(value).encode()
        if Path(path).name == "job_event_stream.py":
            return stream
        return binary
    monkeypatch.setattr(native, "protected_read", read)
    monkeypatch.setitem(native.BINARY_HASHES, "amd64", hashlib.sha256(binary).hexdigest())
    return SimpleNamespace(base=base, value=value, binary=binary, stream=stream)


@pytest.mark.parametrize("field,value", [("image", "sha256:" + "b" * 64), ("base_disk", "/wrong/base.qcow2"),
                                         ("base_identity", [0] * 5), ("host_key", "ssh-ed25519 wrong")])
def test_manifest_is_bound_to_exact_configured_base_and_public_key(manifest, field, value):
    image = manifest.value["image"]
    manifest.value[field] = value
    with pytest.raises(RuntimeError):
        native.NativeProvisioner(manifest.base, image)


def test_mutated_artifact_refused_and_guest_install_transports_bytes_without_staged_code(manifest, monkeypatch):
    provisioner = native.NativeProvisioner(manifest.base, manifest.value["image"])
    record = {"runner_id": RID, "template": "forgejo-runner-v13.1.0-macos-r20260921"}
    paths = {"reg": "/Users/runner/runners/" + RID + "/reg"}
    seen = []
    def guest(argv, *, input, timeout):
        assert argv[:6] == ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c"]
        assert argv[6] == native.INSTALL and timeout == 300
        payload = json.loads(input)
        if payload["action"] == "install":
            assert base64.b64decode(payload["files"][paths["reg"] + "/forgejo-runner"]["data"]) == manifest.binary
        else:
            assert not any("data" in row for row in payload["files"].values())
        seen.append(payload)
        return True, json.dumps({"installed": {p: x["sha256"] for p, x in payload["files"].items()}}), ""
    record["native_files"] = provisioner.install(guest, record, paths, "runner")
    provisioner.verify(guest, record, paths, "runner")
    assert len(seen) == 2 and native.STREAM in seen[0]["files"]
    monkeypatch.setitem(native.BINARY_HASHES, "amd64", "0" * 64)
    with pytest.raises(RuntimeError, match="pinned v2"):
        native.NativeProvisioner(manifest.base, manifest.value["image"])


def test_strict_guest_pin_cannot_use_insecure_saved_mux(monkeypatch):
    pin = ("[127.0.0.1]:51100 " + KEY + "\n").encode()
    monkeypatch.setattr(native, "protected_read", lambda *a: pin)
    guest = native.PinnedGuestExec(known_hosts="/protected/pin", host="127.0.0.1", user="runner", port=51100)
    options = guest._options()
    assert "StrictHostKeyChecking=yes" in options and "UserKnownHostsFile=/protected/pin" in options
    assert "ControlPath=none" in options and "ControlMaster=no" in options
    assert not any(value in options for value in ("StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null", "ControlMaster=auto"))
    guest.port = 51101
    with pytest.raises(RuntimeError, match="another endpoint"):
        guest._options()


def test_existing_private_pin_cannot_be_replaced_with_another_key(manifest, tmp_path, monkeypatch):
    provisioner = native.NativeProvisioner(manifest.base, manifest.value["image"])
    directory = tmp_path / "instance"
    directory.mkdir()
    pin = directory / "guest_known_hosts"
    pin.write_bytes(b"different key")
    monkeypatch.setattr(native, "protected_directory", lambda *a: None)
    monkeypatch.setattr(native, "protected_read", lambda *a: pin.read_bytes())
    with pytest.raises(RuntimeError, match="differs from the trusted base"):
        provisioner.pin({"port": 51100}, directory)
    assert pin.read_bytes() == b"different key"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative installation")
def test_literal_installer_pins_parents_and_refuses_symlink_target(tmp_path, monkeypatch, capsys):
    root = tmp_path / "guest"
    root.mkdir()
    reg = root / "Users/runner/runners" / RID / "reg"
    reg.mkdir(parents=True)
    import pwd
    open_original, fstat_original = os.open, os.fstat
    def opened(path, *args, **kwargs):
        return open_original(root if path == "/" else path, *args, **kwargs)
    def info(fd):
        result = fstat_original(fd)
        return SimpleNamespace(st_uid=0, **{key: getattr(result, key) for key in ("st_mode", "st_nlink")})
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fstat", info)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: None)
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(machine="x86_64"))
    monkeypatch.setattr(pwd, "getpwnam", lambda user: SimpleNamespace(pw_uid=os.getuid()))
    body = b"trusted stream bytes"
    payload = {"action": "install", "runner_id": RID, "runner_user": "runner", "architecture": "amd64", "files": {
        native.STREAM: {"data": base64.b64encode(body).decode(), "sha256": hashlib.sha256(body).hexdigest(), "mode": 0o644}}}
    import io, sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exec(native.INSTALL, {})
    target = root / native.STREAM.lstrip("/")
    assert target.read_bytes() == body and json.loads(capsys.readouterr().out)["installed"]
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"never replace this")
    target.symlink_to(outside)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(AssertionError):
        exec(native.INSTALL, {})
    assert outside.read_bytes() == b"never replace this"


@pytest.mark.parametrize("uid,mode", [(501, 0o444), (0, 0o664), (0, 0o646)])
def test_job_writable_or_nonroot_base_cannot_be_attested(monkeypatch, uid, mode):
    import stat
    monkeypatch.setattr(Path, "lstat", lambda p: SimpleNamespace(st_mode=stat.S_IFREG | mode, st_nlink=1, st_uid=uid))
    with pytest.raises(RuntimeError, match="protected root-owned disk"):
        native.base_identity("/trusted/base.qcow2")


def test_first_start_failure_retries_verified_prepared_definition_without_template_copy(pool, host, monkeypatch):
    from agent.runtimes.macos_appliance import MacApplianceRuntime
    original_create, original_start = MacApplianceRuntime.create, MacApplianceRuntime.start
    copied, started = [], []
    def create(self, rid, spec, *, start=True):
        copied.append(start)
        return original_create(self, rid, spec, start=start)
    def start(self, rid):
        started.append(rid)
        if len(started) == 1:
            raise RuntimeError("first start reply lost")
        return original_start(self, rid)
    monkeypatch.setattr(MacApplianceRuntime, "create", create)
    monkeypatch.setattr(MacApplianceRuntime, "start", start)
    with pytest.raises(RuntimeError, match="reply lost"):
        pool.create(RID, SPEC)
    ready = pool._read(RID)
    assert ready["native_files"] and ready["initialized"] is False
    assert ready["listener_disabled"] is False  # An attempted enable is not a disable proof.
    pool.create(RID, SPEC)
    assert copied == [False] and len(started) == 2
    assert pool._read(RID)["initialized"] is True and pool.status(RID)["running"] is True
