"""macOS runners inside the macOS appliance: a guest operating system, not a
unit on an engine.

The appliance is the `macos-sequoia` guest (design 9.3, 10.6). The agent runs
inside it, as the runner's own user, and each runner instance is a **launchd
job** in that user's domain, over **its own directory tree** under
`/Users/runner/runners/<runner_id>/` - `work`, `cache`, `reg`, `logs`, and
`tmp` for its TMPDIR. There is no nested engine; macOS runners cannot run job
images (design 9.5), and `capabilities()` says so rather than the dashboard
hiding it.

**The five points of design 10.6**, which are the whole uniformity claim:

1. one control agent speaking section 13's protocol - this runtime behind the
   same `Agent` and `AgentServer` as the other two;
2. `status`, `telemetry`, `logs` and the closed probe set - below;
3. `create`, `start`, `stop`, `remove`, `clear_cache` with the same semantics
   and the same idempotency - the contract suite holds it to that;
4. honest `capabilities()`, including what it cannot do;
5. a `runtime_template` naming what an instance was made from - the template
   recorded at create and reported by `status`.

**The appliance's own power** - booting and shutting down the guest - is not
something a runner inside it can do, so it arrives as an `ApplianceHost`, the
hypervisor side. When one is attached, `create` and `start` make sure the
guest is up first, and clear the boot leftover the recorded restart loop
comes from (`/var/tmp/opencore-image-ng.sh-*` after a hard stop, design
9.3.1) before booting. When none is attached, the agent answering at all is
the proof the guest is up. The concrete host side is T-0803's.

**Telemetry includes the guest's root disk**, because a full one is the other
recorded failure - the runner reads offline while the hypervisor side looks
healthy - and the guest is the only place that sees it.

**The runner's software is a template**, a directory named by the spec's
`image` under `/Users/runner/templates/`, copied into `reg` at create: `run`,
which starts the runner once it is registered; `register`, which reads the
registration plan as JSON on standard input and answers
`{"registration_id", "registration_uuid"}`; and `deregister`. The same two
fixed entry points as the other runtimes, so this module never learns which
forge a runner serves. The self-built `forgejo-runner-darwin-*` artefact
becomes one such template (T-0805).

Nothing here has run inside the real appliance yet; that is MACOS-ENV
(T-0802, T-0803). It is exercised against a fake guest.
"""
import json
import os
import plistlib
import posixpath
import subprocess
import time
from typing import Protocol

from .. import naming
from .localfs import LocalFs

#: The areas an appliance instance has: every one in naming except the
#: nested engine's, which it does not have. Asserted against naming by test.
AREAS = ("work", "cache", "reg", "logs")
KEPT_ON_RECREATE = ("cache", "logs")

LAYOUT_ENV_KEYS = {"work": "RUNNER_WORK_DIR", "cache": "RUNNER_CACHE_DIR",
                   "reg": "RUNNER_REG_DIR", "logs": "RUNNER_LOG_DIR"}
TEMPLATE_MARKER = ".template"
LABEL_PREFIX = "com.nomercy."

#: Only what provably belongs to one runner. Xcode's DerivedData lives in the
#: user's Library, shared by every instance in the guest, so it cannot be
#: attributed and is not offered (design 15.2).
SUPPORTED_SCOPES = frozenset({"workspace", "toolcache", "temp"})
DEFAULT_SCOPES = ("toolcache", "temp")

STOP_TIMEOUT = 60

#: The file a hard stop leaves behind, which makes the next boot loop.
#: MEASURED, docs in memory: macos-runner-opencore-restart-loop.
BOOT_LEFTOVER = "/var/tmp/opencore-image-ng.sh-*"

TOOLS = {
    "launchctl": "/bin/launchctl",
    "ps": "/bin/ps",
    "df": "/bin/df",
    "templates": "/Users/runner/templates",
    "launch_agents": "/Users/runner/Library/LaunchAgents",
    "domain": None,             # gui/<uid> or user/<uid>; see _domain
}


class ApplianceHost(Protocol):
    """The hypervisor side of the appliance: what can boot and stop the guest
    as a whole. Implemented for the real appliance in T-0803."""

    def state(self) -> str: ...                 # running | stopped | unknown
    def clear_boot_leftovers(self) -> list: ...
    def boot(self) -> None: ...


