"""One owned QEMU appliance and copy-on-write disk per runner UUID.

The trusted worker configuration chooses the hypervisor image and base disk.
Requests select only an installed guest template. Every host command is fixed
argv; runner commands and registration secrets travel exclusively over SSH.
"""
import json
import os
import re
import shutil
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import hardware, naming
from .appliance_host import BOOT_CLEANUP_LABEL, BOOT_ENTRYPOINT, DockerApplianceHost
from .guest_ssh import GuestExec, GuestFs
from .linux_container import LinuxContainerRuntime, memory_bytes
from .macos_appliance import MacApplianceRuntime, MacRegistrar, SUPPORTED_SCOPES, _exec

GIB = 1024 ** 3
OVERHEAD_BYTES = 2 * GIB
POOL_LABEL = "nomercy.appliance_pool"
RUNNER_LABEL = "nomercy.runner_id"
PINNED_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})\Z")


def _port_free(port):
    with socket.socket() as connection:
        try:
            connection.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _private_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class MacAppliancePoolRuntime:
    kind = "macos-appliance"

    def __init__(self, *, image, base_disk, base_system, data_root, templates,
                 base_guests_disabled, guest, tools=None, nvram_seed=None, ssh_port_base=51000,
                 docker="docker", qemu_img="qemu-img", boot_timeout=600,
                 shutdown_timeout=180, image_uid=1000, image_gid=1000,
                 run=None, guest_factory=None,
                 port_free=_port_free, sleep=time.sleep, clock=time.monotonic):
        if not PINNED_IMAGE.fullmatch(image):
            raise ValueError("appliance pool image must be pinned by sha256")
        if base_guests_disabled is not True:
            raise ValueError("base must be verified to have all runner jobs disabled")
        if not templates or any(not isinstance(t, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", t) for t in templates):
            raise ValueError("pool templates must name verified installed templates")
        self.image, self.templates = image, sorted(set(templates))
        if any(type(value) is not int or not 0 <= value <= 2 ** 31 - 1 for value in (image_uid, image_gid)):
            raise ValueError("pool image UID/GID must be trusted numeric identities")
        self.image_uid, self.image_gid = image_uid, image_gid
        self.base_disk, self.base_system = Path(base_disk).resolve(), Path(base_system).resolve()
        if not self.base_disk.is_file() or not self.base_system.is_file():
            raise ValueError("pool base disk and BaseSystem must exist")
        self.nvram_seed = Path(nvram_seed).resolve() if nvram_seed else None
        if self.nvram_seed and not self.nvram_seed.is_file():
            raise ValueError("pool NVRAM seed must exist")
        self.root = Path(data_root).resolve()
        if (self.base_disk.is_relative_to(self.root) or self.base_system.is_relative_to(self.root)
                or (self.nvram_seed and self.nvram_seed.is_relative_to(self.root))
                or self.root == Path(self.root.anchor)):
            raise ValueError("pool data_root must be a dedicated instances directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        # Exactly one agent owns this pool. Threads are serialized per UUID;
        # flock also excludes a second agent process allocating the same port.
        self._owner_lock = None
        if os.name == "posix":
            import fcntl
            self._owner_lock = open(self.root / ".agent.lock", "a+b")
            os.chmod(self.root / ".agent.lock", 0o600)
            try:
                fcntl.flock(self._owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._owner_lock.close()
                raise RuntimeError("another agent owns this appliance pool")
        self._docker, self._qemu = docker, qemu_img
        self._run, self._guest_factory = run or _exec, guest_factory
        self._guest, self._tools = dict(guest), dict(tools or {})
        self._port_base, self._port_free = ssh_port_base, port_free
        self._boot_timeout, self._shutdown_timeout = boot_timeout, shutdown_timeout
        self._sleep, self._clock = sleep, clock
        self._locks, self._allocation, self._contexts = {}, threading.RLock(), {}
        self._verify_image()
        info = self._json([self._qemu, "info", "--output=json", str(self.base_disk)])
        if info.get("format") != "qcow2" or info.get("backing-filename"):
            raise ValueError("pool base must be a standalone qcow2 disk")
        self.disk_bytes = int(info["virtual-size"])

    def _command(self, argv, timeout=30):
        ok, out, err = self._run(argv, timeout=timeout)
        if not ok:
            raise RuntimeError(err or out or "appliance host command failed")
        return out

    def _json(self, argv):
        value = json.loads(self._command(argv))
        if not isinstance(value, dict):
            raise RuntimeError("unexpected appliance host response")
        return value

    def _verify_image(self):
        image = self._json([self._docker, "image", "inspect", "--format", "{{json .}}", self.image])
        config = image.get("Config") or {}
        if ((config.get("Labels") or {}).get(BOOT_CLEANUP_LABEL) != "true"
                or config.get("Entrypoint") != [BOOT_ENTRYPOINT] or not config.get("Cmd")):
            raise ValueError("pool image lacks the verified boot wrapper and QEMU command")
        self._image_cmd = config["Cmd"]

    @contextmanager
    def _locked(self, rid):
        rid = naming.check(rid)
        with self._allocation:
            lock = self._locks.setdefault(rid, threading.RLock())
        with lock:
            yield rid

    def _directory(self, rid):
        path = self.root / naming.check(rid)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise RuntimeError("runner storage is not an owned directory")
        return path

    def _read(self, rid, required=True):
        path = self._directory(rid) / "instance.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not required:
                return None
            raise RuntimeError("runner has no owned appliance metadata")
        if (not isinstance(value, dict) or value.get("runner_id") != rid or value.get("image") != self.image
                or value.get("base_disk") != str(self.base_disk)
                or value.get("nvram_seed") != (str(self.nvram_seed) if self.nvram_seed else None)
                or value.get("image_uid") != self.image_uid or value.get("image_gid") != self.image_gid
                or type(value.get("port")) is not int
                or not self._port_base <= value["port"] < 65536):
            raise RuntimeError("appliance metadata ownership does not match")
        if (type(value.get("cpus")) is not int or type(value.get("memory")) is not int
                or value.get("template") not in self.templates
                or any(type(value.get(key)) is not bool for key in
                       ("initialized", "retained", "listener_disabled"))):
            raise RuntimeError("appliance metadata is incomplete")
        return value

    def _write(self, record, **changes):
        record.update(changes)
        _private_json(self._directory(record["runner_id"]) / "instance.json", record)

    def _records(self):
        records = []
        for path in self.root.iterdir():
            if path.is_dir():
                try:
                    rid = naming.check(path.name)
                except ValueError:
                    continue
                record = self._read(rid, required=False)
                if record:
                    records.append(record)
        ports = [r["port"] for r in records]
        if len(ports) != len(set(ports)):
            raise RuntimeError("duplicate appliance SSH port assignments")
        return records

    def _allocate(self, rid, spec):
        with self._allocation:
            record = self._read(rid, required=False)
            cpu, memory = self._limits(spec)
            if record:
                if record.get("retained"):
                    if self._power(rid) != "absent":
                        raise RuntimeError("finish removing the retained appliance before recreate")
                    self._write(record, cpus=cpu, memory=memory, template=spec["image"], retained=False)
                elif (record["cpus"], record["memory"], record["template"]) != (cpu, memory, spec["image"]):
                    raise ValueError("existing appliance limits/template require recreate")
                return record
            used = {r["port"] for r in self._records()}
            port = next((p for p in range(self._port_base, 65536) if p not in used and self._port_free(p)), None)
            if port is None:
                raise RuntimeError("no free appliance SSH port")
            directory = self._directory(rid)
            if directory.exists() and any(directory.iterdir()):
                raise RuntimeError("runner storage exists without ownership metadata; repair it before creating")
            directory.mkdir(mode=0o700, exist_ok=True)
            record = dict(runner_id=rid, port=port, image=self.image,
                          base_disk=str(self.base_disk),
                          nvram_seed=str(self.nvram_seed) if self.nvram_seed else None,
                          template=spec["image"],
                          image_uid=self.image_uid, image_gid=self.image_gid,
                          cpus=cpu, memory=memory, listener_disabled=True,
                          initialized=False, retained=False)
            self._write(record)
            return record

    @staticmethod
    def _limits(spec):
        raw = str(spec.get("cpus", 4))
        if not re.fullmatch(r"[1-9][0-9]*", raw) or not 1 <= int(raw) <= 64:
            raise ValueError("appliance cpus must be a whole count from 1 to 64")
        memory = memory_bytes(spec.get("memory", 8 * GIB))
        if memory < 4 * GIB or memory > 128 * GIB or memory % GIB:
            raise ValueError("appliance memory must be whole GiB from 4 to 128")
        if spec.get("memory_swap") or spec.get("cpuset") or spec.get("adopt"):
            raise ValueError("pool does not accept swap, cpuset, or legacy adoption")
        return int(raw), memory

    def _inspect(self, rid):
        name = naming.unit_name(rid)
        ok, out, _ = self._run([self._docker, "inspect", "--type", "container",
                              "--format", "{{json .}}", name], timeout=15)
        if not ok:
            # An unreachable daemon is not evidence that the appliance vanished.
            names = self._command([self._docker, "ps", "-a", "--filter", "name=" + name,
                                   "--format", "{{.Names}}"], timeout=15).splitlines()
            if name not in names:
                return None
            raise RuntimeError("appliance inspection failed")
        data = json.loads(out)
        if not isinstance(data, dict):
            raise RuntimeError("appliance inspection was not an object")
        labels = (data.get("Config") or {}).get("Labels") or {}
        if (data.get("Name", "").lstrip("/") != name or labels.get(RUNNER_LABEL) != rid
                or labels.get(POOL_LABEL) != str(self.root)):
            raise RuntimeError("appliance ownership labels do not match")
        record = self._read(rid, required=False)
        if record:
            config, host = data.get("Config") or {}, data.get("HostConfig") or {}
            environment = dict(value.split("=", 1) for value in config.get("Env") or [] if "=" in value)
            ports = (host.get("PortBindings") or {}).get("10022/tcp") or []
            mounts = {m.get("Destination"): m for m in data.get("Mounts") or []}
            expected = {
                "/home/arch/OSX-KVM/mac_hdd_ng.img": (str(self._directory(rid) / "disk.qcow2"), True),
                "/home/arch/OSX-KVM/BaseSystem.img": (str(self.base_system), False),
                str(self.base_disk): (str(self.base_disk), False),
            }
            if self.nvram_seed:
                expected["/home/arch/OSX-KVM/OVMF_VARS-1024x768.fd"] = (
                    str(self._directory(rid) / "nvram.fd"), True)
            if (config.get("Image") != self.image or config.get("Entrypoint") != [BOOT_ENTRYPOINT]
                    or config.get("Cmd") != self._image_cmd
                    or environment.get("RAM") != str(record["memory"] // GIB)
                    or environment.get("SMP") != str(record["cpus"])
                    or environment.get("CORES") != str(record["cpus"])
                    or environment.get("NOPICKER") != "true"
                    or ports != [{"HostIp": "127.0.0.1", "HostPort": str(record["port"])}]
                    or host.get("Privileged") is not False
                    or host.get("Memory") != record["memory"] + OVERHEAD_BYTES
                    or host.get("MemorySwap") != record["memory"] + OVERHEAD_BYTES
                    or host.get("NanoCpus") != record["cpus"] * 1000000000
                    or any((mounts.get(dst, {}).get("Source"), mounts.get(dst, {}).get("RW")) != pair
                           for dst, pair in expected.items())):
                raise RuntimeError("appliance configuration differs from its owned disk, port, image or limits")
        return data

    def _power(self, rid):
        data = self._inspect(rid)
        if data is None:
            return "absent"
        state = data.get("State") or {}
        if state.get("Running") is True and not state.get("Restarting"):
            return "running"
        if state.get("Status") in ("created", "exited") and not state.get("Running"):
            return "stopped"
        raise RuntimeError("appliance power state is unknown")

    def _context(self, record):
        rid = record["runner_id"]
        if rid not in self._contexts:
            if self._guest_factory:
                self._contexts[rid] = self._guest_factory(record)
            else:
                opts = self._guest
                guest = GuestExec(host="127.0.0.1", user=opts["user"], port=record["port"],
                                  key=opts.get("key"), password=opts.get("password"),
                                  ssh=opts.get("ssh", "ssh"), sshpass=opts.get("sshpass", "sshpass"),
                                  control_dir=str(self._directory(rid) / "ssh.sock"))
                fs = GuestFs(guest)
                inner = MacApplianceRuntime(run=guest, fs=fs, tools=self._tools, remote=True)
                self._contexts[rid] = (guest, inner, MacRegistrar(run=guest, fs=fs))
        return self._contexts[rid]

    def _boot(self, record, maintenance=False):
        rid = record["runner_id"]
        self._records()  # refuse conflicting persistent port assignments
        power = self._power(rid)
        if power == "absent":
            raise RuntimeError("appliance execution unit is absent")
        if maintenance and power == "stopped" and not record.get("listener_disabled"):
            raise RuntimeError("maintenance boot refused: runner was not persistently disabled")
        guest, inner, registrar = self._context(record)
        host = DockerApplianceHost(naming.unit_name(rid), guest, docker=self._docker,
                                   boot_timeout=self._boot_timeout, run=self._run,
                                   sleep=self._sleep, clock=self._clock)
        if power == "stopped" and not self._port_free(record["port"]):
            raise RuntimeError("assigned appliance SSH port is occupied")
        host.boot()
        if maintenance and record.get("initialized"):
            # Never stop a job merely to perform a supposedly stopped cache clear.
            if inner.status(rid).get("running") is not False:
                raise RuntimeError("maintenance guest is not provably quiescent")
        return inner, registrar

    def _shutdown(self, record):
        rid = record["runner_id"]
        power = self._power(rid)
        if power == "stopped":
            return
        if power != "running":
            raise RuntimeError("cannot shut down an unobserved appliance")
        guest, inner, _ = self._context(record)
        if record.get("initialized") and inner.status(rid).get("running") is not False:
            raise RuntimeError("guest runner is not provably stopped; refusing poweroff")
        # launchctl disable persists in the guest across this graceful shutdown.
        ok, out, err = guest(["/usr/bin/sudo", "-n", "/sbin/shutdown", "-h", "now"], timeout=15)
        # SSH can close before shutdown replies: accept only observed poweroff.
        deadline = self._clock() + self._shutdown_timeout
        while self._clock() < deadline:
            if self._power(rid) == "stopped":
                return
            self._sleep(2)
        raise RuntimeError("guest did not power off; no forced stop was attempted" +
                           (": " + (err or out) if not ok else ""))

    def create(self, runner_id, spec):
        with self._locked(runner_id) as rid:
            if spec.get("image") not in self.templates:
                raise ValueError("runner template is not in the verified pool manifest")
            record = self._allocate(rid, spec)
            overlay = self._directory(rid) / "disk.qcow2"
            power = self._power(rid)
            if overlay.is_symlink():
                raise RuntimeError("appliance overlay is not an owned regular file")
            if not overlay.exists():
                if power != "absent" or record.get("initialized"):
                    raise RuntimeError("existing appliance lost its owned disk; refusing to replace data")
                temporary = overlay.with_suffix(".creating")
                if temporary.exists():
                    temporary.unlink()
                self._command([self._qemu, "create", "-f", "qcow2", "-F", "qcow2",
                               "-b", str(self.base_disk), str(temporary)], timeout=120)
                if hasattr(os, "chown"):
                    os.chown(temporary, self.image_uid, self.image_gid)
                os.chmod(temporary, 0o600)
                os.replace(temporary, overlay)
            info = self._json([self._qemu, "info", "-U", "--output=json", str(overlay)])
            if (info.get("format") != "qcow2" or info.get("backing-filename") != str(self.base_disk)
                    or info.get("virtual-size") != self.disk_bytes):
                raise RuntimeError("appliance overlay does not reference its approved base")
            if os.name == "posix":
                stat = overlay.stat()
                if stat.st_uid != self.image_uid or stat.st_gid != self.image_gid or stat.st_mode & 0o777 != 0o600:
                    raise RuntimeError("appliance overlay permissions do not match the trusted image user")
            if self.nvram_seed:
                nvram = self._directory(rid) / "nvram.fd"
                if nvram.is_symlink():
                    raise RuntimeError("appliance NVRAM is not an owned regular file")
                if not nvram.exists():
                    if power != "absent" or record.get("initialized"):
                        raise RuntimeError("existing appliance lost its NVRAM; refusing to replace it")
                    temporary = nvram.with_suffix(".creating")
                    if temporary.exists():
                        temporary.unlink()
                    shutil.copyfile(self.nvram_seed, temporary)
                    if hasattr(os, "chown"):
                        os.chown(temporary, self.image_uid, self.image_gid)
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, nvram)
                if not nvram.is_file():
                    raise RuntimeError("appliance NVRAM is not an owned regular file")
                if os.name == "posix":
                    stat = nvram.stat()
                    if stat.st_uid != self.image_uid or stat.st_gid != self.image_gid or stat.st_mode & 0o777 != 0o600:
                        raise RuntimeError("appliance NVRAM permissions do not match the trusted image user")
            if power == "absent":
                if not self._port_free(record["port"]):
                    raise RuntimeError("assigned appliance SSH port is occupied")
                argv = [self._docker, "create", "--name", naming.unit_name(rid),
                        "--label", RUNNER_LABEL + "=" + rid,
                        "--label", POOL_LABEL + "=" + str(self.root),
                        "--restart", "no", "--device", "/dev/kvm",
                        "--cpus", str(record["cpus"]),
                        "--memory", str(record["memory"] + OVERHEAD_BYTES),
                        "--memory-swap", str(record["memory"] + OVERHEAD_BYTES),
                        "--publish", f"127.0.0.1:{record['port']}:10022",
                        "--mount", f"type=bind,src={overlay},dst=/home/arch/OSX-KVM/mac_hdd_ng.img",
                        "--mount", f"type=bind,src={self.base_disk},dst={self.base_disk},readonly",
                        "--mount", f"type=bind,src={self.base_system},dst=/home/arch/OSX-KVM/BaseSystem.img,readonly",
                        *(["--mount", f"type=bind,src={nvram},dst=/home/arch/OSX-KVM/OVMF_VARS-1024x768.fd"]
                          if self.nvram_seed else []),
                        "--env", f"RAM={record['memory'] // GIB}",
                        "--env", f"SMP={record['cpus']}",
                        "--env", f"CORES={record['cpus']}",
                        "--env", "NOPICKER=true", self.image]
                self._command(argv, timeout=300)
            inner, _ = self._boot(record)
            # Mark before enabling: a crash must never leave a false disabled proof.
            self._write(record, listener_disabled=False)
            inner.create(rid, spec)
            self._write(record, initialized=True)
            return naming.unit_name(rid)

    def start(self, runner_id):
        with self._locked(runner_id) as rid:
            record = self._read(rid)
            inner, _ = self._boot(record)
            self._write(record, listener_disabled=False)
            inner.start(rid)

    def stop(self, runner_id):
        with self._locked(runner_id) as rid:
            record = self._read(rid)
            if self._power(rid) == "stopped":
                return
            inner, _ = self._boot(record)
            inner.stop(rid)
            if inner.status(rid).get("running") is not False:
                raise RuntimeError("runner stop is not proven")
            self._write(record, listener_disabled=True)
            self._shutdown(record)

    def restart(self, runner_id):
        with self._locked(runner_id):
            self.stop(runner_id)
            self.start(runner_id)

    def drain(self, runner_id):
        with self._locked(runner_id) as rid:
            record = self._read(rid)
            if self._power(rid) == "stopped":
                return
            inner, _ = self._boot(record)
            inner.drain(rid)
            self._write(record, listener_disabled=True)

    def cancel_drain(self, runner_id):
        self.start(runner_id)

    def remove(self, runner_id, keep_data=False):
        with self._locked(runner_id) as rid:
            record = self._read(rid, required=False)
            if record is None:
                if self._power(rid) != "absent":
                    raise RuntimeError("refusing removal without owned metadata")
                return
            power = self._power(rid)
            if power == "running":
                # Forgejo drain leaves the guest powered on after its listener
                # exits. Removal may finish that shutdown, but never stop a
                # listener whose job could still be running.
                inner = self._context(record)[1]
                if inner.status(rid).get("running") is not False:
                    raise RuntimeError("runner is not provably stopped; refusing appliance removal")
                inner.stop(rid)
                if inner.status(rid).get("running") is not False:
                    raise RuntimeError("runner stop is not proven; refusing appliance removal")
                self._write(record, listener_disabled=True)
                # A recreation clears guest-owned registration/work before
                # poweroff, avoiding a shutdown followed by another boot.
                if not keep_data or record.get("retained"):
                    self._shutdown(record)
            if keep_data and not record.get("retained"):
                self._write(record, removing_keep_data=True)
                inner, _ = self._boot(record, maintenance=True)
                inner.remove(rid, keep_data=True)
                self._write(record, initialized=False, listener_disabled=True)
                self._shutdown(record)
                # Durable before rm: retry after host crash retains exactly this disk.
                self._write(record, retained=True, removing_keep_data=False)
            if self._power(rid) != "absent":
                if self._power(rid) != "stopped":
                    raise RuntimeError("appliance is not provably stopped")
                self._command([self._docker, "rm", naming.unit_name(rid)], timeout=120)
            if self._power(rid) != "absent":
                raise RuntimeError("appliance removal could not be verified")
            if not keep_data:
                # Only the validated per-UUID directory, never a request path.
                shutil.rmtree(self._directory(rid))
                self._contexts.pop(rid, None)

    def status(self, runner_id):
        rid = naming.check(runner_id)
        try:
            record = self._read(rid, required=False)
            power = self._power(rid)
            if record is None:
                if power != "absent":
                    raise RuntimeError("appliance has no ownership metadata")
                return dict(exists=False, running=False, state="absent")
            common = dict(runtime_template=record["template"], power_state=power)
            if power == "absent":
                return dict(common, exists=False, running=False, state="absent")
            # _power inspected this owned appliance and checked its actual
            # CPU, guest RAM and outer memory caps against the private record.
            # A worker-wide capability alone says nothing about this UUID.
            common["resource_enforcement"] = dict(
                kind=self.kind, appliance_per_runner=True, cpu_enforcement=True,
                memory_enforcement=True, cpu_cores=record["cpus"],
                memory_limit_bytes=record["memory"], memory_overhead_bytes=OVERHEAD_BYTES)
            if power == "stopped":
                return dict(common, exists=True, running=False, state="stopped")
            result = self._context(record)[1].status(rid)
            # A guest network failure or missing launchd definition is a broken
            # existing appliance, never proof its hypervisor unit disappeared.
            return dict(result, **common, exists=True)
        except (OSError, ValueError, RuntimeError) as error:
            return dict(exists=None, running=None, state="unknown", error=str(error))

    def _online(self, rid):
        record = self._read(rid)
        return self._context(record)[1] if self._power(rid) == "running" else None

    def telemetry(self, runner_id):
        rid = naming.check(runner_id)
        result = dict(cpu_percent=None, mem_used_bytes=None, mem_limit_bytes=None,
                      storage_volume_used_bytes=None, storage_volume_total_bytes=None,
                      cpu_cores=None, host_cores=None, host_mem_bytes=None)
        try:
            record = self._read(rid)
            inner = self._online(rid)
            if inner:
                result.update(inner.telemetry(rid))
                result.update(cpu_cores=record["cpus"], mem_limit_bytes=record["memory"])
        except (OSError, ValueError, RuntimeError):
            pass
        return result

    def logs(self, runner_id, since_seconds, max_bytes=256 * 1024):
        rid = naming.check(runner_id)
        inner = self._online(rid)
        return inner.logs(rid, since_seconds, max_bytes) if inner else ""

    def jobs(self, runner_ids):
        from ..jobs import current_job
        return {rid: current_job(self.logs(rid, 86400)) for rid in runner_ids}

    def probe(self, runner_id, probe):
        rid = naming.check(runner_id)
        inner = self._online(rid)
        if not inner:
            return dict(ok=False, value=None, error="guest is powered off")
        got = inner.probe(rid, probe)
        if got.get("ok") and probe in ("disk_usage", "cache_size"):
            # Each runner here has a guest, and a guest disk, of its own:
            # the volume its tree and cache are on bounds it alone.
            got = dict(got, total_bytes=got.get("volume_total_bytes"))
        return got

    def clear_cache(self, runner_id, policy):
        with self._locked(runner_id) as rid:
            record = self._read(rid)
            was_stopped = self._power(rid) == "stopped"
            inner, _ = self._boot(record, maintenance=True)
            try:
                return inner.clear_cache(rid, policy)
            finally:
                if was_stopped:
                    self._shutdown(record)

    def instances(self):
        records = {r["runner_id"]: r for r in self._records()}
        try:
            out = self._command([self._docker, "ps", "-a", "--filter", "label=" + POOL_LABEL + "=" + str(self.root),
                                 "--format", "{{.Label \"nomercy.runner_id\"}}"], timeout=15)
            for value in out.splitlines():
                records.setdefault(naming.check(value), None)
        except (ValueError, RuntimeError):
            if not records:
                raise RuntimeError("appliance inventory could not be observed")
            return [dict(runner_id=rid, state="unknown") for rid in records]
        result = []
        for rid, record in records.items():
            state = self.status(rid)
            if record and record.get("retained") and state.get("exists") is False:
                continue
            unit = dict(runner_id=rid, state=("absent" if state.get("exists") is False
                                             else "running" if state.get("running") is True
                                             else "stopped" if state.get("running") is False
                                             else "unknown"))
            if state.get("resource_enforcement"):
                unit["resource_enforcement"] = state["resource_enforcement"]
            if record and unit["state"] == "running":
                # Through the guest's own runtime, which wrote the hooks and
                # keeps what it found; a stopped guest cannot be read.
                try:
                    guard = self._context(record)[1].origin_guard(rid)
                except (OSError, ValueError, RuntimeError):
                    guard = None
                if guard is not None:
                    unit["origin_guard"] = guard
            result.append(unit)
        return result

    def capabilities(self):
        return dict(kind=self.kind, builds_from="template", templates=self.templates,
                    appliance_per_runner=True, appliance_control=True,
                    cpu_enforcement=True, memory_enforcement=True,
                    per_runner_memory_overhead_bytes=OVERHEAD_BYTES,
                    guest_disk_virtual_bytes=self.disk_bytes, disk_quota=False,
                    supports_drain=True, clear_cache=True, cache_scopes=sorted(SUPPORTED_SCOPES),
                    job_containers=False, nested_builds=False, resettable_os=False,
                    # The Linux host the appliances run on (agent/hardware.py).
                    hardware=hardware.facts("linux"),
                    notes="One QEMU guest and private disk overlay per runner; recreate preserves guest cache/logs.")

    memory_capacity = LinuxContainerRuntime.memory_capacity


class MacPoolRegistrar:
    def __init__(self, pool):
        self.pool = pool

    def _call(self, rid, action, *args):
        with self.pool._locked(rid) as rid:
            record = self.pool._read(rid)
            stopped = self.pool._power(rid) == "stopped"
            _, registrar = self.pool._boot(record, maintenance=stopped)
            try:
                return getattr(registrar, action)(rid, *args)
            finally:
                if stopped:
                    self.pool._shutdown(record)

    def register(self, runner_id, plan):
        return self._call(runner_id, "register", plan)

    def deregister(self, runner_id):
        return self._call(runner_id, "deregister")
