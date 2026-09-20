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

from .. import naming
from .adopted import Adopted

#: Where each storage area is mounted inside the unit.
MOUNTS = {"work": "/runner/work", "docker": "/var/lib/docker",
          "cache": "/runner/cache", "reg": "/runner/reg",
          "logs": "/runner/logs"}

#: Told to the image, so it knows the layout without being built for it. Set
#: by the runtime and not overridable by a spec: the layout is not negotiable.
LAYOUT_ENV = {"RUNNER_WORK_DIR": MOUNTS["work"],
              "RUNNER_CACHE_DIR": MOUNTS["cache"],
              "RUNNER_REG_DIR": MOUNTS["reg"],
              "RUNNER_LOG_DIR": MOUNTS["logs"]}

#: What a `recreate` keeps, and what it discards. See the module docstring.
KEPT_ON_RECREATE = ("docker", "cache", "logs")

RUNNER_LABEL = "nomercy.runner_id"
STOP_TIMEOUT = 60

#: How long a unit may take to be made. The first container built from a
#: freshly built unit image pays for its layers being unpacked into the
#: snapshotter: measured at three minutes for the 17 GB GitHub unit on the WSL
#: worker, and at two seconds for every container after it. A deadline of 180
#: seconds sat just under that, so every rebuild timed out with the unit made
#: but not started, and the undo that followed found its storage in use
#: (2026-09-20). The room here stays inside the controller's own deadline for
#: a slow verb, and `setup-worker.sh` warms each image it builds so this is
#: the margin rather than the rule.
CREATE_TIMEOUT = 240

#: How long a volume of a unit may take to be made or removed. Thirty
#: seconds was not enough on a busy engine - a rebuild failed on "volume
#: logs: timed out after 30s" with the runner it was rebuilding already
#: deregistered and removed - and this engine took 34 seconds to remove a
#: container holding nothing at all (2026-09-20).
VOLUME_TIMEOUT = 120

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
SCOPE_AREAS = {"workspace": "work", "toolcache": "cache", "temp": "unit",
               "engine-build-cache": "docker",
               "engine-images-unused": "docker"}

#: How each scope is cleared: a path inside the unit, or a command to the
#: unit's own nested engine, whose data is the runner's `docker` area.
SCOPE_PATHS = {"workspace": MOUNTS["work"], "toolcache": MOUNTS["cache"],
               "temp": "/tmp"}
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
        return False, "", f"timed out after {timeout}s"
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
    return f"no such {what}" in (err or "").lower()


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