def _exec(args, input=None, timeout=30):
    """Run a program with a literal argument list. Returns (ok, out, err).
    Never a shell and never a string (T-0401)."""
    try:
        p = subprocess.run([*args], input=input, text=True,
                           capture_output=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except OSError as e:
        return False, "", str(e)


class MacApplianceRuntime:
    kind = "macos-appliance"

    def __init__(self, run=None, fs=None, appliance=None, tools=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._appliance = appliance
        self._tools = dict(TOOLS, **(tools or {}))

    # ---- where things are ----------------------------------------------------

    def paths(self, runner_id):
        """Every path this runner owns, derived from its runner_id alone."""
        rid = naming.check(runner_id)
        areas = naming.names(rid, "macos")
        root = posixpath.dirname(areas["work"])
        out = {area: areas[area] for area in AREAS}
        out["root"] = root
        out["tmp"] = posixpath.join(root, "tmp")
        out["plist"] = posixpath.join(self._tools["launch_agents"],
                                      f"{self.label(rid)}.plist")
        return out

    @staticmethod
    def label(runner_id):
        return LABEL_PREFIX + naming.unit_name(runner_id)

    def _domain(self):
        if self._tools["domain"]:
            return self._tools["domain"]
        return f"gui/{os.getuid()}"

    def _target(self, runner_id):
        return f"{self._domain()}/{self.label(runner_id)}"

    def _launchctl(self, *args, timeout=30):
        return self._run([self._tools["launchctl"], *args], timeout=timeout)

    # ---- the appliance as a whole --------------------------------------------

    def _appliance_up(self):
        """Boot the guest if it is down, clearing the leftover that makes a
        boot after a hard stop loop. Nothing to do when this runs inside it."""
        if self._appliance is None:
            return
        state = self._appliance.state()
        if state == "running":
            return
        if state == "unknown":
            raise RuntimeError("the appliance's power state is unknown; not "
                               "booting it blind")
        self._appliance.clear_boot_leftovers()
        self._appliance.boot()

    # ---- lifecycle -----------------------------------------------------------

    def create(self, runner_id, spec):
        rid = naming.check(runner_id)
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("an instance needs a template")
        template = posixpath.join(self._tools["templates"], image)
        self._appliance_up()
        if not self._fs.exists(template):
            raise RuntimeError(f"no runner template {image!r} in this "
                               f"appliance")
        p = self.paths(rid)

        # 1. Storage, private to this user before anything is in it.
        self._fs.makedirs(p["root"])
        self._fs.chmod(p["root"], 0o700)
        for key in (*AREAS, "tmp"):
            self._fs.makedirs(p[key])

        # 2. The runner's software, once.
        marker = posixpath.join(p["reg"], TEMPLATE_MARKER)
        if not self._fs.exists(marker):
            self._fs.copytree(template, p["reg"])
            self._fs.write_text(marker, image)

        # 3. The launchd job. Its environment can carry a token, so the file
        #    is readable by this user alone.
        self._fs.makedirs(self._tools["launch_agents"])
        self._fs.write_text(p["plist"], self._plist(rid, spec, p),
                            mode=0o600)

        # 4. Loaded and running, from whatever state a cut-off create left.
        self._load(rid)
        if not self.status(rid).get("running"):
            self._check(self._launchctl("kickstart", self._target(rid)))
        return naming.unit_name(rid)

    def _plist(self, rid, spec, p):
        env = dict(spec.get("env") or {})
        for area, key in LAYOUT_ENV_KEYS.items():
            env[key] = p[area]
        env["TMPDIR"] = p["tmp"] + "/"
        log = posixpath.join(p["logs"], "runner.log")
        job = {"Label": self.label(rid),
               "ProgramArguments": [posixpath.join(p["reg"], "run")],
               "WorkingDirectory": p["work"],
               "EnvironmentVariables": env,
               "StandardOutPath": log, "StandardErrorPath": log,
               "RunAtLoad": True,
               "KeepAlive": {"SuccessfulExit": False},
               "ExitTimeOut": STOP_TIMEOUT,
               "ProcessType": "Standard"}
        return plistlib.dumps(job).decode("utf-8")

    def _load(self, rid):
        """Bootstrap the job unless launchd already has it."""
        if self._loaded(rid):
            return
        ok, out, err = self._launchctl("bootstrap", self._domain(),
                                       self.paths(rid)["plist"])
        if not ok and "already" not in (out + err).lower():
            raise RuntimeError(err or out or "launchctl bootstrap failed")

    def _loaded(self, rid):
        ok, out, err = self._launchctl("print", self._target(rid))
        if ok:
            return True
        if _not_found(out + err):
            return False
        raise RuntimeError(err or out or "launchctl print failed")

    def start(self, runner_id):
        rid = naming.check(runner_id)
        self._appliance_up()
        self._load(rid)
        self._check(self._launchctl("kickstart", self._target(rid)))

    def stop(self, runner_id):
        """Unloaded, so launchd does not start it again. SIGTERM first, and
        the ExitTimeOut grace a deregistration needs."""
        rid = naming.check(runner_id)
        ok, out, err = self._launchctl("bootout", self._target(rid),
                                       timeout=STOP_TIMEOUT + 30)
        if not ok and not _not_found(out + err):
            raise RuntimeError(err or out or "launchctl bootout failed")

    def restart(self, runner_id):
        rid = naming.check(runner_id)
        self._load(rid)
        self._check(self._launchctl("kickstart", "-k", self._target(rid),
                                    timeout=STOP_TIMEOUT + 30))

    def remove(self, runner_id, keep_data):
        """Remove the job and its storage. Safe when any of it is absent."""
        rid = naming.check(runner_id)
        self.stop(rid)
        p = self.paths(rid)
        self._fs.remove(p["plist"])
        doomed = ([p[a] for a in AREAS if a not in KEPT_ON_RECREATE]
                  + [p["tmp"]]) if keep_data else [p["root"]]
        left = []
        for path in doomed:
            try:
                self._fs.rmtree(path)
            except OSError as e:
                left.append(f"{posixpath.basename(path)}: {e}")
        if left:
            raise RuntimeError("storage left behind: " + "; ".join(left))

    def _check(self, result):
        ok, out, err = result
        if not ok:
            raise RuntimeError(err or out or "failed")

    # ---- observation ---------------------------------------------------------

    def status(self, runner_id):
        """Whether the instance exists and runs, and what it was made from.
        `exists` is None - unknown - when launchd could not be asked."""
        rid = naming.check(runner_id)
        p = self.paths(rid)
        if not self._fs.exists(p["plist"]):
            return {"exists": False, "running": False, "state": "absent"}
        ok, out, err = self._launchctl("print", self._target(rid))
        template = self._template(p)
        if not ok:
            if _not_found(out + err):
                return {"exists": True, "running": False, "state": "exited",
                        "runtime_template": template}
            return {"exists": None, "running": None, "state": "unknown"}
        state = _field(out, "state") or "unknown"
        return {"exists": True, "running": state == "running",
                "state": "running" if state == "running" else "exited",
                "pid": _int(_field(out, "pid")),
                "runtime_template": template}

    def _template(self, p):
        try:
            return self._fs.read_text(posixpath.join(p["reg"],
                                                     TEMPLATE_MARKER)).strip()
        except OSError:
            return None

    def telemetry(self, runner_id):
        """This instance's processes, and the guest's root disk."""
        result = {"cpu_percent": None, "mem_used_bytes": None,
                  "mem_limit_bytes": None, "root_disk_used_bytes": None,
                  "root_disk_total_bytes": None}
        pid = self.status(runner_id).get("pid")
        if pid:
            ok, out, _ = self._run([self._tools["ps"], "-A", "-o",
                                    "pgid=,%cpu=,rss="], timeout=15)
            if ok:
                cpu, rss, seen = 0.0, 0, False
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) == 3 and parts[0] == str(pid):
                        seen = True
                        cpu += float(parts[1])
                        rss += int(parts[2]) * 1024
                if seen:
                    result["cpu_percent"] = round(cpu, 2)
                    result["mem_used_bytes"] = rss
        ok, out, _ = self._run([self._tools["df"], "-k", "/"], timeout=15)
        lines = out.splitlines() if ok else []
        if len(lines) >= 2:
            parts = lines[-1].split()
            try:
                result["root_disk_total_bytes"] = int(parts[1]) * 1024
                result["root_disk_used_bytes"] = int(parts[2]) * 1024
            except (IndexError, ValueError):
                pass
        return result

    def logs(self, runner_id, since_seconds, max_bytes=256 * 1024):
        path = posixpath.join(self.paths(runner_id)["logs"], "runner.log")
        try:
            if time.time() - self._fs.mtime(path) > since_seconds:
                return ""
            return self._fs.tail(path, max_bytes)
        except OSError:
            return ""

    def probe(self, runner_id, probe):
        p = self.paths(runner_id)
        if probe == "disk_usage":
            value = self._fs.du(p["root"])
        elif probe == "cache_size":
            value = self._fs.du(p["cache"])
        elif probe == "agent_version":
            try:
                value = self._fs.read_text(
                    posixpath.join(p["reg"], "agent_version")).strip()
            except OSError as e:
                return {"ok": False, "value": None, "error": str(e)}
        elif probe == "job_state":
            return {"ok": False,
                    "error": "job state is authoritative at the forge"}
        else:
            return {"ok": False, "error": "unknown probe"}
        if value is None:
            return {"ok": False, "error": "could not be measured"}
        return {"ok": True, "value": value}

    def instances(self):
        """Every runner instance in this appliance, and its state."""
        found = []
        prefix = LABEL_PREFIX + naming.PREFIX + "-"
        for name in self._fs.listdir(self._tools["launch_agents"]):
            if not (name.startswith(prefix) and name.endswith(".plist")):
                continue
            rid = name[len(prefix):-len(".plist")]
            try:
                naming.check(rid)
            except naming.InvalidRunnerId:
                continue
            s = self.status(rid)
            found.append({"runner_id": rid,
                          "state": "running" if s.get("running") else
                          ("stopped" if s.get("exists") else "unknown")})
        return found

    # ---- cache ---------------------------------------------------------------

    def clear_cache(self, runner_id, policy):
        p = self.paths(runner_id)
        where = {"workspace": p["work"], "toolcache": p["cache"],
                 "temp": p["tmp"]}
        scopes = list((policy or {}).get("scopes") or DEFAULT_SCOPES)
        per_scope, errors, measured = {}, {}, True
        for scope in scopes:
            if scope not in SUPPORTED_SCOPES:
                errors[scope] = "not supported by this runtime"
                continue
            before = self._fs.du(where[scope])
            try:
                self._fs.clear_dir(where[scope])
            except OSError as e:
                errors[scope] = str(e)[:200]
            after = self._fs.du(where[scope])
            if before is None or after is None:
                measured = False
                continue
            per_scope[scope] = max(0, before - after)
        return {"per_scope": per_scope, "errors": errors,
                "total_bytes": sum(per_scope.values()),
                "measured": measured}

    # ---- what this runtime can do --------------------------------------------

    def capabilities(self):
        return {"kind": self.kind,
                # A macOS guest runs no job images (design 9.5).
                "job_containers": False,
                "nested_builds": False,
                # The appliance can be reset to a snapshot in principle
                # (design 9.5, option B); nothing here does it yet.
                "resettable_os": False,
                # OPEN-7: nothing in the protocol can drain a runner yet.
                "supports_drain": False,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
                "notes": "one launchd job per runner inside the macOS "
                         "appliance, over a directory tree only its user "
                         "can read; no per-instance memory or CPU cap - the "
                         "appliance's own size is the limit"}


class MacRegistrar:
    """Registers the runner through its template's `register`, with the plan -
    token included - on standard input, never in an argument list."""

    def __init__(self, run=None, fs=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()

    def _entry(self, runner_id, name):
        reg = naming.names(naming.check(runner_id), "macos")["reg"]
        return posixpath.join(reg, name)

    def register(self, runner_id, plan):
        entry = self._entry(runner_id, "register")
        if not self._fs.exists(entry):
            raise RuntimeError("the instance has no registration entry point")
        ok, out, err = self._run([entry], input=json.dumps(plan), timeout=120)
        if not ok:
            raise RuntimeError(err or "registration failed")
        try:
            answer = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("the instance did not say how it was "
                               "registered")
        return {"registration_id": str(answer.get("registration_id") or ""),
                "registration_uuid": answer.get("registration_uuid")}

    def deregister(self, runner_id):
        entry = self._entry(runner_id, "deregister")
        if not self._fs.exists(entry):
            raise RuntimeError("the instance is gone; its registration can "
                               "only be removed at the forge")
        ok, out, err = self._run([entry], timeout=60)
        if not ok:
            raise RuntimeError(err or out or "deregistration failed")


def _not_found(text):
    """launchd's ways of saying it has no such job."""
    text = text.lower()
    return ("could not find service" in text or "no such process" in text
            or "113" in text)


def _field(text, name):
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key == name:
            return value.strip()
    return None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
