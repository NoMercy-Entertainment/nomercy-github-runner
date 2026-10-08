"""Linux runners as containers on the worker's own engine.

One container per runner, named from its runner_id, with five named volumes -
design 15.1's workspace, nested engine data, cache, registration and logs -
also named from its runner_id. Nothing here knows which forge the runner
serves: the forge-specific parts live in the image, behind two fixed entry
points, `/runner/register` and `/runner/deregister`.

**The argv carries T-0002's lessons.** A 60-second stop timeout, because the
runner deregisters on SIGTERM and Docker Engine 29 creates containers with a
one-second one. Swap capped at the memory limit, because the limit is what
makes the kernel reclaim a runner's page cache and swap on top would page it
out instead. 180 seconds to remove, because tearing down a nested engine has
been measured at 110.

**What changed from T-0002.** Names are derived, so removal no longer has to
read the data volume's name off the container first - the old code needed that
because compose and the dashboard named the same volume differently. And the
environment goes to Docker in a file readable only by its owner rather than as
`-e` arguments, which any user on the worker can read in `ps`.

**Creating twice is safe.** The unit's name is derived from the runner_id, so a
unit by that name is this runner's. A second create - after a crash, or a retry
whose first reply was lost - adopts the unit instead of failing on a name
already in use. That is what lets the controller re-drive a creation it lost
track of (T-0308).

**Removing is partial on purpose when asked.** `keep_data=True` is what
`recreate` uses: it keeps the nested engine's data, the cache and the logs -
expensive to rebuild, or history - and discards the workspace and the
registration, because a recreate is meant to give a clean runner with a fresh
registration (design 19.2, scenario 6). Anything left behind by a removal that
should not have been is reported as a failure, not logged and forgotten: to the
controller, storage that outlives its runner is a half instance.

**The layout inside the unit is fixed**, and the image must honour it:
`/runner/work`, `/var/lib/docker`, `/runner/cache`, `/runner/reg`,
`/runner/logs`, announced to the image in environment variables. Today's images
do not use it yet - they predate this design - which is recorded as a
prerequisite for the Linux worker in phase 5.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

from .. import cpu, naming
from ..jobs import current_job
from .adopted import Adopted

#: What a heartbeat asks of several units at once: what each may use.
CAPS_FORMAT = ("{{.Name}}\t{{.HostConfig.CpusetCpus}}\t{{.HostConfig.NanoCpus}}"
               "\t{{.HostConfig.MemorySwap}}")

#: Where each storage area is mounted inside the unit.
MOUNTS = {"work": "/runner/work", "docker": "/var/lib/docker",
          "cache": "/runner/cache", "reg": "/runner/reg",
          "logs": "/runner/logs"}

#: Told to the image, so it knows the layout without being built for it. Set
#: by the runtime and not overridable by a spec: the layout is not negotiable.
LAYOUT_ENV = {"RUNNER_WORK_DIR": MOUNTS["work"],
              "RUNNER_CACHE_DIR": MOUNTS["cache"],
              "RUNNER_REG_DIR": MOUNTS["reg"],
              "RUNNER_LOG_DIR": MOUNTS["logs"],
              "HOME": MOUNTS["work"] + "/.home"}

#: What a `recreate` keeps, and what it discards. See the module docstring.
KEPT_ON_RECREATE = ("docker", "cache", "logs")

RUNNER_LABEL = "nomercy.runner_id"
STOP_TIMEOUT = 60
READONLY_TMPFS = "/run:rw,nosuid,nodev,size=64m,mode=755"

#: How long a unit may take to be made. The first container built from a
#: freshly built unit image pays for its layers being unpacked into the
#: snapshotter: measured at three minutes for the 17 GB GitHub unit on the WSL
#: worker, and at two seconds for every container after it. A deadline of 180
#: seconds sat just under that, so every rebuild timed out with the unit made
#: but not started, and the undo that followed found its storage in use
#: (2026-09-20). Measured again with the image already built: `docker
#: create` alone took 58 seconds on a quiet engine and two and a half
#: minutes while that worker's ten runners were building, with the start
#: after it under a second - and the same two and a half minutes for a
#: 700 MB image as for a 17 GB one, because what it waits for is a disk at
#: 58% full I/O pressure. The room here is what a saturated worker needs,
#: inside the controller's own deadline for a slow verb.
CREATE_TIMEOUT = 1500

#: How long a volume of a unit may take to be made or removed. Thirty
#: seconds was not enough on a busy engine - a rebuild failed on "volume
#: logs: timed out after 30s" with the runner it was rebuilding already
#: deregistered and removed - and this engine took 34 seconds to remove a
#: container holding nothing at all (2026-09-20).
VOLUME_TIMEOUT = 120

#: What `_docker` says when the client gave up: the daemon is still doing
#: whatever was asked, and for a removal that matters - it is the difference
#: between "it did not happen" and "it is not finished yet".
TIMED_OUT = "timed out after"

#: How long a removal is given before the client stops waiting on it.
#: Tearing down a unit's nested engine and its layers outlived 180 seconds
#: on the WSL worker, which read as a failed removal and stranded a rebuild
#: whose runner was already deregistered (2026-09-20).
REMOVE_TIMEOUT = 420

#: How long a unit that the engine is still taking apart is waited for.
#: Docker's removal is asynchronous - the client returns while the daemon
#: works - and everything said to a container meanwhile is refused with
#: "container is marked for removal". Tearing down a nested engine has been
#: measured at 110 seconds, so a recreate that reached its create first
#: failed with the runner it was replacing already gone (2026-09-20).
REMOVAL_WAIT = 240

#: Replaced in tests, so waiting does not make the suite wait.
sleep = time.sleep

#: Which of this runner's own storage each clearable scope lives in (T-1601):
#: an area of design 15.1, or "unit" for the unit's own writable layer -
#: which is this runner's alone, because the unit is. The scopes this runtime
#: offers are generated from this table; a scope that is not in some place
#: of the runner's own is not offered at all (design 15.2).
SCOPE_AREAS = {"workspace": "work", "toolcache": "cache", "temp": "work",
               "engine-build-cache": "docker",
               "engine-images-unused": "docker"}

#: How each scope is cleared: a path inside the unit, or a command to the
#: unit's own nested engine, whose data is the runner's `docker` area.
SCOPE_PATHS = {"workspace": MOUNTS["work"], "toolcache": MOUNTS["cache"],
               "temp": MOUNTS["work"] + "/.unit-tmp"}
ENGINE_SCOPES = {"engine-build-cache": ["docker", "buildx", "prune", "-af"],
                 "engine-images-unused": ["docker", "image", "prune", "-af"]}
SUPPORTED_SCOPES = frozenset(SCOPE_AREAS)
DEFAULT_SCOPES = ("engine-build-cache", "engine-images-unused")


def scope_locations(runner_id):
    """Where each scope of this runner is: always inside its own unit, and in
    one of its own volumes except for temp. Derived from the runner_id
    alone."""
    rid = naming.check(runner_id)
    volumes = naming.names(rid, "linux")
    return {scope: {"unit": naming.unit_name(rid), "area": area,
                    "volume": volumes.get(area),
                    "path": SCOPE_PATHS.get(scope, MOUNTS["docker"])}
            for scope, area in SCOPE_AREAS.items()}


def _docker(args, input=None, timeout=30, merge_stderr=False):
    """Run `docker` with a literal argument list. Returns (ok, out, err).

    Never a shell and never a string: the agent's source is scanned for
    exactly that (T-0401).
    """
    try:
        if merge_stderr:
            p = subprocess.run(["docker", *args], input=input, text=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout)
            return p.returncode == 0, p.stdout.strip(), ""
        p = subprocess.run(["docker", *args], input=input, text=True,
                           capture_output=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"{TIMED_OUT} {timeout}s"
    except OSError as e:
        return False, "", str(e)


_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
          "TB": 1000 ** 4, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
          "TIB": 1024 ** 4}


def _absent(err, what):
    """Whether the engine is saying this thing does not exist.

    Read case-insensitively and without its article: Engine 29 answers `get
    <name>: no such volume` where older ones said `No such volume`, and a
    rebuild that took that for a failure stopped half-way with the unit
    already gone (2026-09-20). What does not exist cannot be left behind.
    """
    # Container inspect uses "no such object" on recent engines. Match the
    # resource noun so a missing daemon socket/file still remains unknown.
    kinds = ("container", "object") if what == "container" else (what,)
    return any(f"no such {kind}" in (err or "").lower() for kind in kinds)


def _state_word(state):
    """What the engine calls a container, in the three words the protocol
    has for it."""
    return {"running": "running", "exited": "stopped", "created": "stopped",
            "paused": "stopped"}.get((state or "").strip(), "unknown")


def _bytes(text):
    """A docker size string in bytes, or None when it cannot be read - never
    0, because "could not measure" is not "empty"."""
    m = re.match(r"\s*([0-9.]+)\s*([KMGT]?I?B)\s*$", text or "", re.I)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def memory_bytes(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmgt]?)[bB]?", str(value), re.I)
    if not match:
        raise ValueError("memory limit must be a positive finite size")
    powers = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}
    size = int(Decimal(match.group(1)) * 1024 ** powers[match.group(2).lower()])
    if size > 2 ** 63 - 1:
        raise ValueError("memory limit exceeds the supported range")
    return size


class LinuxContainerRuntime:
    kind = "linux-container"

    def __init__(self, run=None, adopted=None, storage=None, readonly_root=None):
        self._run = run or _docker
        from .linux_storage import LinuxStorage
        # `storage` doubles as a config.py mapping (production, via
        # __main__.runtime_for) and as an already-built LinuxStorage (every
        # test). `readonly_root` (W8) lives in that mapping, not in
        # LinuxStorage's own constructor, so it is read here and popped
        # before the rest of the mapping is handed to LinuxStorage - passed
        # through, it would be an unexpected keyword argument there.
        # Writable by default, as config.py: see the note there (#14).
        if isinstance(storage, dict):
            storage = dict(storage)
            found = storage.pop("readonly_root", False)
            if readonly_root is None:
                readonly_root = found
        self._readonly_root = bool(readonly_root)
        self._storage = (storage if isinstance(storage, LinuxStorage) else
                         LinuxStorage(**storage) if storage else None)
        # Which container on this engine a runner already is, for the few
        # that were serving before the controller knew them (T-0802). Every
        # other runner's unit is named by its runner_id, and nothing about
        # it has to be remembered.
        self._adopted = adopted if adopted is not None else Adopted()

    def _unit(self, runner_id):
        """The container this runner is: the one it was adopted as, or the
        name its runner_id gives."""
        rid = naming.check(runner_id)
        return self._adopted.name_for(rid, naming.unit_name(rid))

    # ---- lifecycle ---------------------------------------------------------

    def create(self, runner_id, spec):
        if self._storage and not (spec or {}).get("adopt"):
            with self._storage.lock():
                return self._create(runner_id, spec)
        return self._create(runner_id, spec)

    def _create(self, runner_id, spec):
        rid = naming.check(runner_id)
        if (spec or {}).get("adopt"):
            return self._adopt(rid, spec["adopt"])
        name = naming.unit_name(rid)
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("a unit needs an image")
        if self._storage:
            image = self._readonly_image(image)
        memory = str(spec.get("memory") or "").strip()
        memory_swap = spec.get("memory_swap")
        if memory_swap is not None and (not memory or memory_bytes(memory) <= 0
                or memory_bytes(memory_swap) < memory_bytes(memory)):
            raise ValueError("memory_swap must be a positive total RAM+swap limit at least memory")

        existing = self.status(rid)
        if existing.get("exists") is None:
            raise RuntimeError("container status is unknown; storage and registration are left alone")
        managed = None
        if self._storage:
            # Refuse legacy/foreign volumes before allocating a new image.
            self._storage_volumes(rid, require_all=False)
            managed = self._storage.ensure(rid, spec.get("disk_limit"))
        if existing["exists"] and existing.get("state") != "removing":
            if managed:
                self._storage_volumes(rid, require_all=True)
                self._storage_container(rid)
            # Adopt: see the module docstring. A unit left made but not
            # started - a create whose client gave up while the engine was
            # still unpacking the image - is finished here, because the
            # registration that follows has to exec into it.
            self._start(rid)
            return name
        if existing["exists"]:
            self._gone(rid)

        volumes = naming.names(rid, "linux")
        for area in naming.AREAS:
            args = ["volume", "create",
                                    "--label", f"{RUNNER_LABEL}={rid}",
                                    "--label", f"nomercy.area={area}"]
            if managed:
                args += ["--driver", "local", "--opt", "type=none", "--opt", "o=bind",
                         "--opt", f"device={managed[area]}"]
            ok, _, err = self._run([*args, volumes[area]], timeout=VOLUME_TIMEOUT)
            if not ok:
                raise RuntimeError(f"volume {area}: {err}")
        if managed:
            self._storage_volumes(rid, require_all=True)

        args = ["run", "-d", "--name", name,
                # The nested engine needs it; nothing else in the spec can
                # ask for more.
                "--privileged",
                "--restart", "unless-stopped",
                "--log-driver", "local", "--log-opt", "max-size=10m",
                "--log-opt", "max-file=3",
                "--stop-timeout", str(spec.get("stop_timeout", STOP_TIMEOUT)),
                "--label", f"{RUNNER_LABEL}={rid}"]
        if managed and self._readonly_root:
            args += ["--read-only", "--tmpfs", READONLY_TMPFS]
        for key, value in sorted((spec.get("labels") or {}).items()):
            args += ["--label", f"{key}={value}"]
        for area in naming.AREAS:
            args += ["-v", f"{volumes[area]}:{MOUNTS[area]}"]

        cpus = str(spec.get("cpus") or "").strip()
        if cpus not in ("", "0"):
            args += ["--cpus", cpus]
        if memory not in ("", "0"):
            args += ["--memory", memory, "--memory-swap", str(memory_swap) if memory_swap is not None else memory]
        if spec.get("cpuset"):
            args += ["--cpuset-cpus", str(spec["cpuset"])]

        env = dict(spec.get("env") or {})
        env.update(LAYOUT_ENV)
        env_file = _env_file(env)
        try:
            args += ["--env-file", env_file, image]
            ok, out, err = self._run(args, timeout=CREATE_TIMEOUT)
        finally:
            os.unlink(env_file)
        if not ok:
            if "already in use" in err:
                return name                 # another create won; adopt
            raise RuntimeError(err or out or "docker run failed")
        return name

    def _gone(self, rid, wait=None):
        """Wait out a removal the engine is still doing. True once the unit
        is away, False when it is still there after `wait` - or when it is
        there and not being removed at all, which is nothing to wait for."""
        wait = REMOVAL_WAIT if wait is None else wait
        deadline = time.monotonic() + wait
        while True:
            state = self.status(rid)
            if state["exists"] is False:
                return True
            if state["exists"] is None:
                return False
            if state.get("state") != "removing":
                return False
            if time.monotonic() >= deadline:
                return False
            sleep(2)

    def _adopt(self, rid, adopt):
        """Take over a container that is already a runner.

        It keeps its name, its volumes, its registration and the job it may
        be running: all that is written is this worker's note of which
        container the runner_id means. Nothing is created, started, stopped
        or relabelled - a container cannot be relabelled after it is made,
        and a runner with a job must not notice this at all.

        Idempotent, and refused for a container that is not there: adopting
        what does not exist would leave a spec pointing at nothing.
        """
        name = str((adopt or {}).get("label") or "")
        if not name:
            raise ValueError("adopting needs the name of the container that "
                             "is already there")
        ok, _, err = self._run(["inspect", "--type", "container",
                                "--format", "{{.Id}}", name], timeout=30)
        if not ok:
            raise RuntimeError(f"no container {name!r} on this engine to "
                               f"adopt: {err}".strip())
        self._adopted.record(rid, name)
        return name

    def start(self, runner_id):
        if self._storage:
            with self._storage.lock():
                data = self._storage._metadata(runner_id)
                self._storage.ensure(runner_id, data["bytes"])
                self._storage_volumes(runner_id, require_all=True)
                self._storage_container(runner_id)
                return self._start(runner_id)
        return self._start(runner_id)

    def _start(self, runner_id):
        """Started, and in service. The restart policy a drain takes off is
        put back first, so a unit started after a drain is not left one exit
        away from staying down."""
        name = self._unit(runner_id)
        self._check(["update", "--restart=unless-stopped", name], timeout=30)
        self._check(["start", name], timeout=60)

    def stop(self, runner_id):
        """SIGTERM with a grace period long enough to deregister."""
        self._check(["stop", "-t", str(STOP_TIMEOUT),
                     self._unit(runner_id)], timeout=STOP_TIMEOUT + 20)

    def restart(self, runner_id):
        if self._storage:
            with self._storage.lock():
                data = self._storage._metadata(runner_id)
                self._storage.ensure(runner_id, data["bytes"])
                self._storage_volumes(runner_id, require_all=True)
                self._storage_container(runner_id)
                return self._restart(runner_id)
        return self._restart(runner_id)

    def _restart(self, runner_id):
        self._check(["restart", "-t", str(STOP_TIMEOUT),
                     self._unit(runner_id)], timeout=STOP_TIMEOUT + 30)

    def drain(self, runner_id):
        """A graceful stop that stays stopped (OPEN-7): the restart policy
        is taken off first, so the engine does not bring the unit back when
        its process exits, and then the process is sent SIGTERM - not
        `docker stop`, which kills it when its timeout runs out. What the
        runner does with a running job on SIGTERM is its own business; a
        forgejo-runner finishes it within its `shutdown_timeout`, which the
        unit image's `/runner/run` sets (images/linux/unit/) - unset, it
        would cancel the job at once. The image's entry point must also wait
        for the runner rather than exit under it: PID 1 exiting ends the
        container, and every process in it.

        Asked again on every reconciler pass until the runner is drained, so
        it must be safe to repeat - and it is: forgejo-runner keeps its
        signal handler until it exits, so a second SIGTERM while it finishes
        a job is ignored (its main.go: NotifyContext, `defer stop()`)."""
        name = self._unit(runner_id)
        self._check(["update", "--restart=no", name], timeout=30)
        if self.status(runner_id).get("running"):
            self._check(["kill", "--signal=TERM", name], timeout=30)

    def cancel_drain(self, runner_id):
        """Back into service: the restart policy restored, and the unit
        started if it has stopped - which is what a start does."""
        self.start(runner_id)

    def remove(self, runner_id, keep_data):
        if self._storage:
            with self._storage.lock():
                return self._remove(runner_id, keep_data)
        return self._remove(runner_id, keep_data)

    def _storage_volumes(self, rid, require_all=False):
        """Volume names alone are not ownership: verify driver, path and labels."""
        from .linux_storage import AREA_DIRS
        base = self._storage._paths(rid)[3]
        for area, volume in naming.names(rid, "linux").items():
            ok, out, err = self._run(["volume", "inspect", volume], timeout=VOLUME_TIMEOUT)
            if not ok:
                if _absent(err, "volume") and not require_all:
                    continue
                raise RuntimeError(f"cannot verify runner volume {area}: {err}")
            try:
                rows = json.loads(out)
                info = rows[0]
                labels = info.get("Labels") or {}
                expected = {"type": "none", "o": "bind", "device": str(base / AREA_DIRS[area])}
                if (len(rows) != 1 or info.get("Name") != volume or info.get("Driver") != "local"
                        or info.get("Options") != expected or labels.get(RUNNER_LABEL) != rid
                        or labels.get("nomercy.area") != area):
                    raise ValueError("volume is not owned by managed storage")
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise RuntimeError(f"unsafe or legacy volume {volume}; explicit storage migration required") from error

    def _storage_container(self, rid):
        ok, out, err = self._run(["inspect", "--format", "{{json .}}", self._unit(rid)], timeout=30)
        try:
            if not ok:
                raise ValueError(err)
            info = json.loads(out)
            mounts = info["Mounts"]
            host = info["HostConfig"]
            if self._readonly_root and (host.get("ReadonlyRootfs") is not True
                    or (host.get("Tmpfs") or {}).get("/run") != READONLY_TMPFS.split(":", 1)[1]):
                raise ValueError("container root is not read-only with a bounded /run")
            expected = {MOUNTS[area]: volume for area, volume in naming.names(rid).items()}
            actual = {item["Destination"]: item.get("Name") for item in mounts if item.get("Type") == "volume"}
            if any(actual.get(path) != volume for path, volume in expected.items()):
                raise ValueError("container is not using its bounded volumes")
        except (ValueError, TypeError, KeyError) as error:
            raise RuntimeError("existing container storage does not match its owned disk; migration required") from error

    def _readonly_image(self, image):
        args = ["image", "inspect", "--format", "{{json .}}", "--", image]
        ok, out, err = self._run(args, timeout=30)
        if not ok and _absent(err, "image"):
            self._check(["pull", "--", image], timeout=CREATE_TIMEOUT)
            ok, out, err = self._run(args, timeout=30)
        try:
            if not ok:
                raise ValueError(err)
            info = json.loads(out)
            if self._readonly_root and (info.get("Config", {}).get("Labels") or {}).get("nomercy.readonly_root") != "true":
                raise ValueError("image has not declared read-only root compatibility")
            identity = info["Id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
                raise ValueError("image identity is unknown")
            return identity  # avoid a tag changing between validation and run
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise RuntimeError("managed storage requires an image labelled nomercy.readonly_root=true") from error

    def _storage_volume_absent(self, volume):
        ok, _, err = self._run(["volume", "inspect", volume], timeout=VOLUME_TIMEOUT)
        if ok or not _absent(err, "volume"):
            raise RuntimeError(f"volume is not proven absent; retaining filesystem: {volume}")

    def _remove(self, runner_id, keep_data):
        """Remove the unit and its storage. Safe when any of it is absent."""
        rid = naming.check(runner_id)
        if self._storage:
            self._storage_volumes(rid)
        # Stopped before it is forced. `rm -f` gives a unit ten seconds and
        # then kills it, which takes its nested engine down mid-write, and
        # the engine has twice been left unable to finish such a removal:
        # the container sits in `removing`, its name cannot be used again,
        # and the runner being rebuilt is already gone (2026-09-20). A
        # failure here is not one - what will not stop is still removed
        # below - and nothing is aborted: a remove comes after the drain
        # and the deregistration (MIG-9).
        self._run(["stop", "-t", str(STOP_TIMEOUT), self._unit(rid)],
                  timeout=STOP_TIMEOUT + 30)
        ok, _, err = self._run(["rm", "-f", "-v", self._unit(rid)],
                               timeout=REMOVE_TIMEOUT)
        if not ok and not _absent(err, "container")                 and not err.startswith(TIMED_OUT):
            raise RuntimeError(err)
        # The client returns - or gives up - while the daemon is still
        # taking the unit apart, and a volume it still holds cannot be
        # removed. Waited out here rather than reported as storage left
        # behind, which is what the undo of a failed create said while the
        # engine was still working (2026-09-20).
        if not self._gone(rid):
            raise RuntimeError(
                f"the unit of {rid} is still there {REMOVAL_WAIT}s after it "
                f"was removed; its storage is left alone")
        # The unit is gone, so the note of which one it was means nothing.
        # Forgotten after the removal, so a removal that failed leaves the
        # runner still pointing at its container.
        self._adopted.forget(rid)
        volumes = naming.names(rid, "linux")
        left = []
        for area in naming.AREAS:
            if keep_data and (area in KEPT_ON_RECREATE or (self._storage and area == "work")):
                continue
            ok, _, err = self._run(["volume", "rm", volumes[area]],
                                   timeout=VOLUME_TIMEOUT)
            if not ok and not _absent(err, "volume"):
                left.append(f"{area}: {err}")
        if left:
            raise RuntimeError("storage left behind: " + "; ".join(left))
        if self._storage:
            if keep_data:
                self._storage_volume_absent(volumes["reg"])
                if self._storage.existing(rid) is not None:
                    self._storage.reset_registration(rid)
            else:
                for volume in volumes.values():
                    self._storage_volume_absent(volume)
                self._storage.remove(rid)

    def _check(self, args, timeout=30):
        ok, out, err = self._run(args, timeout=timeout)
        if not ok:
            raise RuntimeError(err or out)
        return out

    # ---- observation -------------------------------------------------------

    def status(self, runner_id):
        """Whether the unit exists and runs. `exists` is None - unknown - when
        the engine could not be asked, which is never read as absent."""
        ok, out, err = self._run(["inspect", "--format", "{{json .State}}",
                                  self._unit(runner_id)], timeout=30)
        if not ok:
            if _absent(err, "container"):
                return {"exists": False, "running": False, "state": "absent"}
            return {"exists": None, "running": None, "state": "unknown"}
        try:
            state = json.loads(out)
        except ValueError:
            return {"exists": None, "running": None, "state": "unknown"}
        if (not isinstance(state, dict) or type(state.get("Running")) is not bool
                or state.get("Status") not in
                ("created", "running", "paused", "restarting", "removing", "exited", "dead")
                or (state["Running"] is False and state["Status"] in ("running", "paused"))
                or (state["Running"] is True and state["Status"] in ("created", "exited", "dead"))):
            return {"exists": None, "running": None, "state": "unknown"}
        return {"exists": True, "running": state["Running"],
                "state": state.get("Status"),
                "exit_code": state.get("ExitCode"),
                "started_at": state.get("StartedAt"),
                "restart_count": state.get("RestartCount")}

    def telemetry(self, runner_id):
        ok, out, _ = self._run(["stats", "--no-stream", "--format",
                                "{{.CPUPerc}}\t{{.MemUsage}}",
                                self._unit(runner_id)], timeout=25)
        result = {"cpu_percent": None, "mem_used_bytes": None,
                  "mem_limit_bytes": None}
        if ok and "\t" in out:
            cpu, mem = out.split("\t", 1)
            try:
                result["cpu_percent"] = float(cpu.strip().rstrip("%"))
            except ValueError:
                pass
            used, _, limit = mem.partition("/")
            result["mem_used_bytes"] = _bytes(used)
            result["mem_limit_bytes"] = _bytes(limit)
        self._storage_telemetry(runner_id, result)
        return result

    def _storage_telemetry(self, rid, result):
        if not self._storage:
            return
        try:
            result.update(self._storage.telemetry(rid))
        except (OSError, RuntimeError, ValueError):
            result.update(disk_limit_enforced=False, disk_used_bytes=None,
                          disk_limit_bytes=None, disk_free_bytes=None)

    def telemetry_all(self, runner_ids):
        """CPU and memory of several units in one `docker stats` call - one
        per unit would take longer than a heartbeat's interval on a full
        worker. A unit missing from the answer is left out: unknown."""
        names = {self._unit(r): r for r in runner_ids}
        if not names:
            return {}
        ok, out, _ = self._run(["stats", "--no-stream", "--format",
                                "{{.Name}}	{{.CPUPerc}}	{{.MemUsage}}",
                                *sorted(names)], timeout=25)
        found = {}
        for line in (out.splitlines() if ok else []):
            parts = line.split("	")
            if len(parts) != 3 or parts[0] not in names:
                continue
            entry = {"cpu_percent": None, "mem_used_bytes": None,
                     "mem_limit_bytes": None}
            try:
                entry["cpu_percent"] = float(parts[1].strip().rstrip("%"))
            except ValueError:
                pass
            used, _, limit = parts[2].partition("/")
            entry["mem_used_bytes"] = _bytes(used)
            entry["mem_limit_bytes"] = _bytes(limit)
            found[names[parts[0]]] = entry
        for rid, limits in self._cores(names).items():
            if rid in found:
                found[rid].update(limits)
                found[rid]["host_cores"] = os.cpu_count()
        for rid in runner_ids:
            if self._storage:
                self._storage_telemetry(rid, found.setdefault(rid, {}))
        return found

    def _cores(self, names):
        """What each unit may use, as the engine has it - its cpuset and
        its quota, read back rather than remembered from the spec, so the
        number on a card is what the unit is really held to. One call for
        all of them; a unit missing from the answer is left out."""
        ok, out, _ = self._run(["inspect", "--format", CAPS_FORMAT,
                                *sorted(names)], timeout=25)
        cores = {}
        for line in (out.splitlines() if ok else []):
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            rid = names.get(parts[0].lstrip("/"))
            if rid is not None:
                try:
                    swap = int(parts[3])
                except ValueError:
                    swap = -1
                cores[rid] = {"cpu_cores": cpu.ceiling(parts[1], parts[2]),
                              "mem_swap_limit_bytes": swap if swap > 0 else None}
        return cores

    def jobs(self, runner_ids):
        """Which job each of these units says it is running, from the tail
        of its own output. Read side by side: one `docker logs` per unit in
        turn is a minute on a busy engine, and this runs for every
        measurement. A unit whose log could not be read is left out - its
        card then says it is busy without saying with what, which is true."""
        names = {self._unit(r): r for r in runner_ids}
        if not names:
            return {}

        def read(name):
            ok, out, _ = self._run(["logs", "--tail", "200", name],
                                   timeout=10, merge_stderr=True)
            return names[name], (current_job(out) if ok else None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            return {rid: job for rid, job in pool.map(read, sorted(names))
                    if job}

    def logs(self, runner_id, since_seconds):
        """The unit's own output, stdout and stderr merged in order - the
        GitHub runner logs to one and forgejo-runner to the other."""
        ok, out, _ = self._run(["logs", "--since", f"{since_seconds}s",
                                self._unit(runner_id)],
                               timeout=20, merge_stderr=True)
        return out if ok else ""

    def probe(self, runner_id, probe):
        name = self._unit(runner_id)
        if probe == "disk_usage":
            paths = list(MOUNTS.values())
            ok, out, err = self._run(
                ["exec", name, "du", "-sx", "-B1", *paths], timeout=45)
            try:
                measured = dict((line.split("\t", 1)[1],
                                 int(line.split("\t", 1)[0]))
                                for line in out.splitlines())
                complete = ok and set(measured) == set(paths)
                value = sum(measured.values()) if complete else None
            except (ValueError, IndexError):
                value = None
            return {"ok": value is not None, "value": value,
                    "error": err if not ok else ""}
        if probe == "cache_size":
            rows = self._df(name)
            if rows is None:
                return {"ok": False, "error": "docker system df did not "
                                              "answer"}
            ok, out, _ = self._run(["inspect", "--format",
                                    "{{json .Config.Env}}", name], timeout=15)
            cap = None
            try:
                for env in json.loads(out) if ok else []:
                    if env.startswith("RUNNER_BUILD_CACHE_GC="):
                        cap = _bytes(env.split("=", 1)[1])
            except (ValueError, TypeError, AttributeError):
                pass
            value = rows.get("Build Cache")
            return {"ok": value is not None, "value": value, "cap_bytes": cap}
        if probe == "agent_version":
            ok, out, err = self._run(["exec", name, "cat",
                                      f"{MOUNTS['reg']}/agent_version"],
                                     timeout=15)
            return {"ok": ok, "value": out.strip() if ok else None,
                    "error": "" if ok else err}
        if probe == "job_state":
            return {"ok": False,
                    "error": "job state is authoritative at the forge"}
        return {"ok": False, "error": "unknown probe"}

    def instances(self):
        """Every unit on this engine that belongs to a runner, and its state.
        Raises when the engine cannot be asked: an empty list would say this
        worker runs nothing."""
        ok, out, err = self._run(["ps", "-a", "--filter",
                                  f"label={RUNNER_LABEL}", "--format",
                                  '{{.Label "' + RUNNER_LABEL + '"}}\t'
                                  "{{.State}}"], timeout=30)
        if not ok:
            raise RuntimeError(err or "docker ps failed")
        found, seen = [], set()
        for line in out.splitlines():
            rid, _, state = line.partition("\t")
            if not rid:
                continue
            seen.add(rid)
            found.append({"runner_id": rid, "state": _state_word(state)})
        # The adopted ones, which carry no label: a container's labels are
        # fixed when it is made, and adopting exists precisely not to make it
        # again (T-0802). Without this they are in no heartbeat, and a runner
        # that is serving reads as `unknown` on its card with no telemetry.
        for rid, name in sorted(self._adopted.all().items()):
            if rid in seen:
                continue
            ok, state, _ = self._run(["inspect", "--type", "container",
                                      "--format", "{{.State.Status}}", name],
                                     timeout=30)
            found.append({"runner_id": rid,
                          "state": _state_word(state) if ok else "absent"})
        return found

    def _df(self, name):
        ok, out, _ = self._run(["exec", name, "docker", "system", "df",
                                "--format", "{{json .}}"], timeout=60)
        if not ok:
            return None
        rows = {}
        for line in out.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows[row.get("Type")] = _bytes(row.get("Size"))
        return rows

    # ---- cache -------------------------------------------------------------

    def clear_cache(self, runner_id, policy):
        """Clear the named scopes inside this one unit, measuring each.

        Every command runs inside this runner's own container and every path
        is one of its own mounts, so nothing outside this runner_id can be
        touched. A scope this runtime cannot clear is reported as an error
        for that scope rather than skipped in silence. A second call finds
        nothing to free and still succeeds.
        """
        policy = policy or {}
        scopes = list(policy.get("scopes") or DEFAULT_SCOPES)
        timeout = int(policy.get("timeout", 300))
        with self._cache_unit(runner_id) as name:
            return self._clear_scopes(name, scopes, timeout)

    @contextmanager
    def _cache_unit(self, runner_id):
        name = self._unit(runner_id)
        state = self.status(runner_id)
        if state.get("running"):
            yield name
            return
        if not state.get("exists") or state.get("state") not in ("exited", "created", "stopped"):
            raise RuntimeError("cache cleanup requires a known execution unit")
        image = self._check(["inspect", "--format", "{{.Config.Image}}", name])
        extra = []
        if self._storage:
            self._storage_volumes(runner_id, require_all=True)
            self._storage_container(runner_id)
            if self._storage.existing(runner_id) is None:
                raise RuntimeError("managed maintenance requires the owned filesystem")
            image = self._readonly_image(image)
            if self._readonly_root:
                extra = ["--read-only", "--tmpfs", READONLY_TMPFS]
        helper = f"{name}-maintenance-{uuid.uuid4().hex[:8]}"
        try:
            self._check(["run", "-d", "--name", helper, "--privileged",
                         "--network", "none", "--restart", "no",
                         "--cpus", "1", "--memory", "1g", "--memory-swap", "1g",
                         "--log-driver", "local", "--log-opt", "max-size=10m",
                         "--log-opt", "max-file=2", "--volumes-from", name,
                         "--entrypoint", "/runner/maintenance", *extra, image],
                        timeout=CREATE_TIMEOUT)
            ready = False
            for _ in range(30):
                ok, _, _ = self._run(["exec", helper, "docker", "info"], timeout=5)
                if ok:
                    ready = True
                    break
                sleep(1)
            if not ready:
                raise RuntimeError("maintenance engine did not become ready; runner stayed stopped")
            yield helper
        finally:
            self._run(["stop", "-t", "60", helper], timeout=80)
            ok, _, err = self._run(["rm", "-f", helper], timeout=REMOVE_TIMEOUT)
            if not ok and not _absent(err, "container"):
                raise RuntimeError(f"maintenance cleanup failed: {err}")

    def _clear_scopes(self, name, scopes, timeout):
        per_scope, errors, measured = {}, {}, True

        for scope in scopes:
            if scope not in SUPPORTED_SCOPES:
                errors[scope] = "not supported by this runtime"
                continue
            before = self._measure(name, scope)
            ok, _, err = self._clear(name, scope, timeout)
            if not ok:
                errors[scope] = (err or "failed")[:200]
                if "timed out" in err:
                    break           # the engine is probably still at it
            after = self._measure(name, scope)
            if before is None or after is None:
                measured = False
                continue
            per_scope[scope] = max(0, before - after)
        return {"per_scope": per_scope, "errors": errors,
                "total_bytes": sum(per_scope.values()),
                "measured": measured}

    def _measure(self, name, scope):
        if scope in ENGINE_SCOPES:
            rows = self._df(name)
            if rows is None:
                return None
            return rows.get("Build Cache" if scope == "engine-build-cache"
                            else "Images")
        ok, out, _ = self._run(["exec", name, "du", "-sb",
                                SCOPE_PATHS[scope]], timeout=60)
        if not ok:
            return None
        try:
            return int(out.split()[0])
        except (ValueError, IndexError):
            return None

    def _clear(self, name, scope, timeout):
        if scope in ENGINE_SCOPES:
            return self._run(["exec", name, *ENGINE_SCOPES[scope]],
                             timeout=timeout)
        return self._run(["exec", name, "find", SCOPE_PATHS[scope],
                          "-mindepth", "1", "-delete"], timeout=timeout)

    # ---- what this runtime can do -------------------------------------------

    def memory_capacity(self, path="/proc/meminfo"):
        """Physical RAM and configured swap on this worker."""
        try:
            with open(path, encoding="ascii") as handle:
                fields = dict(line.split(":", 1) for line in handle if ":" in line)
            return {"memory_bytes": int(fields["MemTotal"].split()[0]) * 1024,
                    "swap_bytes": int(fields["SwapTotal"].split()[0]) * 1024}
        except (OSError, KeyError, ValueError, IndexError):
            return None

    def capabilities(self):
        return {"kind": self.kind,
                # Any image this engine can pull, so no list: what a unit is
                # made from is a reference, not something installed here.
                "builds_from": "image",
                "job_containers": True,
                "nested_builds": True,
                "resettable_os": False,
                "disk_limit_enforced": self._storage is not None,
                # The name placement asks for, as the Windows runtime says it.
                "disk_quota": self._storage is not None,
                # OPEN-7: a graceful stop that stays stopped.
                "supports_drain": True,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
                # What a pinned window is cut from: the controller staggers
                # each runner's cpuset over these, so it needs the number
                # before any runner is here to report it.
                "host_cores": os.cpu_count(),
                "notes": "one container per runner, five named volumes "
                         "derived from its runner_id"}


class LinuxRegistrar:
    """Registers the runner from inside its own unit.

    Through the image's `/runner/register`, which is where the forge-specific
    command lives - `config.sh` for GitHub, `forgejo-runner register` for
    Forgejo - so this module stays forge-blind. The plan, token included, goes
    to it on standard input. It never appears in an argument list, where
    anyone on the worker could read it in `ps` for as long as the command ran.
    """

    def __init__(self, run=None, adopted=None):
        self._run = run or _docker
        self._adopted = adopted if adopted is not None else Adopted()

    def _unit(self, runner_id):
        """The same container the runtime means: registering an adopted
        runner has to reach the unit it already is."""
        rid = naming.check(runner_id)
        return self._adopted.name_for(rid, naming.unit_name(rid))

    #: What a registration is given. Two minutes was not enough on a busy
    #: worker: a fresh unit is starting its nested engine while config.sh
    #: runs, and the exec was cut off after the credentials were written and
    #: before `.runner` was - a runner registered at the forge that no unit
    #: could use (2026-09-20). The controller's own step allows longer.
    REGISTER_TIMEOUT = 300

    def register(self, runner_id, plan):
        ok, out, err = self._run(["exec", "-i", self._unit(runner_id),
                                  "/runner/register"],
                                 input=json.dumps(plan),
                                 timeout=self.REGISTER_TIMEOUT)
        if not ok:
            raise RuntimeError(err or "registration failed")
        try:
            answer = json.loads(out)
        except ValueError:
            raise RuntimeError("the unit did not say how it was registered")
        return {"registration_id": str(answer.get("registration_id") or ""),
                "registration_uuid": answer.get("registration_uuid")}

    def deregister(self, runner_id):
        """From inside the unit, where the runner's own credentials are. A
        unit that is gone cannot do it, and says so rather than pretending:
        that registration can only be removed at the forge."""
        ok, _, err = self._run(["exec", self._unit(runner_id),
                                "/runner/deregister"], timeout=60)
        if not ok:
            if _absent(err, "container"):
                raise RuntimeError("the unit is gone; its registration can "
                                   "only be removed at the forge")
            raise RuntimeError(err or "deregistration failed")


def _env_file(env):
    """A file of KEY=value lines readable only by its owner. The caller
    deletes it once `docker run` has read it."""
    for key, value in env.items():
        # Checked here as well as at the door: a line break would let a value
        # write a second variable into the file.
        if any(c in str(value) for c in "\x00\r\n"):
            raise ValueError(f"environment value for {key} is not one line")
    fd, path = tempfile.mkstemp(prefix="unit-env-", text=True)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for key, value in sorted(env.items()):
                fh.write(f"{key}={value}\n")
    except Exception:
        os.unlink(path)
        raise
    return path