class LinuxContainerRuntime:
    kind = "linux-container"

    def __init__(self, run=None, adopted=None):
        self._run = run or _docker
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
        rid = naming.check(runner_id)
        if (spec or {}).get("adopt"):
            return self._adopt(rid, spec["adopt"])
        name = naming.unit_name(rid)
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("a unit needs an image")

        existing = self.status(rid)
        if existing["exists"] and existing.get("state") != "removing":
            # Adopt: see the module docstring. A unit left made but not
            # started - a create whose client gave up while the engine was
            # still unpacking the image - is finished here, because the
            # registration that follows has to exec into it.
            self.start(rid)
            return name
        if existing["exists"]:
            self._await_removal(rid)

        volumes = naming.names(rid, "linux")
        for area in naming.AREAS:
            ok, _, err = self._run(["volume", "create",
                                    "--label", f"{RUNNER_LABEL}={rid}",
                                    "--label", f"nomercy.area={area}",
                                    volumes[area]],
                                   timeout=VOLUME_TIMEOUT)
            if not ok:
                raise RuntimeError(f"volume {area}: {err}")

        args = ["run", "-d", "--name", name,
                # The nested engine needs it; nothing else in the spec can
                # ask for more.
                "--privileged",
                "--restart", "unless-stopped",
                "--stop-timeout", str(spec.get("stop_timeout", STOP_TIMEOUT)),
                "--label", f"{RUNNER_LABEL}={rid}"]
        for key, value in sorted((spec.get("labels") or {}).items()):
            args += ["--label", f"{key}={value}"]
        for area in naming.AREAS:
            args += ["-v", f"{volumes[area]}:{MOUNTS[area]}"]

        cpus = str(spec.get("cpus") or "").strip()
        memory = str(spec.get("memory") or "").strip()
        if cpus not in ("", "0"):
            args += ["--cpus", cpus]
        if memory not in ("", "0"):
            args += ["--memory", memory, "--memory-swap", memory]
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

    def _await_removal(self, rid, wait=REMOVAL_WAIT):
        """Wait out a removal the engine is still doing, so this create
        builds the unit again rather than talking to the one on its way
        out. Raises when it is still there: a unit that will not go is not
        a unit to build over."""
        deadline = time.monotonic() + wait
        while True:
            state = self.status(rid)
            if not state["exists"] or state.get("state") != "removing":
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the unit of {rid} has been marked for removal for "
                    f"{wait}s and is still there")
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
        """Remove the unit and its storage. Safe when any of it is absent."""
        rid = naming.check(runner_id)
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
                               timeout=180)
        if not ok and not _absent(err, "container"):
            raise RuntimeError(err)
        # The client returns while the daemon is still taking the unit
        # apart, and a volume it still holds cannot be removed. Waited out
        # here rather than reported as storage left behind, which is what
        # the undo of a failed create said while the engine was still
        # working (2026-09-20).
        self._await_removal(rid)
        # The unit is gone, so the note of which one it was means nothing.
        # Forgotten after the removal, so a removal that failed leaves the
        # runner still pointing at its container.
        self._adopted.forget(rid)
        volumes = naming.names(rid, "linux")
        left = []
        for area in naming.AREAS:
            if keep_data and area in KEPT_ON_RECREATE:
                continue
            ok, _, err = self._run(["volume", "rm", volumes[area]],
                                   timeout=VOLUME_TIMEOUT)
            if not ok and not _absent(err, "volume"):
                left.append(f"{area}: {err}")
        if left:
            raise RuntimeError("storage left behind: " + "; ".join(left))

    def _check(self, args, timeout=30):
        ok, out, err = self._run(args, timeout=timeout)
        if not ok:
            raise RuntimeError(err or out)

    # ---- observation -------------------------------------------------------

    def status(self, runner_id):
        """Whether the unit exists and runs. `exists` is None - unknown - when
        the engine could not be asked, which is never read as absent."""
        ok, out, err = self._run(["inspect", "--format", "{{json .State}}",
                                  self._unit(runner_id)], timeout=30)
        if not ok:
            if "No such" in err:
                return {"exists": False, "running": False, "state": "absent"}
            return {"exists": None, "running": None, "state": "unknown"}
        try:
            state = json.loads(out)
        except ValueError:
            return {"exists": None, "running": None, "state": "unknown"}
        return {"exists": True, "running": bool(state.get("Running")),
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
        return result

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
        return found

    def logs(self, runner_id, since_seconds):
        """The unit's own output, stdout and stderr merged in order - the
        GitHub runner logs to one and forgejo-runner to the other."""
        ok, out, _ = self._run(["logs", "--since", f"{since_seconds}s",
                                self._unit(runner_id)],
                               timeout=20, merge_stderr=True)
        return out if ok else ""

    def probe(self, runner_id, probe):
        name = self._unit(runner_id)
        if probe in ("disk_usage", "cache_size"):
            rows = self._df(name)
            if rows is None:
                return {"ok": False, "error": "docker system df did not "
                                              "answer"}
            key = "Images" if probe == "disk_usage" else "Build Cache"
            return {"ok": True, "value": rows.get(key)}
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
        name = self._unit(runner_id)
        policy = policy or {}
        scopes = list(policy.get("scopes") or DEFAULT_SCOPES)
        timeout = int(policy.get("timeout", 300))
        per_scope, errors, measured = {}, {}, True

        if not self.status(runner_id).get("running"):
            raise RuntimeError("the unit is not running")

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

    def capabilities(self):
        return {"kind": self.kind,
                # Any image this engine can pull, so no list: what a unit is
                # made from is a reference, not something installed here.
                "builds_from": "image",
                "job_containers": True,
                "nested_builds": True,
                "resettable_os": False,
                # OPEN-7: a graceful stop that stays stopped.
                "supports_drain": True,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
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
