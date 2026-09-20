"""A stand-in for the docker CLI, and for a forge, that behave like the real ones.

The Linux runtime builds `docker` argument lists and reads what comes back.
This interprets those lists against an in-memory model of containers and
volumes, so the runtime - and the contract suite after it - can be exercised
without an engine. It is deliberately not a forgiving fake: where Docker
refuses, it refuses the same way, because those refusals are what the runtime
has to handle.

- `run` with a name already in use fails with Docker's "Conflict" message.
- `volume rm` of a volume a container still uses fails.
- `exec` into a container that is not running fails.
- `rm` and `volume rm` of something absent fail with "No such ...".

The image a unit runs is modelled too, as far as the runtime relies on it: it
provides `/runner/register` and `/runner/deregister`, which talk to the fake
forge, and a nested engine whose build cache and images can be pruned.

`Crash` is the agent process dying: a BaseException, so it passes through
every `except Exception` exactly as a real death would.
"""
import json
import shlex


class Crash(BaseException):
    pass


class FakeForge:
    """Registrations, and whether each registered runner shows online."""

    def __init__(self):
        self.records = {}
        self.reachable = True
        self._next = 1000
        self.unit_running = lambda unit: False

    def register(self, unit, plan):
        if not self.reachable:
            raise ConnectionError("the forge did not answer")
        self._next += 1
        record = {"registration_id": str(self._next),
                  "registration_uuid": f"uuid-{self._next}",
                  "name": plan.get("name", ""),
                  "labels": plan.get("labels", ""),
                  "unit": unit}
        self.records[record["registration_id"]] = record
        return record

    def delete(self, registration_id):
        if not self.reachable:
            raise ConnectionError("the forge did not answer")
        return self.records.pop(str(registration_id), None) is not None

    def for_unit(self, unit):
        return [r for r in self.records.values() if r["unit"] == unit]

    def online(self, registration_id):
        record = self.records.get(str(registration_id))
        return bool(record) and self.unit_running(record["unit"])


def _size(n):
    return f"{int(n)}B"


