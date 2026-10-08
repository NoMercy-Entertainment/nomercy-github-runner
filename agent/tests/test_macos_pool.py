"""Appliance isolation and fail-closed power/storage transitions, no live host."""
import json
import os
from pathlib import Path

import pytest

from agent import naming
from agent.runtimes.appliance_host import BOOT_CLEANUP_LABEL, BOOT_ENTRYPOINT
from agent.runtimes.macos_appliance import MacApplianceRuntime, MacRegistrar
from agent.runtimes.macos_pool import (GIB, MacAppliancePoolRuntime, MacPoolRegistrar,
                                       POOL_LABEL, RUNNER_LABEL)
from .fake_macos import FakeMac, TEMPLATE, TOOLS
from .test_macos_runtime import RID, OTHER, SPEC, PLAN

IMAGE = "sha256:" + "a" * 64


class Host:
    def __init__(self):
        self.units, self.guests, self.calls = {}, {}, []
        self.unreachable = False
        self.ssh_broken = False
        self.shutdown_stuck = False
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self, argv, timeout=30):
        self.calls.append(list(argv))
        if argv[0] == "qemu-img":
            if argv[1] == "info":
                info = {"format": "qcow2", "virtual-size": 100 * GIB}
                if Path(argv[-1]).name == "disk.qcow2":
                    info["backing-filename"] = str(Path(argv[-1]).parents[2] / "base.qcow2")
                return True, json.dumps(info), ""
            Path(argv[-1]).write_bytes(b"overlay")
            return True, "", ""
        assert argv[0] == "docker", "runner commands must never execute on the Linux host"
        if self.unreachable:
            return False, "", "daemon unavailable"
        config = dict(Labels={BOOT_CLEANUP_LABEL: "true"}, Entrypoint=[BOOT_ENTRYPOINT], Cmd=["./Launch.sh"])
        if argv[1:3] == ["image", "inspect"]:
            return True, json.dumps({"Config": config}), ""
        if argv[1] == "inspect":
            data = self.units.get(argv[-1])
            return (True, json.dumps(data), "") if data else (False, "", "not found")
        if argv[1] == "ps":
            filter_ = argv[argv.index("--filter") + 1]
            if filter_.startswith("name="):
                return True, "\n".join(n for n in self.units if n == filter_[5:]), ""
            return True, "\n".join(u["Config"]["Labels"][RUNNER_LABEL] for u in self.units.values()), ""
        if argv[1] == "create":
            name = argv[argv.index("--name") + 1]
            assert name not in self.units
            labels = dict(config["Labels"])
            for index, arg in enumerate(argv):
                if arg == "--label":
                    key, value = argv[index + 1].split("=", 1)
                    labels[key] = value
            config.update(Labels=labels, Image=IMAGE,
                          Env=[argv[i + 1] for i, a in enumerate(argv) if a == "--env"])
            mounts = []
            for index, arg in enumerate(argv):
                if arg == "--mount":
                    parts = dict(p.split("=", 1) for p in argv[index + 1].split(",") if "=" in p)
                    mounts.append(dict(Source=parts["src"], Destination=parts["dst"],
                                       RW="readonly" not in argv[index + 1]))
            port = argv[argv.index("--publish") + 1].split(":")[1]
            host_config = dict(Privileged=False, Memory=int(argv[argv.index("--memory") + 1]),
                               MemorySwap=int(argv[argv.index("--memory-swap") + 1]),
                               NanoCpus=int(argv[argv.index("--cpus") + 1]) * 1000000000,
                               PortBindings={"10022/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]})
            self.units[name] = dict(Name="/" + name, Config=config,
                                    HostConfig=host_config, Mounts=mounts,
                                    State=dict(Running=False, Status="created"))
            return True, name, ""
        name = argv[-1]
        if argv[1] == "start":
            self.units[name]["State"] = dict(Running=True, Status="running")
            rid = self.units[name]["Config"]["Labels"][RUNNER_LABEL]
            if rid in self.guests:
                self.guests[rid].appliance.power = "running"
            return True, name, ""
        if argv[1] == "rm":
            assert self.units[name]["State"]["Running"] is False
            del self.units[name]
            return True, name, ""
        raise AssertionError(argv)

    def guest(self, record):
        rid = record["runner_id"]
        mac = self.guests.setdefault(rid, FakeMac())

        def run(args, **kwargs):
            if self.ssh_broken:
                return False, "", "SSH unavailable"
            if args == ["/usr/bin/uname", "-s"]:
                return True, "Darwin", ""
            if args == ["/usr/bin/sudo", "-n", "/sbin/shutdown", "-h", "now"]:
                mac.calls.append(args)
                if not self.shutdown_stuck:
                    mac.appliance.power = "stopped"
                    self.units[naming.unit_name(rid)]["State"] = dict(Running=False, Status="exited")
                return True, "", ""
            return mac(args, **kwargs)

        inner = MacApplianceRuntime(run=run, fs=mac, tools=TOOLS, remote=True)
        return run, inner, MacRegistrar(run=run, fs=mac)


@pytest.fixture
def host():
    return Host()


@pytest.fixture
def pool(tmp_path, host):
    disk, system = tmp_path / "base.qcow2", tmp_path / "BaseSystem.img"
    disk.write_bytes(b"immutable base")
    system.write_bytes(b"system")
    return MacAppliancePoolRuntime(image=IMAGE, base_disk=str(disk), base_system=str(system),
                                   data_root=str(tmp_path / "instances"), templates=[TEMPLATE],
                                   base_guests_disabled=True, guest={"user": "runner"}, tools=TOOLS,
                                   image_uid=os.getuid() if hasattr(os, "getuid") else 1000,
                                   image_gid=os.getgid() if hasattr(os, "getgid") else 1000,
                                   run=host, guest_factory=host.guest, port_free=lambda p: True,
                                   sleep=host.sleep, clock=lambda: host.now, shutdown_timeout=10)


def test_two_guests_have_distinct_disk_port_and_guest_commands(pool, host):
    pool.create(RID, SPEC)
    pool.create(OTHER, SPEC)
    assert pool._read(RID)["port"] != pool._read(OTHER)["port"]
    creates = [a for a in host.calls if a[:2] == ["docker", "create"]]
    assert len(creates) == 2
    assert all(a[-1] == IMAGE and "--privileged" not in a for a in creates)
    assert all(a[a.index("--memory") + 1] == str(10 * GIB) for a in creates)
    assert all("NOPICKER=true" in a for a in creates)
    assert all(any("readonly" in v and "BaseSystem" in v for v in a) for a in creates)
    assert str(pool._directory(RID) / "disk.qcow2") in " ".join(creates[0])
    assert str(pool._directory(OTHER) / "disk.qcow2") not in " ".join(creates[0])
    assert pool.base_disk.read_bytes() == b"immutable base"
    assert pool.status(RID)["running"] is True
    assert len(pool.instances()) == 2
    assert host.guests[RID] is not host.guests[OTHER]


def test_debloated_guests_keep_independent_nvram(tmp_path, host):
    disk, system, seed = (tmp_path / name for name in ("base.qcow2", "BaseSystem.img", "seed.fd"))
    disk.write_bytes(b"immutable base")
    system.write_bytes(b"system")
    seed.write_bytes(b"authenticated root disabled")
    runtime = MacAppliancePoolRuntime(
        image=IMAGE, base_disk=str(disk), base_system=str(system), nvram_seed=str(seed),
        data_root=str(tmp_path / "instances"), templates=[TEMPLATE],
        base_guests_disabled=True, guest={"user": "runner"}, tools=TOOLS,
        image_uid=os.getuid() if hasattr(os, "getuid") else 1000,
        image_gid=os.getgid() if hasattr(os, "getgid") else 1000,
        run=host, guest_factory=host.guest, port_free=lambda p: True,
        sleep=host.sleep, clock=lambda: host.now,
    )
    runtime.create(RID, SPEC)
    runtime.create(OTHER, SPEC)
    first = runtime._directory(RID) / "nvram.fd"
    second = runtime._directory(OTHER) / "nvram.fd"
    assert first.read_bytes() == second.read_bytes() == seed.read_bytes()
    first.write_bytes(b"guest-specific boot state")
    assert second.read_bytes() == seed.read_bytes()
    assert any(str(first) in " ".join(call) for call in host.calls if call[:2] == ["docker", "create"])


def test_recreate_preserves_overlay_cache_and_logs_but_clears_registration_work(pool, host):
    pool.create(RID, SPEC)
    guest = host.guests[RID]
    inner = pool._context(pool._read(RID))[1]
    paths = inner.paths(RID)
    for area in ("cache", "logs", "reg", "work"):
        guest.put(paths[area] + "/valuable", 123)
    overlay = pool._directory(RID) / "disk.qcow2"
    overlay.write_bytes(b"unique guest disk")
    port = pool._read(RID)["port"]
    pool.stop(RID)
    pool.remove(RID, keep_data=True)
    assert pool.status(RID)["exists"] is False
    assert pool.instances() == []
    assert overlay.read_bytes() == b"unique guest disk"
    pool.create(RID, dict(SPEC, memory="12g", cpus="6"))
    assert pool._read(RID)["port"] == port
    assert guest.exists(paths["cache"] + "/valuable")
    assert guest.exists(paths["logs"] + "/valuable")
    assert not guest.exists(paths["reg"] + "/valuable")
    assert not guest.exists(paths["work"] + "/valuable")
    assert pool.status(RID)["running"] is True


@pytest.mark.parametrize("keep_data", [False, True])
def test_drained_guest_can_deregister_and_remove_without_an_extra_boot(pool, host, keep_data):
    pool.create(RID, SPEC)
    registrar = MacPoolRegistrar(pool)
    registrar.register(RID, PLAN)
    guest = host.guests[RID]
    inner = pool._context(pool._read(RID))[1]
    paths = inner.paths(RID)
    guest.put(paths["cache"] + "/valuable", 123)
    guest.put(paths["logs"] + "/valuable", 123)
    overlay = pool._directory(RID) / "disk.qcow2"
    original = overlay.read_bytes()
    pool.drain(RID)
    registrar.deregister(RID)
    assert pool.status(RID)["running"] is False
    assert pool._power(RID) == "running"
    host.calls.clear()
    guest.calls.clear()

    pool.remove(RID, keep_data=keep_data)

    assert pool.status(RID)["exists"] is False
    assert not any(a[:2] in (["docker", "start"], ["docker", "stop"], ["docker", "kill"])
                   for a in host.calls)
    shutdown = ["/usr/bin/sudo", "-n", "/sbin/shutdown", "-h", "now"]
    assert guest.calls.count(shutdown) == 1
    assert inner.label(RID) in guest.disabled
    if keep_data:
        assert overlay.read_bytes() == original
        assert guest.exists(paths["cache"] + "/valuable")
        assert guest.exists(paths["logs"] + "/valuable")
        assert pool._read(RID)["listener_disabled"] is True
    else:
        assert not pool._directory(RID).exists()


@pytest.mark.parametrize("keep_data", [False, True])
def test_remove_waits_for_a_draining_job_to_finish(pool, host, keep_data):
    pool.create(RID, SPEC)
    registrar = MacPoolRegistrar(pool)
    registrar.register(RID, PLAN)
    guest = host.guests[RID]
    unit = naming.unit_name(RID)
    assert guest.start_job(unit)
    pool.drain(RID)
    assert pool.status(RID)["running"] is True
    guest.calls.clear()

    with pytest.raises(RuntimeError, match="not provably stopped"):
        pool.remove(RID, keep_data=keep_data)

    assert guest.work[unit] == "running"
    assert pool._power(RID) == "running"
    assert not any(a[0] == TOOLS["launchctl"] and a[1] in ("bootout", "kill")
                   for a in guest.calls)
    assert not any("/sbin/shutdown" in a for a in guest.calls)
    assert guest.finish_job(unit)
    registrar.deregister(RID)
    pool.remove(RID, keep_data=keep_data)
    assert pool.status(RID)["exists"] is False


@pytest.mark.parametrize("keep_data", [False, True])
def test_remove_never_stops_a_guest_with_unknown_listener_state(pool, host, monkeypatch, keep_data):
    pool.create(RID, SPEC)
    pool.drain(RID)
    inner = pool._context(pool._read(RID))[1]
    monkeypatch.setattr(inner, "status", lambda rid: {"running": None, "state": "unknown"})
    guest = host.guests[RID]
    guest.calls.clear()

    with pytest.raises(RuntimeError, match="not provably stopped"):
        pool.remove(RID, keep_data=keep_data)

    assert pool._power(RID) == "running"
    assert (pool._directory(RID) / "disk.qcow2").exists()
    assert not guest.calls


def test_stopped_cache_clear_never_enables_or_starts_listener(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    guest = host.guests[RID]
    guest.calls.clear()
    result = pool.clear_cache(RID, {"scopes": ["toolcache"]})
    assert result["measured"] is True
    assert pool.status(RID)["running"] is False
    assert not any(a[0] == TOOLS["launchctl"] and a[1] in ("enable", "bootstrap", "kickstart") for a in guest.calls)
    assert pool._power(RID) == "stopped"


def test_unknown_guest_or_host_never_reports_absence_or_forces_poweroff(pool, host):
    pool.create(RID, SPEC)
    host.ssh_broken = True
    assert pool.status(RID)["exists"] is True
    assert pool.status(RID)["running"] is None
    with pytest.raises(RuntimeError):
        pool.stop(RID)
    assert host.units[naming.unit_name(RID)]["State"]["Running"] is True
    host.unreachable = True
    assert pool.status(RID)["exists"] is None
    assert pool.instances() == [{"runner_id": RID, "state": "unknown"}]
    with pytest.raises(RuntimeError):
        pool.remove(RID)
    assert (pool._directory(RID) / "disk.qcow2").exists()
    assert not any(a[:2] in (["docker", "stop"], ["docker", "kill"]) for a in host.calls)


def test_stopped_telemetry_is_unknown_without_boot_or_ssh(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    guest = host.guests[RID]
    guest.calls.clear()
    assert all(v is None for v in pool.telemetry(RID).values())
    assert pool.logs(RID, 300) == ""
    assert pool.probe(RID, "disk_usage")["ok"] is False
    assert not guest.calls


def test_port_conflict_and_false_disabled_proof_refuse_maintenance_boot(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    pool._port_free = lambda p: False
    with pytest.raises(RuntimeError, match="port is occupied"):
        pool.start(RID)
    pool._port_free = lambda p: True
    record = pool._read(RID)
    pool._write(record, listener_disabled=False)
    with pytest.raises(RuntimeError, match="persistently disabled"):
        pool.clear_cache(RID, {})
    assert pool._power(RID) == "stopped"


def test_ownership_conflict_refuses_delete(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    host.units[naming.unit_name(RID)]["Config"]["Labels"][RUNNER_LABEL] = OTHER
    with pytest.raises(RuntimeError, match="ownership"):
        pool.remove(RID)
    assert (pool._directory(RID) / "disk.qcow2").exists()


def test_delete_removes_only_stopped_owned_instance_and_is_idempotent(pool, host):
    pool.create(RID, SPEC)
    pool.create(OTHER, SPEC)
    with pytest.raises(RuntimeError, match="stop"):
        pool.remove(RID)
    pool.stop(RID)
    pool.remove(RID)
    pool.remove(RID)
    assert not pool._directory(RID).exists()
    assert pool._directory(OTHER).exists()
    assert pool.status(OTHER)["running"] is True
    assert pool.base_disk.exists()


def test_registrar_uses_only_selected_guest_and_preserves_stopped_state(pool, host):
    pool.create(RID, SPEC)
    pool.create(OTHER, SPEC)
    pool.stop(RID)
    registrar = MacPoolRegistrar(pool)
    registrar.register(RID, PLAN)
    assert pool._power(RID) == "stopped"
    assert any(PLAN["token"] in text for text in host.guests[RID].inputs)
    assert not host.guests[OTHER].inputs
    assert PLAN["token"] not in str(host.calls)


@pytest.mark.parametrize("changes", [{"cpus": "1.5"}, {"cpus": "0"}, {"memory": "3g"},
                                      {"memory": "4.5g"}, {"image": "../../bad"},
                                      {"memory_swap": "10g"}, {"adopt": {"label": "old"}}])
def test_bad_request_never_creates_host_unit(pool, host, changes):
    with pytest.raises(ValueError):
        pool.create(RID, dict(SPEC, **changes))
    assert not host.units


def test_lost_create_reply_retry_reuses_disk_and_unit(pool, host):
    pool.create(RID, SPEC)
    disk = pool._directory(RID) / "disk.qcow2"
    disk.write_bytes(b"retained")
    pool.create(RID, SPEC)
    assert disk.read_bytes() == b"retained"
    assert len([a for a in host.calls if a[:2] == ["docker", "create"]]) == 1


def test_stuck_guest_shutdown_is_failure_without_force(pool, host):
    pool.create(RID, SPEC)
    host.shutdown_stuck = True
    with pytest.raises(RuntimeError, match="no forced stop"):
        pool.stop(RID)
    assert pool._power(RID) == "running"
    assert not any(a[:2] == ["docker", "stop"] for a in host.calls)


def test_descendant_processes_with_new_process_groups_are_counted(pool, host):
    pool.create(RID, SPEC)
    inner = pool._context(pool._read(RID))[1]
    pid = inner.status(RID)["pid"]
    original = inner._run

    def run(argv, **kwargs):
        if argv[0] == TOOLS["ps"]:
            return True, f"{pid} 1 {pid} 1.5 100\n600 {pid} 600 75 200\n700 600 700 30 300\n900 1 900 90 900", ""
        return original(argv, **kwargs)

    inner._run = run
    telemetry = pool.telemetry(RID)
    assert telemetry["cpu_percent"] == 106.5
    assert telemetry["mem_used_bytes"] == 600 * 1024
    assert telemetry["cpu_cores"] == 4


def test_duplicate_port_metadata_fails_closed(pool, host):
    pool.create(RID, SPEC)
    pool.create(OTHER, SPEC)
    record = pool._read(OTHER)
    pool._write(record, port=pool._read(RID)["port"])
    with pytest.raises(RuntimeError, match="duplicate"):
        pool.instances()


@pytest.mark.parametrize("verb", ["create", "rm"])
def test_lost_docker_reply_after_effect_is_recovered_without_losing_disk(pool, host, verb):
    original = pool._run
    failed = False

    def run(argv, **kwargs):
        nonlocal failed
        result = original(argv, **kwargs)
        if argv[:2] == ["docker", verb] and not failed:
            failed = True
            raise RuntimeError("agent interrupted after Docker completed")
        return result

    if verb == "create":
        pool._run = run
        with pytest.raises(RuntimeError, match="interrupted"):
            pool.create(RID, SPEC)
        pool.create(RID, SPEC)
        assert pool.status(RID)["running"] is True
    else:
        pool.create(RID, SPEC)
        pool.stop(RID)
        pool._run = run
        with pytest.raises(RuntimeError, match="interrupted"):
            pool.remove(RID, keep_data=True)
        pool.remove(RID, keep_data=True)
        assert pool.status(RID)["exists"] is False
    assert (pool._directory(RID) / "disk.qcow2").exists()
    assert pool.base_disk.read_bytes() == b"immutable base"


def test_host_configuration_drift_refuses_start_and_delete(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    host.units[naming.unit_name(RID)]["HostConfig"]["PortBindings"]["10022/tcp"][0]["HostIp"] = "0.0.0.0"
    assert pool.status(RID)["exists"] is None
    with pytest.raises(RuntimeError, match="configuration differs"):
        pool.start(RID)
    with pytest.raises(RuntimeError, match="configuration differs"):
        pool.remove(RID)


def test_missing_owned_disk_is_not_silently_replaced(pool, host):
    pool.create(RID, SPEC)
    pool.stop(RID)
    (pool._directory(RID) / "disk.qcow2").unlink()
    with pytest.raises(RuntimeError, match="lost its owned disk"):
        pool.create(RID, SPEC)


def test_orphaned_storage_without_metadata_is_not_adopted(pool, host):
    directory = pool._directory(RID)
    directory.mkdir()
    (directory / "disk.qcow2").write_bytes(b"unidentified old registration")
    with pytest.raises(RuntimeError, match="without ownership metadata"):
        pool.create(RID, SPEC)
    assert not host.units


def test_it_declares_the_appliance_hosts_hardware(pool, monkeypatch):
    """The Linux host the appliances run on: logical CPUs and MemTotal, the
    controller's maximum for a macOS fleet's limits (GitHub #5)."""
    from agent import hardware
    monkeypatch.setattr(hardware, "linux_memory", lambda path=None: 64 * GIB)
    assert pool.capabilities()["hardware"] == {"logical_cpus": os.cpu_count(),
                                               "memory_bytes": 64 * GIB}