class FakeDocker:
    def __init__(self, forge=None):
        self.containers = {}
        self.volumes = {}
        self.calls = []
        self.inputs = []
        self.forge = forge or FakeForge()
        self.forge.unit_running = lambda unit: (
            self.containers.get(unit, {}).get("state") == "running")
        #: A verb after which to raise Crash, once.
        self.crash_after = None
        #: verb -> (stderr) to fail once.
        self.fail_once = {}
        #: unit -> "running" | "aborted": the job its runner has.
        self.jobs = {}

    # ---- helpers for tests --------------------------------------------------

    def put(self, volume, path, size):
        """Put `size` bytes of data at `path` inside `volume`."""
        self.volumes.setdefault(volume, _new_volume())["files"][path] = size

    def engine(self, volume, build_cache=0, images=0):
        v = self.volumes.setdefault(volume, _new_volume())
        v["engine"] = {"build_cache": build_cache, "images": images}

    def volume_bytes(self, volume):
        v = self.volumes.get(volume)
        if v is None:
            return None
        return sum(v["files"].values()) + sum(v["engine"].values())

    def snapshot(self):
        """Every volume's contents, for "nothing else changed" checks."""
        return {name: (dict(v["files"]), dict(v["engine"]))
                for name, v in self.volumes.items()}

    # ---- the CLI -------------------------------------------------------------

    def __call__(self, args, input=None, timeout=None, merge_stderr=False):
        args = list(args)
        self.calls.append(args)
        if input is not None:
            self.inputs.append(input)
        verb = args[0]
        if verb in self.fail_once:
            return False, "", self.fail_once.pop(verb)
        handler = getattr(self, f"_{verb.replace('-', '_')}", None)
        if handler is None:
            return False, "", f"unknown docker command {verb!r}"
        result = handler(args[1:], input)
        if self.crash_after == verb:
            self.crash_after = None
            raise Crash(f"the agent died after `docker {verb}`")
        return result

    def _volume(self, args, input):
        sub, rest = args[0], args[1:]
        if sub == "create":
            name = rest[-1]
            labels = {}
            for i, a in enumerate(rest[:-1]):
                if a == "--label":
                    k, _, v = rest[i + 1].partition("=")
                    labels[k] = v
            v = self.volumes.setdefault(name, _new_volume())
            v["labels"].update(labels)
            return True, name, ""
        if sub == "rm":
            name = rest[-1]
            if name not in self.volumes:
                return False, "", f"Error: No such volume: {name}"
            users = [c for c, d in self.containers.items()
                     if name in d["mounts"]]
            if users:
                return False, "", (f"Error response from daemon: remove "
                                   f"{name}: volume is in use - {users}")
            del self.volumes[name]
            return True, name, ""
        if sub == "ls":
            label = rest[rest.index("--filter") + 1].split("=", 1)[1] \
                if "--filter" in rest else None
            names = [n for n, v in self.volumes.items()
                     if label is None or label.split("=")[0] in v["labels"]]
            return True, "\n".join(sorted(names)), ""
        return False, "", f"unknown volume command {sub!r}"

    def _run(self, args, input):
        opts = {"labels": {}, "mounts": {}, "env": {}, "flags": []}
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-d" or a == "--privileged":
                opts["flags"].append(a)
                i += 1
                continue
            if a in ("--name", "--restart", "--stop-timeout", "--cpus",
                     "--memory", "--memory-swap", "--cpuset-cpus"):
                opts[a.lstrip("-")] = args[i + 1]
                i += 2
                continue
            if a == "--label":
                k, _, v = args[i + 1].partition("=")
                opts["labels"][k] = v
                i += 2
                continue
            if a == "-v":
                vol, _, path = args[i + 1].partition(":")
                opts["mounts"][vol] = path
                i += 2
                continue
            if a == "--env-file":
                with open(args[i + 1], encoding="utf-8") as fh:
                    for line in fh.read().splitlines():
                        if line:
                            k, _, v = line.partition("=")
                            opts["env"][k] = v
                i += 2
                continue
            if a == "-e":
                k, _, v = args[i + 1].partition("=")
                opts["env"][k] = v
                i += 2
                continue
            opts["image"] = a
            i += 1
        name = opts.get("name")
        if name in self.containers:
            return False, "", (f'docker: Error response from daemon: '
                               f'Conflict. The container name "/{name}" is '
                               f'already in use by container "abc".')
        for vol in opts["mounts"]:
            self.volumes.setdefault(vol, _new_volume())
        self.containers[name] = {"state": "running", "restarts": 0,
                                 "tmp": {}, "draining": False, **opts}
        self.containers[name].setdefault("restart", "no")
        return True, "0123456789ab", ""

    def _need(self, name):
        if name not in self.containers:
            return False, "", f"Error response from daemon: No such " \
                              f"container: {name}"
        return None

    def _start(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        if self.containers[name]["state"] == "running":
            # A no-op, as the engine's is: a process that was told to drain
            # is still draining.
            return True, name, ""
        self.containers[name]["draining"] = False
        return self._set(name, "running")

    def _stop(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        self._abort_job(name)          # stop kills after its timeout
        return self._set(name, "exited")

    def _restart(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        self._abort_job(name)
        self.containers[name]["restarts"] += 1
        self.containers[name]["draining"] = False
        return self._set(name, "running")

    def _update(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        for a in args[:-1]:
            if a.startswith("--restart="):
                self.containers[name]["restart"] = a.split("=", 1)[1]
        return True, name, ""

    def _kill(self, args, input):
        """SIGTERM to the runner process: it takes nothing new, finishes a
        job it has, then exits - as forgejo-runner does."""
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        if self.jobs.get(name) == "running":
            self.containers[name]["draining"] = True
        else:
            self._exited_by_itself(name)
        return True, name, ""

    def _exited_by_itself(self, name):
        c = self.containers[name]
        c["draining"] = False
        # The engine brings back a unit whose process exited on its own
        # unless the restart policy says not to.
        c["state"] = "running" if c.get("restart") in (
            "always", "unless-stopped") else "exited"

    # ---- jobs: what a forge would give the runner -------------------------

    def start_job(self, name):
        """The forge gives this unit's runner a job, if it would."""
        if not self.offers(name):
            return False
        self.jobs[name] = "running"
        return True

    def finish_job(self, name):
        """The running job ends. True when it ran to completion, False when
        it had been killed. A draining runner exits after it."""
        state = self.jobs.pop(name, None)
        if state != "running":
            return False
        if self.containers.get(name, {}).get("draining"):
            self._exited_by_itself(name)
        return True

    def offers(self, name):
        """Whether the forge could give this unit's runner a new job now."""
        c = self.containers.get(name)
        return (bool(c) and c["state"] == "running" and not c["draining"]
                and self.jobs.get(name) != "running"
                and bool(self.forge.for_unit(name)))

    def _abort_job(self, name):
        if self.jobs.get(name) == "running":
            self.jobs[name] = "aborted"

    def _set(self, name, state):
        self.containers[name]["state"] = state
        return True, name, ""

    def _rm(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return missing
        self._abort_job(name)
        del self.containers[name]
        return True, name, ""

    #: What `inspect --format` is asked for, and what the engine answers.
    #: A Go template is not interpreted here; the few this agent uses are
    #: answered literally, and an unknown one is refused rather than being
    #: silently served the JSON, which is how a caller that asks for a field
    #: would otherwise read a whole document as its value.
    FORMATS = {
        "{{.Id}}": lambda c: "0123456789abcdef",
        "{{.State.Status}}": lambda c: c["state"],
        "{{.State.Running}}": lambda c: str(c["state"] == "running").lower(),
        "{{json .State}}": lambda c: json.dumps({
            "Status": c["state"], "Running": c["state"] == "running",
            "ExitCode": 0, "StartedAt": "2026-09-18T00:00:00Z",
            "RestartCount": c["restarts"]}),
    }

    def _inspect(self, args, input):
        name = args[-1]
        missing = self._need(name)
        if missing:
            return False, "", f"Error: No such object: {name}"
        c = self.containers[name]
        if "--format" in args:
            wanted = args[args.index("--format") + 1]
            if wanted not in self.FORMATS:
                return False, "", f"template: unsupported here: {wanted}"
            return True, self.FORMATS[wanted](c), ""
        return True, json.dumps({
            "Status": c["state"], "Running": c["state"] == "running",
            "ExitCode": 0, "StartedAt": "2026-09-18T00:00:00Z",
            "RestartCount": c["restarts"]}), ""

    def _ps(self, args, input):
        lines = []
        for name, c in sorted(self.containers.items()):
            rid = c["labels"].get("nomercy.runner_id")
            if rid:
                lines.append(f"{rid}\t{c['state']}")
        return True, "\n".join(lines), ""

    def _stats(self, args, input):
        if "--format" in args and "{{.Name}}" in args[args.index("--format")
                                                     + 1]:
            # Several at once, as a heartbeat asks: one line per unit that
            # is running, nothing for one that is not.
            names = args[args.index("--format") + 2:]
            return True, "\n".join(
                f"{n}\t1.50%\t100MiB / 32GiB" for n in names
                if self.containers.get(n, {}).get("state") == "running"), ""
        name = args[-1]
        return self._need(name) or (True, "1.50%\t100MiB / 32GiB", "")

    def _logs(self, args, input):
        name = args[-1]
        return self._need(name) or (True, f"log of {name}", "")

    # ---- exec: what the image inside provides --------------------------------

    def _exec(self, args, input):
        if args[0] == "-i":
            args = args[1:]
        name, cmd = args[0], args[1:]
        missing = self._need(name)
        if missing:
            return missing
        c = self.containers[name]
        if c["state"] != "running":
            return False, "", f"Error response from daemon: container " \
                              f"{name} is not running"
        vol_at = {path: vol for vol, path in c["mounts"].items()}

        def volume_for(path):
            for mount, vol in vol_at.items():
                if path == mount or path.startswith(mount + "/"):
                    return self.volumes[vol]
            return None

        engine = (volume_for("/var/lib/docker") or _new_volume())["engine"]
        if cmd[:3] == ["docker", "system", "df"]:
            return True, "\n".join([
                json.dumps({"Type": "Images", "Size": _size(engine["images"])}),
                json.dumps({"Type": "Build Cache",
                            "Size": _size(engine["build_cache"])})]), ""
        if cmd[:3] == ["docker", "buildx", "prune"]:
            engine["build_cache"] = 0
            return True, "Total: done", ""
        if cmd[:3] == ["docker", "image", "prune"]:
            engine["images"] = 0
            return True, "Total reclaimed space: done", ""
        if cmd[0] == "du":
            path = cmd[-1]
            v = volume_for(path)
            files = v["files"] if v is not None else c["tmp"]
            return True, f"{sum(files.values())}\t{path}", ""
        if cmd[0] == "find" and "-delete" in cmd:
            path = cmd[1]
            v = volume_for(path)
            (v["files"] if v is not None else c["tmp"]).clear()
            return True, "", ""
        if cmd == ["cat", "/runner/reg/agent_version"]:
            return True, "2.336.0\n", ""
        if cmd == ["/runner/register"]:
            plan = json.loads(input or "{}")
            try:
                record = self.forge.register(name, plan)
            except ConnectionError as e:
                return False, "", f"register: {e}"
            reg = volume_for("/runner/reg")
            reg["files"]["registration.json"] = 64
            reg["registration"] = record["registration_id"]
            return True, json.dumps({
                "registration_id": record["registration_id"],
                "registration_uuid": record["registration_uuid"]}), ""
        if cmd == ["/runner/deregister"]:
            reg = volume_for("/runner/reg")
            rid = reg.get("registration") if reg else None
            if rid:
                try:
                    self.forge.delete(rid)
                except ConnectionError as e:
                    return False, "", f"deregister: {e}"
                reg.pop("registration", None)
                reg["files"].pop("registration.json", None)
            return True, "", ""
        return False, "", f"exec: {shlex.join(cmd)}: command not found"


def _new_volume():
    return {"labels": {}, "files": {},
            "engine": {"build_cache": 0, "images": 0}}
